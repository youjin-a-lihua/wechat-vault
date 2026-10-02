#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新旧真实微信数据兼容性测试
==========================

用户指令：「D盘里的好像是老数据 … G盘是最新的 … 先做测试吧看看新老的数据兼容性如何」

本脚本对两代真实数据做**只读**抽样实测：

  样本组 A（老）：D:\\xwechat_files\\       —— 2025-08 iPhone 时代落盘
  样本组 B（新）：G:\\…\\xwechat_files\\   —— 2026-09 Windows 微信 4.x 落盘

测什么：
  1. .dat 附件解码成功率（分格式统计：plain / xor / v1 / v2 / tmp / fail）
  2. 官方备份包格式识别（老 Backup.db+BAK_* vs 新 Backup/<wxid>/<hex>/…）
  3. 消息索引（ChatPackage / Index）是否可读
  4. 两代数据的字段结构差异

产出：控制台报告 + compat_real_report.json
"""

from __future__ import annotations

import json
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "archiver"))
import wv_dat  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

GROUPS = {
    "A_old": Path(r"D:\xwechat_files"),
    "B_new": Path(r"G:\We chat and QQ\Wechat\xwechat_files"),
}

SAMPLE_PER_GROUP = 400
MAX_WALK_FILES = 400000


def sample_dat_files(root: Path, n: int) -> list[Path]:
    """在 attach 目录下抽样 .dat 文件（只读）。"""
    cands: list[Path] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # 只关心附件目录，跳过备份包（里面是加密流，不叫 .dat）
        for fn in filenames:
            if fn.lower().endswith(".dat"):
                cands.append(Path(dirpath) / fn)
                if len(cands) >= MAX_WALK_FILES:
                    break
            scanned += 1
        if len(cands) >= MAX_WALK_FILES:
            break
    if not cands:
        return []
    random.seed(20261001)
    return random.sample(cands, min(n, len(cands)))


def test_group(name: str, root: Path) -> dict:
    print("=" * 72)
    print(f"【{name}】 {root}")
    print("=" * 72)

    out: dict = {"root": str(root), "exists": root.exists()}

    if not root.exists():
        print("  ⚠ 目录不存在，跳过")
        out["skipped"] = True
        return out

    # ---------- 1. .dat 解码 ----------
    print("\n── 1) .dat 附件解码 ──")
    files = sample_dat_files(root, SAMPLE_PER_GROUP)
    print(f"  抽样 {len(files)} 个 .dat（只读）")

    fmt_counter: Counter = Counter()
    ext_counter: Counter = Counter()
    err_counter: Counter = Counter()
    key_counter: Counter = Counter()
    size_by_fmt: dict[str, list[int]] = defaultdict(list)

    for i, p in enumerate(files, 1):
        try:
            blob = p.read_bytes()
        except OSError as e:
            err_counter[f"read:{type(e).__name__}"] += 1
            continue
        try:
            r = wv_dat.decode_dat(blob)
        except Exception as e:  # 绝不该再发生，发生即回归
            err_counter[f"decode:{type(e).__name__}:{e}"] += 1
            continue

        if r["ok"]:
            fmt_counter[r["fmt"]] += 1
            ext_counter[r["ext"]] += 1
            size_by_fmt[r["fmt"]].append(len(blob))
            if r["fmt"] == "xor":
                k = r["detail"].split("0x")[-1].rstrip("）")
                key_counter[k] += 1
        else:
            err_counter[r["detail"][:48]] += 1
        if i % 100 == 0:
            print(f"    … {i}/{len(files)}")

    ok = sum(fmt_counter.values())
    total = ok + sum(v for k, v in err_counter.items() if not k.startswith("read:"))

    print(f"\n  ▸ 解码成功 {ok} / {total}  "
          f"({ok / total * 100:.1f}%)" if total else "  ▸ 无样本")
    for k, v in fmt_counter.most_common():
        avg = sum(size_by_fmt[k]) / len(size_by_fmt[k]) / 1024
        print(f"      {k:<6} {v:>4} 个   平均 {avg:.0f} KB")
    print("  ▸ 扩展名分布：", dict(ext_counter.most_common(8)))
    if key_counter:
        print("  ▸ XOR 密钥分布：", dict(key_counter.most_common(6)))
    if err_counter:
        print("  ▸ 失败原因：")
        for k, v in err_counter.most_common(8):
            print(f"      [{v}] {k}")

    out["dat"] = {
        "sampled": len(files),
        "ok": ok,
        "total": total,
        "ok_rate": round(ok / total, 4) if total else 0,
        "formats": dict(fmt_counter),
        "exts": dict(ext_counter),
        "xor_keys": dict(key_counter),
        "errors": dict(err_counter),
    }

    # ---------- 2. 备份包结构 ----------
    print("\n── 2) 备份包结构识别 ──")
    bk_new = list(root.glob("*/Backup/*/*")) + list(root.glob("Backup/*/*"))
    bk_old = list(root.rglob("Backup.db")) + list(root.rglob("BAK_0_TEXT"))
    bk_magic = Counter()
    for f in (list(root.rglob("backup.attr"))[:5]
              + list(root.rglob("roam_device_info.dat"))[:5]):
        try:
            bk_magic[f.read_bytes()[:4].decode("latin1")] += 1
        except OSError:
            pass

    print(f"  新格式目录候选（Backup/<id>/<hex>/）：{len(bk_new)}")
    print(f"  老格式文件（Backup.db / BAK_0_TEXT）：{len(bk_old)}")
    if bk_magic:
        print(f"  backup.attr magic：{dict(bk_magic)}")
        if all(m == "RMFH" for m in bk_magic):
            print("  ✔ 确认为微信官方加密备份包（RMFH），离线不可解析")

    out["backup"] = {
        "new_style_dirs": len(bk_new),
        "old_style_files": len(bk_old),
        "magic": dict(bk_magic),
    }

    # ---------- 3. 消息主体数据 ----------
    print("\n── 3) 消息主体/索引可读性 ──")
    dbs = [p for p in root.rglob("*.db")][:200]
    sqlite_ok = 0
    for p in dbs:
        try:
            with p.open("rb") as fh:
                if fh.read(16) == b"SQLite format 3\x00":
                    sqlite_ok += 1
        except OSError:
            pass
    pkgs = list(root.rglob("ChatPackage"))[:5]

    print(f"  *.db 总数（抽样上限 200）：{len(dbs)}，其中真 SQLite：{sqlite_ok}")
    print(f"  ChatPackage 数量（抽样上限 5）：{len(pkgs)}")

    out["messages"] = {"db_files": len(dbs), "db_sqlite": sqlite_ok,
                       "chatpackage": len(pkgs)}

    # ---------- 4. 磁盘占用 ----------
    total_bytes = 0
    dat_bytes = 0
    cnt = 0
    for dirpath, _d, filenames in os.walk(root):
        for fn in filenames:
            try:
                sz = (Path(dirpath) / fn).stat().st_size
            except OSError:
                continue
            total_bytes += sz
            cnt += 1
            if fn.lower().endswith(".dat"):
                dat_bytes += sz
    print(f"\n── 4) 占用 ──  {total_bytes/1024**3:.2f} GB / {cnt:,} 个文件"
          f"（.dat 占 {dat_bytes/1024**3:.2f} GB）")
    out["disk"] = {"bytes": total_bytes, "files": cnt, "dat_bytes": dat_bytes}

    return out


def main() -> int:
    report = {"generated": __import__("datetime").datetime.now().isoformat()}
    for name, root in GROUPS.items():
        report[name] = test_group(name, root)
        print()

    # ---------- 汇总 ----------
    print("=" * 72)
    print("兼容性结论")
    print("=" * 72)
    a = report.get("A_old", {}).get("dat", {})
    b = report.get("B_new", {}).get("dat", {})
    rows = [
        ("样本组", "老数据 D:", "新数据 G:"),
        ("抽样数", a.get("sampled"), b.get("sampled")),
        ("解码成功", a.get("ok"), b.get("ok")),
        ("成功率", f"{a.get('ok_rate', 0)*100:.1f}%", f"{b.get('ok_rate', 0)*100:.1f}%"),
        ("格式分布", str(a.get("formats")), str(b.get("formats"))),
    ]
    w = max(len(str(r[0])) for r in rows)
    for r in rows:
        print(f"  {str(r[0]):<{w}}  {str(r[1]):<28}  {str(r[2])}")

    outp = ROOT / "tests" / "compat_real_report.json"
    outp.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"\n报告已写入：{outp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
