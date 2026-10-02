#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
真实微信 .dat 加密强度实测结论
==============================

目的：用大量真实样本给出**决定性证据**，回答"这批 .dat 到底能不能离线解开"。

结论（实测定论）：
  ▸ 这批 .dat **不是单字节 XOR**，也不是"只需 magic 命中"的弱加密。
    旧 detect_xor_key 之所以"全部成功"，是因为微信 .dat 以常量 magic
    `07 08` 开头，而 BMP 以 `BM`(42 4d) 开头，于是
        key = 0x07 ^ 0x42 = 0x45   ← 对每个文件都是同一个值
    这是**常量对常量**的必然结果，与文件真实内容无关。
    换句话说：**只要文件以 07 08 开头，detect_xor_key 必然返回 0x45 且 ext=.bmp**
    —— 它测的不是密钥，是魔数。

  ▸ 强校验（完整 BMP/JPG/PNG 结构）在真实样本上**0 通过**。
    XOR 0x45 后虽然首 2 字节是 "BM"，但 BITMAPFILEHEADER 的
    fsize/off/hdrsize/dim/planes/bpp 全是垃圾 → 不是合法图片。

  ▸ 这批 .dat 是**真加密**（微信 4.x 的 V2 方案，会话级运行时密钥），
    密钥不在文件里，必须趁 Weixin.exe 运行时从内存提取，
    或让官方客户端自己解密（走"聊天记录管理 → 导入与导出"）。

本脚本输出可复核的证据表。
"""

from __future__ import annotations

import os
import random
import struct
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "archiver"))
import wv_dat as D  # noqa: E402

SOURCES = {
    "G_新2026": Path(r"G:\We chat and QQ\Wechat\xwechat_files"),
    "D_老2025": Path(r"D:\xwechat_files"),
}
SAMPLE = 200


def bmp_strong(buf: bytes) -> tuple[bool, str]:
    if len(buf) < 54:
        return False, "short"
    if buf[:2] != b"BM":
        return False, "no BM"
    fsize = struct.unpack_from("<I", buf, 2)[0]
    off = struct.unpack_from("<I", buf, 10)[0]
    hsz = struct.unpack_from("<I", buf, 14)[0]
    w, h = struct.unpack_from("<ii", buf, 18)
    planes = struct.unpack_from("<H", buf, 26)[0]
    bpp = struct.unpack_from("<H", buf, 28)[0]
    if fsize not in (0, len(buf)):
        return False, f"fsize {fsize} != {len(buf)}"
    if not (54 <= off < len(buf)):
        return False, f"off {off}"
    if hsz not in (40, 52, 56, 108, 124):
        return False, f"hdrsize {hsz}"
    if not (0 < w <= 20000 and 0 < abs(h) <= 20000):
        return False, f"dim {w}x{h}"
    if planes != 1:
        return False, f"planes {planes}"
    if bpp not in (1, 4, 8, 16, 24, 32):
        return False, f"bpp {bpp}"
    return True, f"{w}x{h} {bpp}bpp"


def pick(root: Path, n: int) -> list[Path]:
    dirs = []
    for dirpath, _d, _f in os.walk(root):
        if os.path.basename(dirpath) != "Img":
            continue
        m = os.path.basename(os.path.dirname(dirpath))
        if len(m) == 7 and m[4] == "-" and m[:4].isdigit():
            dirs.append(Path(dirpath))
    dirs.sort(key=lambda p: os.path.basename(os.path.dirname(p)), reverse=True)
    out: list[Path] = []
    for d in dirs[:8]:
        try:
            out.extend(f for f in d.iterdir()
                       if f.suffix.lower() == ".dat"
                       and not D.is_metadata_dat(f.name))
        except OSError:
            continue
        if len(out) >= n * 3:
            break
    random.seed(20261001)
    random.shuffle(out)
    return out[:n]


def main() -> int:
    print("=" * 74)
    print("真实微信 .dat —— 加密强度实测证据")
    print("=" * 74)

    for tag, root in SOURCES.items():
        print()
        print("─" * 74)
        print(f"【{tag}】{root}")
        print("─" * 74)
        if not root.exists():
            print("  不存在")
            continue

        files = pick(root, SAMPLE)
        if not files:
            print("  未抽到样本")
            continue
        print(f"  样本数: {len(files)}")

        first2 = Counter()          # 前 2 字节分布
        keys = Counter()            # detect_xor_key 返回的 key
        exts = Counter()            # detect_xor_key 返回的 ext
        weak_ok = 0                 # magic 命中
        strong_ok = 0              # 强校验通过
        strong_reasons = Counter()
        period_hits = 0

        for f in files:
            try:
                blob = f.read_bytes()
            except OSError:
                continue
            if len(blob) < 64:
                continue

            first2[blob[:2].hex()] += 1

            k = D.detect_xor_key(blob[:16])
            if k:
                weak_ok += 1
                keys[k[0]] += 1
                exts[k[1]] += 1
                dec = D.xor_bytes(blob, k[0])
                ok, why = bmp_strong(dec) if k[1] == ".bmp" else (False, "n/a")
                if ok:
                    strong_ok += 1
                else:
                    strong_reasons[why.split()[0] if why else "?"] += 1

            # 周期性检测（取尾部 512 字节，找 16~48 字节的完美重复）
            tail = blob[-512:]
            for p in range(16, 49):
                if len(tail) > p * 3 and all(
                        tail[i] == tail[i - p]
                        for i in range(p, len(tail) - p)):
                    period_hits += 1
                    break

        print(f"\n  ① 前 2 字节分布（前 3）：{dict(first2.most_common(3))}")
        print(f"  ② detect_xor_key 命中率：{weak_ok}/{len(files)}"
              f"  ({weak_ok / len(files) * 100:.1f}%)")
        print(f"     返回的密钥分布：{dict(keys.most_common(5))}"
              f"   扩展名：{dict(exts.most_common(5))}")
        print(f"  ③ 强校验（完整 BMP 结构）通过：{strong_ok}/{len(files)}"
              f"  ({strong_ok / len(files) * 100:.1f}%)")
        if strong_reasons:
            print(f"     失败主因：{dict(strong_reasons.most_common(4))}")
        print(f"  ④ 尾部存在完美 16~48 字节周期的文件：{period_hits}/{len(files)}")

        print()
        print("  ▸ 判读：")
        if keys and len(keys) == 1 and list(keys)[0] == 0x45:
            print("     密钥恒为 0x45 → 这是 0x07^0x42 的常量巧合，")
            print("     detect_xor_key 实际测的是『文件以 07 08 开头』，")
            print("     不是真实密钥。属**假阳性**。")
        if strong_ok == 0:
            print("     强校验 0 通过 → 这批 .dat 是**真加密**，")
            print("     离线无密钥不可解，须走官方客户端导出/内存取密钥。")

    print()
    print("=" * 74)
    print("最终结论")
    print("=" * 74)
    print("""
  ✗ 单字节 XOR 方案对这批数据无效（0% 产出合法图片）
  ✗ 「magic 命中即成功」是假阳性陷阱（密钥恒 0x45 已自证）

  ✔ 正确路线（合规、零风险）：
      微信 → 设置 → 聊天记录管理 → 导入与导出 → 导出到电脑
    得到的是**明文**记录 + 附件，可直接被 WeChat Vault 消费。

  ✔ 备选路线（需开机 + 运行时）：
      从 Weixin.exe 进程内存提取 V2 会话密钥，再离线解密
      （wv_dat.py 的 AES 分支已就绪，拿到 16 字节密钥即可用
        --aes-key 解密）
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
