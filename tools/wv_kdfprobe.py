#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wv_kdfprobe.py —— 单进程高效密钥校验探针

优化核心：
  PBKDF2 慢（256000 轮）。但 SQLCipher4 中
      aes_key = PBKDF2(enc_key, salt ^ 0x3a, iter)
      mac_key = PBKDF2(enc_key, salt,       iter)
  只有 mac_key 用于 page1 校验。所以每个候选**只需 1 次 PBKDF2**（不是2次）。

  用 hashlib.pbkdf2_hmac 的 C 实现，单次约 100ms，478 个候选 ≈ 50s。
"""
from __future__ import annotations

import os
import ctypes, ctypes.wintypes as wt, hashlib, hmac, json, re, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wv_winkey as W
import logging

log = logging.getLogger(__name__)

DB_DIR = Path(os.environ.get("WV_WX_DB_STORAGE", r"C:\path\to\xwechat_files"))
          # 设 WV_WX_DB_STORAGE 指向你的 db_storage 目录
PROBE_DB = DB_DIR / "session" / "session.db"
OUT_DIR = Path(__file__).resolve().parent.parent / "local_keys"


def scan_96hex(pid: int) -> set[str]:
    h = W.k32.OpenProcess(0x0410, False, pid)
    if not h:
        print(f"[!] OpenProcess({pid}) 失败, err={ctypes.get_last_error()}")
        return set()
    regions = W.iter_readable_regions(h)
    cands: set[str] = set()
    pat = re.compile(rb"(?<![0-9a-fA-F])([0-9a-fA-F]{96})(?![0-9a-fA-F])")
    for base, size in regions:
        buf = W.read_region(h, base, size)
        if buf:
            for m in pat.finditer(buf):
                cands.add(m.group(1).decode().lower())
    W.k32.CloseHandle(h)
    return cands


def main() -> int:
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else 31552
    print(f"[*] 目标 PID {pid}")
    print(f"[*] 探测库 {PROBE_DB}")
    if not PROBE_DB.exists():
        print("[!] 探测库不存在")
        return 2

    with open(PROBE_DB, "rb") as f:
        page1 = f.read(4096)
    salt = page1[:16]
    print(f"[*] salt = {salt.hex()}")

    cands = scan_96hex(pid)
    print(f"[*] 候选 96hex 串 {len(cands)} 个")

    stored_a = page1[4096 - 48:4096 - 48 + 64]   # 变体A 位置
    stored_b = page1[4096 - 64:4096]             # 变体B 位置
    data_a = page1[16:4096 - 48]
    data_c = page1[16:4096 - 64 + 16]

    t0 = time.time()
    hits: list[tuple[str, str, str]] = []
    tried = 0
    for c in cands:
        for kp, label in ((c[:64], "前64"), (c[32:], "后64")):
            try:
                enc = bytes.fromhex(kp)
            except Exception as e:
                log.debug("候选非合法十六进制，跳过：%s", e)
                continue
            tried += 1
            # 只需 1 次 PBKDF2 得到 mac_key
            mk = hashlib.pbkdf2_hmac("sha512", enc, salt, 256000, dklen=32)
            mac_a = hmac.new(mk, data_a, hashlib.sha512).digest()
            mac_c = hmac.new(mk, data_c, hashlib.sha512).digest()
            tests = {
                "A_raw": (mac_a, stored_a),
                "A_xor": (mac_a, bytes(a ^ b for a, b in zip(stored_a, mk))),
                "B_raw": (mac_a, stored_b),
                "B_xor": (mac_a, bytes(a ^ b for a, b in zip(stored_b, mk))),
                "C_raw": (mac_c, stored_b),
                "C_xor": (mac_c, bytes(a ^ b for a, b in zip(stored_b, mk))),
            }
            for name, (calc, want) in tests.items():
                if hmac.compare_digest(calc, want):
                    hits.append((c, label, name))
                    print(f"  [HIT] {label} 变体{name}: {c}")
        if tried % 200 == 0 and tried:
            print(f"    …已试 {tried} 个（{time.time() - t0:.0f}s）")

    print(f"\n[*] 共试 {tried} 个密钥，耗时 {time.time() - t0:.1f}s，命中 {len(hits)} 个")
    if hits:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": pid,
            "probe_db": str(PROBE_DB),
            "salt": salt.hex(),
            "hits": [{"hex": h, "part": p, "variant": v} for h, p, v in hits],
        }
        (OUT_DIR / "kdf_hits.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[✓] 已保存 {OUT_DIR / 'kdf_hits.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
