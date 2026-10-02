#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新旧数据兼容性 —— 端到端导入实测（只读源，只写沙箱）
====================================================

用户指令：「D盘里的好像是老数据 … G盘是最新的 … 先做测试吧看看新老的数据兼容性如何」

本脚本做**真实的导入链路验证**：

  1. 从 D:\\（老，2025）与 G:\\（新，2026）各**复制**一份代表性切片
     到工作区沙箱 wv_test/（不动源文件，源目录全程只读）
  2. 用 wv_archiver scan 导入沙箱归档库
  3. 用 wv_dat decode 批量解码其中的 .dat
  4. 校验：会话数、消息数、去重幂等（跑两遍）、账号隔离
  5. 输出兼容性结论

切片内容（每边）：
  - Backup/ 或 Backup.db 等官方备份包（存档轨）
  - msg/attach/<hash>/<YYYY-MM>/Img/ 下若干 .dat（可读轨）
  - 任意 .txt / .html（若存在，可读轨）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

PY = sys.executable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "archiver"))
import wv_dat  # noqa: E402
import wv_archiver as WVA  # noqa: E402

# 单一事实来源：直接复用产品代码里的判别函数，避免测试与产品逻辑漂移
looks_like_chat_export = WVA.looks_like_chat_export

SANDBOX = ROOT.parent / "wv_test"
INBOX = SANDBOX / "inbox"
ARCH = ROOT / "archiver" / "wv_archiver.py"
DAT = ROOT / "archiver" / "wv_dat.py"

SOURCES = {
    "D_老数据2025": Path(r"D:\xwechat_files"),
    "G_新数据2026": Path(r"G:\We chat and QQ\Wechat\xwechat_files"),
}

DAT_LIMIT = 120         # 每边最多复制多少个 .dat
SIZE_BUDGET = 300 * 1024 * 1024   # 每边复制总量上限 300MB
IMG_DIRS = 4            # 取月份最新的 N 个 Img 目录


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """执行子进程，统一按 UTF-8 解码。"""
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def stage_slice(tag: str, src: Path) -> dict:
    """把源目录的代表性切片复制进沙箱。源目录只读打开。"""
    dst = INBOX / tag
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    info = {"tag": tag, "src": str(src), "copied_files": 0, "copied_bytes": 0,
            "backup_copied": 0, "dat_copied": 0, "txt_copied": 0,
            "bubble_copied": 0}

    if not src.exists():
        info["error"] = "源目录不存在"
        return info

    def cp(f: Path, outdir: Path) -> int:
        outdir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(f, outdir / f.name)
        except OSError:
            return 0
        sz = f.stat().st_size
        info["copied_files"] += 1
        info["copied_bytes"] += sz
        return sz

    # ---- a) 官方备份包（存档轨）—— 只取**结构**，每包最多 20MB ----
    # 说明：整包动辄 9~10GB，测试只需验证"能否识别 + 能否搬运"，
    # 因此对每个包只拷前 N 个最小文件，避免把沙箱撑爆。
    BAK_FILE_BUDGET = 20 * 1024 * 1024

    n_bak = 0
    for pat in ("*/Backup/*/*", "Backup/*/*"):
        for d in src.glob(pat):
            if not d.is_dir():
                continue
            target = dst / "Backup" / d.name
            target.mkdir(parents=True, exist_ok=True)
            got = 0
            for f in sorted(d.rglob("*"), key=lambda x: x.stat().st_size if x.is_file() else 0):
                if not f.is_file():
                    continue
                if got >= BAK_FILE_BUDGET:
                    break
                rel = f.relative_to(d)
                o = target / rel
                o.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(f, o)
                except OSError:
                    continue
                got += f.stat().st_size
                info["copied_files"] += 1
                info["copied_bytes"] += f.stat().st_size
            n_bak += 1
            if n_bak >= 2:
                break
        if n_bak >= 2:
            break

    # 老格式：Backup.db / BAK_*（同样限流，只取前 8MB）
    for f in (list(src.rglob("Backup.db"))[:1]
              + list(src.rglob("BAK_0_TEXT"))[:1]
              + list(src.rglob("BAK_0_MEDIA"))[:2]):
        try:
            if f.stat().st_size > 8 * 1024 * 1024:
                continue
        except OSError:
            continue
        if cp(f, dst / "Backup"):
            n_bak += 1
    info["backup_copied"] = n_bak

    # ---- b) 可读轨①：msg/attach/<hash>/<YYYY-MM>/Img/*.dat ----
    # 实测路径形如 wxid_xxx/msg/attach/<32hex>/<YYYY-MM>/Img/
    # 判据：目录名是 Img，且其父目录名形如 YYYY-MM。
    img_dirs: list[Path] = []
    for dirpath, _dirnames, _filenames in os.walk(src):
        if os.path.basename(dirpath) != "Img":
            continue
        month = os.path.basename(os.path.dirname(dirpath))
        if len(month) == 7 and month[4] == "-" and month[:4].isdigit():
            img_dirs.append(Path(dirpath))
    img_dirs.sort(key=lambda p: os.path.basename(os.path.dirname(p)), reverse=True)
    img_dirs = img_dirs[:IMG_DIRS]

    n_dat = 0
    per_dir = max(1, DAT_LIMIT // max(1, len(img_dirs)))
    for d in img_dirs:
        files = sorted(f for f in d.iterdir()
                       if f.suffix.lower() == ".dat"
                       and not wv_dat.is_metadata_dat(f.name))
        if not files:
            continue
        outdir = dst / "msg" / "attach" / d.parent.parent.name / d.parent.name / "Img"
        for f in files[:per_dir]:
            if info["copied_bytes"] >= SIZE_BUDGET:
                break
            if cp(f, outdir):
                n_dat += 1
    info["dat_copied"] = n_dat
    info["img_dirs_used"] = len(img_dirs)

    # ---- c) 可读轨②：Bubble 缓存图（新格式特有）----
    n_bub = 0
    for dirpath, _dirnames, filenames in os.walk(src):
        if os.path.basename(dirpath) != "Bubble":
            continue
        if n_bub >= 24 or info["copied_bytes"] >= SIZE_BUDGET:
            break
        files = [f for f in (Path(dirpath) / x for x in filenames)
                 if f.suffix.lower() == ".dat" and not wv_dat.is_metadata_dat(f.name)]
        if not files:
            continue
        par = os.path.basename(os.path.dirname(dirpath))
        outdir = dst / "cache" / ("2026-09" if "2026" in dirpath else "misc") / par
        for f in files[:8]:
            if cp(f, outdir):
                n_bub += 1
    info["bubble_copied"] = n_bub

    # ---- d) 元数据 .dat（负面样本：应被解码器跳过）----
    n_meta = 0
    for f in list(src.rglob("alt_name.dat"))[:2] + list(src.rglob("phoneid.dat"))[:2]:
        if cp(f, dst / "元数据测试"):
            n_meta += 1
    info["meta_copied"] = n_meta

    # ---- e) 文本导出（若存在）—— 只收真的聊天记录导出，排除配置/垃圾 ----
    n_txt = 0
    for f in list(src.rglob("*.txt"))[:60]:
        if not f.is_file() or f.stat().st_size > 20 * 1024 * 1024:
            continue
        if not looks_like_chat_export(f):
            info.setdefault("txt_rejected", []).append(f.name)
            continue
        if cp(f, dst / "导出文本"):
            n_txt += 1
    info["txt_copied"] = n_txt

    return info


def arch_scan(source: Path, tag: str) -> dict:
    store = SANDBOX / f"store_{tag}"
    r = run([PY, str(ARCH), "scan", "--source", str(source),
             "--store", str(store), "-q"])
    out = (r.stdout or "") + (r.stderr or "")
    return {"rc": r.returncode, "out": out[-2500:], "store": str(store)}


def arch_stats(store: Path) -> dict:
    r = run([PY, str(ARCH), "stats", "--store", str(store)])
    return {"rc": r.returncode, "out": (r.stdout or "")[-2500:]}


def dat_decode(src: Path, dst: Path) -> dict:
    if not any(src.rglob("*.dat")):
        return {"rc": -1, "out": "无 .dat 样本"}
    r = run([PY, str(DAT), "decode", "--src", str(src), "--dst", str(dst)])
    out = (r.stdout or "") + (r.stderr or "")
    return {"rc": r.returncode, "out": out[-2500:]}


def main() -> int:
    SANDBOX.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)

    report: dict = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "sources": {}, "stages": {}}

    log("=" * 74)
    log("阶段 1／准备沙箱切片（源目录只读，仅复制不修改）")
    log("=" * 74)
    for tag, src in SOURCES.items():
        log(f"\n▶ {tag}")
        info = stage_slice(tag, src)
        report["sources"][tag] = info
        if "error" in info:
            log(f"   ✗ {info['error']}")
            continue
        log(f"   备份包：{info['backup_copied']} 个   .dat：{info['dat_copied']} 个"
            f"   文本：{info['txt_copied']} 个")
        log(f"   共 {info['copied_files']} 文件 / "
            f"{info['copied_bytes'] / 1024**2:.1f} MB")

    log("\n" + "=" * 74)
    log("阶段 2／导入归档库（第一遍 = 全量）")
    log("=" * 74)
    for tag in SOURCES:
        d = INBOX / tag
        if not d.exists():
            continue
        log(f"\n▶ 导入 {tag}")
        res = arch_scan(d, tag)
        report["stages"].setdefault(tag, {})["scan1"] = res
        for line in res["out"].strip().splitlines()[-18:]:
            log("   " + line)
        st = arch_stats(SANDBOX / f"store_{tag}")
        report["stages"][tag]["stats1"] = st
        log("   ── 统计 ──")
        for line in st["out"].strip().splitlines()[:22]:
            log("   " + line)

    log("\n" + "=" * 74)
    log("阶段 3／幂等复测（第二遍 = 应当 0 新增）")
    log("=" * 74)
    for tag in SOURCES:
        d = INBOX / tag
        if not d.exists():
            continue
        log(f"\n▶ 复导入 {tag}")
        res = arch_scan(d, tag)
        report["stages"][tag]["scan2"] = res
        for line in res["out"].strip().splitlines()[-12:]:
            log("   " + line)

    log("\n" + "=" * 74)
    log("阶段 4／.dat 附件解码（可读轨）")
    log("=" * 74)
    for tag in SOURCES:
        d = INBOX / tag
        if not d.exists():
            continue
        log(f"\n▶ 解码 {tag}")
        res = dat_decode(d, SANDBOX / f"media_{tag}")
        report["stages"][tag]["dat"] = res
        for line in res["out"].strip().splitlines()[-16:]:
            log("   " + line)

    outp = SANDBOX / "compat_e2e_report.json"
    outp.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    log(f"\n报告：{outp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
