#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wv_keymem.py —— 从运行中的微信进程内存里定位 SQLCipher 密钥（Linux）

原理：
    WCDB 打开数据库时会把 salt 与 enc_key 一起缓存在进程内存里。
    salt 是明文存在每个库文件的前 16 字节，所以拿 salt 当锚点去内存里搜，
    命中位置附近通常就能捞到 32 字节的 enc_key。

    比 gdb 下断点可靠：不需要精确时序，微信登录后内存里一直有。

用法：
    python wv_keymem.py --account-dir /config/xwechat_files/<wxid>_xxx --out /tmp/keys.json
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import struct
import sys
from pathlib import Path

log = logging.getLogger("wv_keymem")

KDF_ITER = 256000
KEY_SZ = 32
SALT_SZ = 16
PAGE_SZ = 4096
RESERVE = 48


def derive(enc_key: bytes, salt: bytes):
    mac_salt = bytes(b ^ 0x3A for b in salt)
    aes = hashlib.pbkdf2_hmac("sha512", enc_key, salt, KDF_ITER, dklen=KEY_SZ)
    mac = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, KDF_ITER, dklen=KEY_SZ)
    return aes, mac


def verify_key(enc_key: bytes, db_path: Path) -> bool:
    """用 page1 末尾的 HMAC 校验候选 key 是否真的是这个库的密钥。"""
    try:
        with open(db_path, "rb") as f:
            page1 = f.read(PAGE_SZ)
    except OSError:
        return False
    if len(page1) < PAGE_SZ:
        return False
    salt = page1[:SALT_SZ]
    _, mac_key = derive(enc_key, salt)
    ct = page1[SALT_SZ:PAGE_SZ - RESERVE]
    mac = hmac.new(mac_key, ct, hashlib.sha512).digest()
    stored = page1[PAGE_SZ - RESERVE:PAGE_SZ - RESERVE + 64]
    return hmac.compare_digest(mac, stored) or \
        hmac.compare_digest(mac, bytes(a ^ b for a, b in zip(stored, mac_key[:64])))


def readable_regions(pid: int):
    """列出进程可读的内存区间（跳过文件映射，只扫匿名/堆）。"""
    out = []
    try:
        with open("/proc/%d/maps" % pid) as f:
            for line in f:
                parts = line.split()
                if len(parts) < 2 or "r" not in parts[1]:
                    continue
                # 只扫匿名映射，跳过 .so / 可执行文件（省时间）
                path = parts[5] if len(parts) > 5 else ""
                if path.endswith((".so", ".so.1")) or "/opt/wechat" in path:
                    continue
                a, b = parts[0].split("-")
                start, end = int(a, 16), int(b, 16)
                if end - start > 512 * 1024 * 1024:      # 跳过超大映射
                    continue
                out.append((start, end))
    except OSError:            # /proc/<pid>/maps 读失败：进程已退出或权限不足，返回空集
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
            except (OSError, ValueError):   # 该内存段不可读（已 unmap 或受保护），跳过
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
                # salt 前面 32 字节 / 后面 32 字节 / 紧邻前后各取窗口
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
    log.warning("共 %d 个 salt 命中点，均未通过 HMAC 校验", hits)
    return None


def find_pid(pattern: str = "opt/wechat/wechat") -> int:
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except OSError:        # 读 /proc 条目失败：进程可能在扫描期间退出，跳过
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
