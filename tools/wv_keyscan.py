#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wv_keyscan.py —— 微信 4.x 密钥定位（基于 salt 锚点）

思路切换：
  之前靠"96hex 串"猜格式，全落空。
  微信 4.x (WCDB) 的特性：**salt 是明文存在 page1 头 16 字节的**，
  而运行时 enc_key 会与 salt 一起出现在内存中（派生密钥的入参被缓存）。

  所以我们直接拿探测库的 salt（28b05d2f...）去内存里搜，
  找到 salt 出现的位置，然后把该位置**前后 ±256 字节**的原始字节取出来，
  从中提取候选 32 字节作为 enc_key —— 这是最贴近真实的做法。

另外补充：
  - 也搜 salt 的十六进制字符串形式（50 hex 字符）
  - 对每个锚点周围做多种偏移/长度切片
"""
from __future__ import annotations

import os
import ctypes, hashlib, hmac, json, re, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wv_winkey as W

DB_DIR = Path(os.environ.get("WV_WX_DB_STORAGE", r"C:\path\to\xwechat_files"))
          # 设 WV_WX_DB_STORAGE 指向你的 db_storage 目录
OUT_DIR = Path(__file__).resolve().parent.parent / "local_keys"


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: wv_keyscan.py <pid> [库相对路径]")
        return 2
    pid = int(sys.argv[1])
    rel = sys.argv[2] if len(sys.argv) > 2 else "session/session.db"
    probe = DB_DIR / rel
    with open(probe, "rb") as f:
        page1 = f.read(4096)
    salt = page1[:16]
    salt_hex = salt.hex()
    print(f"[*] PID {pid}  探测库 {rel}")
    print(f"[*] salt(bytes) = {salt_hex}")

    h = W.k32.OpenProcess(0x0410, False, pid)
    if not h:
        print(f"[!] OpenProcess 失败 err={ctypes.get_last_error()}")
        return 1
    regions = W.iter_readable_regions(h)
    print(f"[*] 内存区域 {len(regions)} 块")
    W.k32.CloseHandle(h)

    # 两种锚点：raw salt bytes / salt 的 hex 字符串
    anchors = {
        "raw": salt,
        "hex": salt_hex.encode(),
    }

    # 收集候选 enc_key（纯 bytes，32 字节）
    candidates: set[bytes] = set()
    ctx_notes: list[str] = []

    h = W.k32.OpenProcess(0x0410, False, pid)
    n_hit = 0
    for base, size in regions:
        buf = W.read_region(h, base, size)
        if not buf:
            continue
        for aname, pat in anchors.items():
            start = 0
            while True:
                idx = buf.find(pat, start)
                if idx < 0:
                    break
                n_hit += 1
                # 取锚点周围 ±256 字节
                lo = max(0, idx - 256)
                hi = min(len(buf), idx + len(pat) + 256)
                ctx = buf[lo:hi]
                if n_hit <= 5:
                    ctx_notes.append(f"{aname}@{base + idx:#x}: "
                                     f"{ctx[:64].hex()}")
                # 从上下文中穷举 32 字节窗口作为 enc_key 候选
                for off in range(0, max(1, len(ctx) - 32 + 1)):
                    candidates.add(ctx[off:off + 32])
                start = idx + 1
    W.k32.CloseHandle(h)
    print(f"[*] salt 锚点命中 {n_hit} 处，候选 32B 密钥 {len(candidates)} 个")

    for n in ctx_notes:
        print("    上下文:", n)

    # 校验：标准 SQLCipher4 公式
    stored = page1[4096 - 48:4096 - 48 + 64]
    data = page1[16:4096 - 48]
    stored2 = page1[4096 - 64:4096]
    data2 = page1[16:4096 - 64 + 16]

    t0 = time.time()
    tried = 0
    hits: list[tuple[bytes, str]] = []
    for enc in candidates:
        tried += 1
        mk = hashlib.pbkdf2_hmac("sha512", enc, salt, 256000, dklen=32)
        mac = hmac.new(mk, data, hashlib.sha512).digest()
        if hmac.compare_digest(mac, stored) or \
           hmac.compare_digest(mac, bytes(a ^ b for a, b in zip(stored, mk))):
            hits.append((enc, "A"))
            print(f"  [HIT-A] enc_key = {enc.hex()}")
            continue
        mac2 = hmac.new(mk, data2, hashlib.sha512).digest()
        if hmac.compare_digest(mac2, stored2) or \
           hmac.compare_digest(mac2, bytes(a ^ b for a, b in zip(stored2, mk))):
            hits.append((enc, "B"))
            print(f"  [HIT-B] enc_key = {enc.hex()}")
        if tried % 2000 == 0:
            print(f"    …已试 {tried}/{len(candidates)}（{time.time()-t0:.0f}s）")

    print(f"\n[*] 试了 {tried} 个候选，耗时 {time.time()-t0:.1f}s，命中 {len(hits)}")
    if hits:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "enc_keys.json").write_text(json.dumps({
            "pid": pid, "db": rel, "salt": salt_hex,
            "enc_keys": [{"key": e.hex(), "variant": v} for e, v in hits],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[✓] 保存 {OUT_DIR / 'enc_keys.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
