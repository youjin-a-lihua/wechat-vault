#!/usr/bin/env python3
"""从微信进程内存定位 SQLCipher 密钥。"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import struct
import sys
from pathlib import Path

log = logging.getLogger("wv_keymem")

# SQLCipher 4 的 HMAC key 派生固定 2 轮（不是口令 KDF 的 256000 轮）
MAC_KDF_ITER = 2
KEY_SZ = 32
SALT_SZ = 16
PAGE_SZ = 4096
RESERVE = 48

# 解密裁判在 archiver/wv_sync（与生产解密器同一份页模型实现）
_ARCHIVER = Path(__file__).resolve().parent.parent / "archiver"
if str(_ARCHIVER) not in sys.path:
    sys.path.insert(0, str(_ARCHIVER))
from wv_sync import verify_key_by_decrypt  # noqa: E402

def derive(enc_key: bytes, salt: bytes):
    """SQLCipher 4：enc_key 已是最终 AES key，不再派生；
    只需派生 HMAC 校验 key = PBKDF2(enc_key, salt^0x3A, 2 轮)。"""
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, MAC_KDF_ITER, dklen=KEY_SZ)
    return enc_key, mac

def verify_key(enc_key: bytes, db_path: Path) -> bool:
    """解密裁判：按 wv_sync.decrypt_one 的页模型解 page 0，
    验证明文是合法 SQLite 页。HMAC 猜谜已废弃——公式错一处就是假阴性。"""
    return verify_key_by_decrypt(db_path, enc_key.hex())

def readable_regions(pid: int):
    """列出进程可读的内存区间（跳过文件映射，只扫匿名/堆）。"""
    out = []
    try:
        with open("/proc/%d/maps" % pid) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2 or "r" not in parts[1]:
                    continue
                path = parts[5] if len(parts) > 5 else ""
                if path.endswith((".so", ".so.1")) or "/opt/wechat" in path:
                    continue
                a, b = parts[0].split("-")
                start, end = int(a, 16), int(b, 16)
                if end - start > 512 * 1024 * 1024:
                    continue
                out.append((start, end))
    except OSError:
        log.debug("maps unreadable")
        pass
    return out

def scan(pid: int, salt: bytes, db_path: Path, max_hits: int = 4000):
    """在内存里搜 salt，在其附近按 32 字节窗口尝试 enc_key。"""
    regions = readable_regions(pid)
    log.info("可扫描区间 %d 个", len(regions))
    hits = 0
    try:
        mem = open("/proc/%d/mem" % pid, "rb", buffering=0)
    except OSError as e:
        log.error("无法打开进程内存：%s（需要 root + CAP_SYS_PTRACE）", e)
        return None
    try:
        for start, end in regions:
            size = end - start
            try:
                mem.seek(start)
                data = mem.read(size)
            except (OSError, ValueError):
                log.debug("region unreadable, skip")
                continue
            if not data:
                continue
            off = 0
            while True:
                i = data.find(salt, off)
                if i < 0:
                    break
                off = i + 1
                hits += 1
                lo = max(0, i - 64)
                hi = min(len(data), i + SALT_SZ + 64)
                window = data[lo:hi]
                for w in range(0, max(1, len(window) - KEY_SZ)):
                    cand = window[w:w + KEY_SZ]
                    if len(cand) != KEY_SZ:
                        continue
                    if verify_key(cand, db_path):
                        log.info("命中！salt 命中 %d 次后找到有效 key", hits)
                        return cand
                if hits > max_hits:
                    log.warning("已尝试 %d 个 salt 命中点，未找到有效 key", hits)
                    return None
    finally:
        mem.close()
    log.warning("共 %d 个 salt 命中点，均未通过解密裁判", hits)
    return None

def find_pid(pattern: str = "opt/wechat/wechat") -> int:
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except OSError:
            log.debug("cmdline unreadable, skip")
            continue
        if pattern in cmd and "crashpad" not in cmd:
            return int(d.name)
    return 0

def main() -> int:
    ap = argparse.ArgumentParser(description="从微信进程内存定位 SQLCipher 密钥")
    ap.add_argument("--account-dir", required=True, help="<wxid>_<后缀> 目录")
    ap.add_argument("--db", default="message/message_0.db", help="用于校验的库（相对 db_storage）")
    ap.add_argument("--out", default="", help="输出 json（默认打印到 stdout）")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    acct = Path(a.account_dir)
    db = acct / "db_storage" / a.db
    if not db.is_file():
        log.error("找不到校验库：%s", db)
        return 2
    salt = db.read_bytes()[:SALT_SZ] if db.stat().st_size >= SALT_SZ else b""
    if not salt:
        log.error("读不到 salt")
        return 2
    log.info("salt = %s", salt.hex())

    pid = a.pid or find_pid()
    if not pid:
        log.error("找不到微信进程")
        return 2
    log.info("微信 pid = %d", pid)

    key = scan(pid, salt, db)
    if not key:
        return 1
    rec = {a.db: {"enc_key": key.hex(), "salt": salt.hex()}}
    text = json.dumps(rec, indent=2)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        log.info("已写入 %s", a.out)
    print(text)
    return 0

if __name__ == "__main__":
    sys.exit(main())
