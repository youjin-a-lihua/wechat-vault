#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
解码产物真伪校验 —— 拿真实微信 .dat 验证 XOR 密钥是否正确
========================================================

背景：仅靠"前 3 字节等于 BM"就判为 BMP 是不够的 ——
BMP 头还有位深/尺寸/偏移等字段，可以用它们做**强校验**，
从而把"偶然撞上 BM 的密钥"剔除掉。

本脚本：
  1. 本机抽样真实 .dat
  2. 对每个文件跑 decode_dat
  3. 若判为 .bmp，则进一步用 PIL 或自写 BMP 头解析校验
  4. 同时对全部 256 个候选 XOR 密钥做穷举，找出"能过强校验"的密钥集
  5. 报告：真密钥是什么、有多少个密钥能骗过弱校验、强校验能挡掉多少
"""

from __future__ import annotations

import os
import struct
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "archiver"))
import wv_dat as D  # noqa: E402

SOURCES = {
    "new2026": Path(r"G:\We chat and QQ\Wechat\xwechat_files"),
    "old2025": Path(r"D:\xwechat_files"),
}
SAMPLE = 60


def bmp_header_ok(buf: bytes) -> tuple[bool, str]:
    """
    BMP 头强校验。

    合法 BMP 前 14 字节 = BITMAPFILEHEADER：
       0-1  'BM'
       2-5  文件大小（通常 == 实际长度，或 0）
       6-9  保留（通常 0）
      10-13 像素数据偏移（>= 54 且 < 文件长度）
    紧接着 40 字节 BITMAPINFOHEADER：
      14-17 头大小（通常 40 / 108 / 124）
      18-21 宽（> 0 且 <= 100000）
      22-25 高（> 0 且 <= 100000）
      26-27 平面数（必须为 1）
      28-29 位深（1/4/8/16/24/32）
    """
    if len(buf) < 54:
        return False, "too short"
    if buf[:2] != b"BM":
        return False, "no BM"
    fsize = struct.unpack_from("<I", buf, 2)[0]
    if fsize not in (0, len(buf)):
        return False, f"size mismatch {fsize}!={len(buf)}"
    off = struct.unpack_from("<I", buf, 10)[0]
    if not (54 <= off < len(buf)):
        return False, f"bad off {off}"
    hsz = struct.unpack_from("<I", buf, 14)[0]
    if hsz not in (40, 52, 56, 108, 124):
        return False, f"bad hdrsize {hsz}"
    w, h = struct.unpack_from("<ii", buf, 18)
    if not (0 < w <= 100000 and 0 < abs(h) <= 100000):
        return False, f"bad dim {w}x{h}"
    planes = struct.unpack_from("<H", buf, 26)[0]
    if planes != 1:
        return False, f"bad planes {planes}"
    bpp = struct.unpack_from("<H", buf, 28)[0]
    if bpp not in (1, 4, 8, 16, 24, 32):
        return False, f"bad bpp {bpp}"
    return True, f"{w}x{h} {bpp}bpp off={off}"


def jpg_header_ok(buf: bytes) -> tuple[bool, str]:
    if len(buf) < 4 or buf[:2] != b"\xff\xd8":
        return False, "no SOI"
    if buf[-2:] != b"\xff\xd9":
        return False, "no EOI"
    return True, "jpg ok"


def png_header_ok(buf: bytes) -> tuple[bool, str]:
    if not buf.startswith(b"\x89PNG\r\n\x1a\n"):
        return False, "no sig"
    if b"IHDR" not in buf[:32]:
        return False, "no IHDR"
    if not buf.rstrip(b"\x00").endswith(b"IEND\xaeB`\x82"):
        return False, "no IEND"
    return True, "png ok"


STRONG = {".bmp": bmp_header_ok, ".jpg": jpg_header_ok, ".png": png_header_ok}


def strong_check(ext: str, buf: bytes) -> tuple[bool, str]:
    fn = STRONG.get(ext)
    if fn is None:
        return True, "no strong check"
    return fn(buf)


def pick(root: Path, n: int) -> list[Path]:
    dirs = []
    for dirpath, _d, _f in os.walk(root):
        if os.path.basename(dirpath) != "Img":
            continue
        month = os.path.basename(os.path.dirname(dirpath))
        if len(month) == 7 and month[4] == "-" and month[:4].isdigit():
            dirs.append(Path(dirpath))
    dirs.sort(key=lambda p: os.path.basename(os.path.dirname(p)), reverse=True)
    out: list[Path] = []
    for d in dirs[:5]:
        try:
            fs = sorted(f for f in d.iterdir()
                        if f.suffix.lower() == ".dat"
                        and not D.is_metadata_dat(f.name))
        except OSError:
            continue
        out.extend(fs[:max(1, n // 5)])
    return out[:n]


def main() -> int:
    grand = Counter()
    keys_real = Counter()
    weak_only = 0

    for tag, root in SOURCES.items():
        print("=" * 70)
        print(f"【{tag}】{root}")
        print("=" * 70)
        if not root.exists():
            print("  不存在，跳过")
            continue

        files = pick(root, SAMPLE)
        print(f"  抽样 {len(files)} 个真实 .dat\n")

        for f in files:
            try:
                blob = f.read_bytes()
            except OSError:
                grand["read_err"] += 1
                continue

            r = D.decode_dat(blob)
            if not r["ok"]:
                grand["decode_fail"] += 1
                continue

            ext = r["ext"]
            ok, why = strong_check(ext, r["data"])
            grand["decode_ok"] += 1
            if ok:
                grand["strong_ok"] += 1
                keys_real[r["detail"]] += 1
            else:
                grand["strong_fail"] += 1
                if weak_only < 6:
                    weak_only += 1
                    print(f"  [弱校验过/强校验拒] {f.name[:44]}")
                    print(f"      detail={r['detail']} ext={ext} why={why}")

            # 穷举 256 个密钥，统计有多少能过"弱校验"（仅比 magic）
            if len(files) <= 60 and ext == ".bmp":
                pass

        # 专门做一次密钥穷举统计（取前 20 个文件）
        weak_pass = Counter()
        strong_pass = Counter()
        for f in files[:20]:
            try:
                blob = f.read_bytes()
            except OSError:
                continue
            for k in range(1, 256):
                dec = D.xor_bytes(blob, k)
                ident = D.identify(dec)
                if not ident:
                    continue
                weak_pass[k] += 1
                sok, _w = strong_check(ident[0], dec)
                if sok:
                    strong_pass[k] += 1

        print(f"\n  ▸ 密钥穷举（{min(20, len(files))} 个文件）：")
        print(f"      过弱校验的密钥数：{len(weak_pass)}   "
              f"（即只看 magic 会有这么多假阳性）")
        print(f"      过强校验的密钥数：{len(strong_pass)}  "
              f"→ {dict(strong_pass.most_common(5))}")

    print()
    print("=" * 70)
    print("汇总")
    print("=" * 70)
    for k, v in grand.most_common():
        print(f"  {k:<16} {v}")
    print(f"\n  真实密钥分布（强校验通过）：{dict(keys_real.most_common(6))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
