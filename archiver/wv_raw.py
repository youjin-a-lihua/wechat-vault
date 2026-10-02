#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Vault · 存档轨归档器（双轨制之「存档轨」）
==================================================

职责（设计文档 3.3 双轨制）：
  接收微信官方产生的**不可读原始包**，做灾难恢复级存档，长期保存于 NAS。
  这部分数据不解析、不解密、不建索引 —— 因为构造上就打不开（见下）。

存档轨的数据类型：
  1. 手机微信「聊天记录迁移与备份 → 备份聊天记录到电脑」产生的备份包
     （Windows 端通常是一组 Backup.db / BAK_0_TEXT / BAK_0_MEDIA / ... 文件）
  2. Windows 微信 `xwechat_files/` 全量目录（db_storage 加密库 + msg 附件）
  3. 任意 .bak / .zip / .tar 原始包

为什么不能解析（设计文档 2.2 可读性矩阵）：
  - `Backup/*.bak`、`db_storage/*.db` 是设备绑定 + SQLCipher4 加密的
  - 换设备/换账号都打不开，**只能在原机 + 原账号恢复**
  - 所以它的价值是「原始存档」（防手机丢），而不是「可读数据」

本模块做的事（守好"存档不丢、可校验"这条底线）：
  - 扫描投放目录，把原始包**按日期分代**搬进 raw-snapshots/YYYY-MM-DD/
  - 逐文件 SHA-256 指纹 + 大小 + 时间，写入 MANIFEST
  - 幂等：同一内容重复投放不产生重复存档
  - 生成 archive-index.json，记录每一代存档的内容清单

用法：
  python wv_raw.py ingest --source <投放目录> --dest <raw-snapshots> [--manifest <MANIFEST.json>]
  python wv_raw.py list   --dest <raw-snapshots>
  python wv_raw.py verify --dest <raw-snapshots>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import logging

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# 存档轨认的文件类型。注意：**不含** .txt/.csv/.json/.html ——
# 那些是"可读轨"的输入，由 wv_archiver.py 处理，两者严格分工不重叠。
RAW_EXTS = {".bak", ".db", ".zip", ".tar", ".gz", ".7z", ".rar", ".dat", ".bin"}

# 这些文件名（不论扩展名）一律视为存档轨内容 —— 微信备份包的组成部分。
# 实测：Windows 微信「备份与恢复」产生的是一组**无扩展名**文件：
#   Backup.db  BAK_0_TEXT  BAK_0_MEDIA  BAK_1_TEXT  BAK_1_MEDIA ...
RAW_FILENAMES = {"backup.db", "bak_0_text", "bak_0_media"}

# 无扩展名但必须收的文件名前缀（微信备份包分片）
RAW_NAME_PREFIXES = ("bak_",)

# 跳过项：隐藏/临时/占位
_SKIP_PREFIX = (".", "~$", "_")


def now_ts() -> int:
    return int(time.time())


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """流式计算 SHA-256（大文件不爆内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _is_raw_candidate(p: Path) -> bool:
    """是否属于存档轨处理范围。"""
    if not p.is_file():
        return False
    name = p.name
    if name.startswith(_SKIP_PREFIX):
        return False
    low = name.lower()
    # ① 微信备份包（无扩展名的分片，如 BAK_0_TEXT）
    if low in RAW_FILENAMES or low.startswith(RAW_NAME_PREFIXES):
        return True
    # ② 常见归档/库文件扩展名
    return p.suffix.lower() in RAW_EXTS


def _load_index(dest: Path) -> dict:
    idx = dest / "archive-index.json"
    if idx.is_file():
        try:
            return json.loads(idx.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("原始档索引损坏，按空索引继续（该批快照暂不可见）：%s", e)
            pass
    return {"version": "1.0", "generations": [], "files": []}


def _save_index(dest: Path, data: dict) -> Path:
    data["updated_at"] = datetime.now(CST).isoformat()
    out = dest / "archive-index.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _merge_manifest(manifest_path: Path, raw_files: list[dict], dest: Path) -> None:
    """把存档轨文件并入主 MANIFEST.json（若存在），保持单一完整性清单。"""
    if not manifest_path:
        return
    data: dict = {}
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
    data.setdefault("vault_version", "1.0")
    # 存档轨条目独立成段，不与可读轨的 files 混淆
    data["raw_archive"] = {
        "root": str(dest),
        "n_files": len(raw_files),
        "total_bytes": sum(f.get("size") or 0 for f in raw_files),
        "files": raw_files,
        "updated_at": datetime.now(CST).isoformat(),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def ingest(source: Path, dest: Path, manifest_path: Path | None = None,
           verbose: bool = True, day: str | None = None) -> dict:
    """
    把 source 下所有存档轨原始包按日期分代搬进 dest/<YYYY-MM-DD>/。

    幂等：按内容 SHA-256 去重 —— 已在任何一代存过的内容不再重复存。
    搬运而非复制：源文件搬走后投放目录保持干净（节省 SSD 空间）。
    """
    if not source.is_dir():
        raise NotADirectoryError(source)
    dest.mkdir(parents=True, exist_ok=True)

    day = day or datetime.now(CST).strftime("%Y-%m-%d")
    gen_dir = dest / day
    idx = _load_index(dest)
    known = {f["sha256"] for f in idx.get("files", []) if f.get("sha256")}

    stat = {"moved": 0, "skip_dup": 0, "skip_filter": 0, "errors": 0, "bytes": 0}
    added: list[dict] = []

    # 收集候选（含子目录，支持整个 xwechat_files 目录丢进来）
    candidates: list[Path] = []
    for root, _dirs, fns in os.walk(source):
        # 不处理目标目录自身（避免自我递归）
        if Path(root).resolve() == dest.resolve():
            continue
        for fn in sorted(fns):
            p = Path(root) / fn
            if _is_raw_candidate(p):
                candidates.append(p)
            else:
                stat["skip_filter"] += 1

    if verbose:
        print(f"存档轨扫描: {source}")
        print(f"  候选原始包: {len(candidates)} 个")

    for p in candidates:
        try:
            size = p.stat().st_size
            # 大文件指纹计算可能耗时，先按 size 粗筛（已知相同 size 的再算哈希）
            fhash = sha256_file(p)
            if fhash in known:
                stat["skip_dup"] += 1
                if verbose:
                    print(f"  [重复] {p.name}（内容已在库，跳过）")
                continue

            gen_dir.mkdir(parents=True, exist_ok=True)
            target = gen_dir / p.name
            # 同名不同内容 → 加指纹后缀，绝不覆盖
            if target.exists():
                target = gen_dir / f"{p.stem}_{fhash[:8]}{p.suffix}"

            shutil.move(str(p), str(target))
            rel = target.relative_to(dest)
            rec = {
                "path": str(rel).replace("\\", "/"),
                "name": p.name,
                "generation": day,
                "sha256": fhash,
                "size": size,
                "mtime": p.stat().st_mtime if p.exists() else time.time(),
                "archived_at": now_ts(),
            }
            added.append(rec)
            known.add(fhash)
            stat["moved"] += 1
            stat["bytes"] += size
            if verbose:
                print(f"  [存档] {p.name} → {rel}（{size/1048576:.1f} MB）")
        except Exception as e:
            stat["errors"] += 1
            print(f"  [错误] {p}: {e}", file=sys.stderr)

    if added:
        idx.setdefault("files", []).extend(added)
        idx.setdefault("generations", [])
        if day not in idx["generations"]:
            idx["generations"].append(day)
        _save_index(dest, idx)
        _merge_manifest(manifest_path, idx["files"], dest)

    # 清理空目录（搬运后的残留）
    for root, dirs, fns in os.walk(source, topdown=False):
        if Path(root).resolve() == source.resolve():
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:  # 可安全忽略：目录非空说明仍有内容，本就不该删
            pass

    stat["generation"] = day
    stat["gen_dir"] = str(gen_dir)
    if verbose:
        print(f"完成：存档 {stat['moved']} / 重复跳过 {stat['skip_dup']} "
              f"/ 非存档轨文件 {stat['skip_filter']} / 错误 {stat['errors']}"
              f"（{stat['bytes']/1048576:.1f} MB）")
    return stat


def list_generations(dest: Path) -> dict:
    """列出现有存档代与内容。"""
    idx = _load_index(dest)
    by_gen: dict[str, list[dict]] = {}
    for f in idx.get("files", []):
        by_gen.setdefault(f.get("generation") or "未知", []).append(f)
    return {
        "root": str(dest),
        "n_files": len(idx.get("files", [])),
        "total_bytes": sum(f.get("size") or 0 for f in idx.get("files", [])),
        "generations": {g: {"n_files": len(v),
                            "bytes": sum(x.get("size") or 0 for x in v),
                            "files": [x["path"] for x in v]}
                        for g, v in sorted(by_gen.items())},
    }


def verify(dest: Path, verbose: bool = True) -> dict:
    """校验存档轨完整性：逐文件重算 SHA-256 与索引比对。"""
    idx = _load_index(dest)
    ok = miss = bad = 0
    problems: list[str] = []
    for f in idx.get("files", []):
        p = dest / f["path"]
        if not p.is_file():
            miss += 1
            problems.append(f"缺失: {f['path']}")
            continue
        actual = sha256_file(p)
        if actual != f.get("sha256"):
            bad += 1
            problems.append(f"指纹不符: {f['path']}")
        else:
            ok += 1
    if verbose:
        print(f"存档轨校验：正常 {ok} / 缺失 {miss} / 变更 {bad}")
        for x in problems[:20]:
            print("   ", x)
    return {"ok": ok, "missing": miss, "changed": bad, "problems": problems}


def main() -> int:
    ap = argparse.ArgumentParser(description="WeChat Vault 存档轨归档器（双轨制）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_in = sub.add_parser("ingest", help="接收原始包并按日期分代存档")
    p_in.add_argument("--source", required=True, help="投放目录（inbox/raw）")
    p_in.add_argument("--dest", required=True, help="raw-snapshots 目录")
    p_in.add_argument("--manifest", help="主 MANIFEST.json 路径（可选）")
    p_in.add_argument("--day", help="指定代次日期 YYYY-MM-DD（默认今天）")
    # 兼容 -v/-q（与 wv_archiver 的 CLI 保持一致，避免脚本混用时参数报错）
    p_in.add_argument("-v", "--verbose", action="store_true", default=True)
    p_in.add_argument("-q", "--quiet", action="store_true", help="静音")

    p_ls = sub.add_parser("list", help="列出存档代")
    p_ls.add_argument("--dest", required=True)

    p_vf = sub.add_parser("verify", help="校验存档完整性")
    p_vf.add_argument("--dest", required=True)

    args = ap.parse_args()

    if args.cmd == "ingest":
        src, dst = Path(args.source), Path(args.dest)
        if not src.is_dir():
            print(f"投放目录不存在: {src}", file=sys.stderr)
            return 1
        manifest = Path(args.manifest) if args.manifest else None
        st = ingest(src, dst, manifest, verbose=not getattr(args, "quiet", False),
                    day=args.day)
        return 0 if st["errors"] == 0 else 2

    if args.cmd == "list":
        info = list_generations(Path(args.dest))
        print(f"存档轨根目录: {info['root']}")
        print(f"共 {info['n_files']} 个文件 / {info['total_bytes']/1048576:.1f} MB")
        for g, v in info["generations"].items():
            print(f"  [{g}] {v['n_files']} 个文件 / {v['bytes']/1048576:.1f} MB")
            for name in v["files"][:20]:
                print(f"      {name}")
        return 0

    if args.cmd == "verify":
        r = verify(Path(args.dest))
        return 0 if (r["missing"] == 0 and r["changed"] == 0) else 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
