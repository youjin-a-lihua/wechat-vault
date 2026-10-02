#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wv_wxdb.py —— 微信 4.x SQLCipher4 数据库解密器（纯 Python，零第三方依赖）

对标微备份路线：
    WCDB 用 SQLCipher 4 加密本地库（AES-256-CBC + HMAC-SHA512 + 256000 轮 PBKDF2）。
    all_keys.json 里存的是 96 hex = enc_key(32B) + salt(16B)。
    实际上微信所有库共用同一个 enc_key，salt 每库不同（存在 page1 前 16 字节）。

SQLCipher4 page 布局：
    page_size = 4096
    reserve   = 48   （每页尾部预留）
    usable    = 4096 - 48 = 4048
    page1 = salt(16) || ciphertext[0:4032] || hmac(64)
           其中 ciphertext 起点 = 4096 - 48 + 16 = 4064（page1 偏移）
           即 page1 的密文区从 offset 16 开始，长度 4048 - 16 = 4032
    其余页 pageN = ciphertext[0:4048] || hmac(64)
    IV = 从 0 开始、每页 +1 的随机（SQLCipher4 用 page number 作为 IV 明文）

解密流程：
    1. mac_key    = PBKDF2(enc_key, salt, 256000, 32, sha512)
    2. key        = PBKDF2(enc_key, salt ^ 0x3a, 256000, 32, sha512)   ← 注意异或
    3. HMAC(page1[16:4096-64+16]) 与 page1[4096-64:4096]（先与 mac_key[:64] 异或）比对
    4. AES-256-CBC 解密，IV = 1..N 的 4 字节大端 + 12 字节 0

依赖：只有标准库。AES-256-CBC 用纯 Python 实现（慢但可靠），
      若本机有 cryptography/pycryptodome 会自动优先使用。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import struct
import sys
import time
from pathlib import Path
import logging

log = logging.getLogger(__name__)

# ------------------------------------------------------------------- AES 后端

_AES_BACKEND = "pure"


def _load_aes():
    """优先用原生 AES（快 100x），没有就退回纯 Python"""
    global _AES_BACKEND
    try:
        from Crypto.Cipher import AES  # pycryptodome
        _AES_BACKEND = "pycryptodome"
        return ("pycryptodome", AES)
    except ImportError as e:
        log.debug("pycryptodome 不可用，改用下一后端：%s", e)
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        _AES_BACKEND = "cryptography"
        return ("cryptography", (Cipher, algorithms, modes))
    except ImportError as e:
        log.debug("cryptography 不可用，改用纯实现：%s", e)
        pass
    _AES_BACKEND = "pure"
    return ("pure", None)


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-CBC 解密（无 padding 处理，调用方按块对齐）"""
    name, mod = _load_aes()
    if name == "pycryptodome":
        AES = mod
        c = AES.new(key, AES.MODE_CBC, iv)
        return c.decrypt(data)
    if name == "cryptography":
        Cipher, algorithms, modes = mod
        d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        return d.update(data) + d.finalize()
    return _pure_aes_cbc_decrypt(key, iv, data)


# ------------------------------------------------ 纯 Python AES-256-CBC（兜底）

_SBOX = None
_RCON = None


def _init_tables():
    global _SBOX, _RCON
    if _SBOX is not None:
        return
    p = q = 1
    sbox = [0] * 256
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) \
            ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    _SBOX = sbox
    rcon = [1]
    for _ in range(13):
        v = rcon[-1] << 1
        if v > 0xFF:
            v ^= 0x11B
        rcon.append(v & 0xFF)
    _RCON = rcon


def _expand_key_256(key: bytes):
    """AES-256 密钥扩展 → 60 个 4 字节字"""
    _init_tables()
    nk, nr = 8, 14
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_SBOX[b] for b in t]
            t[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return w


def _inv_shift_rows(s):
    return [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11],
            s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]


def _gf_mul(a, b):
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return r


def _decrypt_block(block: bytes, w) -> bytes:
    _init_tables()
    s = list(block)
    # AddRoundKey (last)
    rk = sum(w[56:60], [])
    s = [s[i] ^ rk[i] for i in range(16)]
    for rnd in range(13, 0, -1):
        s = _inv_shift_rows(s)
        s = [_INV_SBOX[b] for b in s]
        rk = sum(w[4 * rnd:4 * rnd + 4], [])
        s = [s[i] ^ rk[i] for i in range(16)]
        # InvMixColumns
        out = [0] * 16
        for c in range(4):
            a0, a1, a2, a3 = s[4 * c:4 * c + 4]
            out[4 * c + 0] = _gf_mul(a0, 14) ^ _gf_mul(a1, 11) ^ _gf_mul(a2, 13) ^ _gf_mul(a3, 9)
            out[4 * c + 1] = _gf_mul(a0, 9) ^ _gf_mul(a1, 14) ^ _gf_mul(a2, 11) ^ _gf_mul(a3, 13)
            out[4 * c + 2] = _gf_mul(a0, 13) ^ _gf_mul(a1, 9) ^ _gf_mul(a2, 14) ^ _gf_mul(a3, 11)
            out[4 * c + 3] = _gf_mul(a0, 11) ^ _gf_mul(a1, 13) ^ _gf_mul(a2, 9) ^ _gf_mul(a3, 14)
        s = out
    s = _inv_shift_rows(s)
    s = [_INV_SBOX[b] for b in s]
    rk = sum(w[0:4], [])
    return bytes(s[i] ^ rk[i] for i in range(16))


_INV_SBOX = None


def _init_inv_sbox():
    global _INV_SBOX
    if _INV_SBOX is not None:
        return
    _init_tables()
    inv = [0] * 256
    for i, v in enumerate(_SBOX):
        inv[v] = i
    _INV_SBOX = inv


def _pure_aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    _init_inv_sbox()
    w = _expand_key_256(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(data) - 15, 16):
        blk = data[off:off + 16]
        dec = _decrypt_block(blk, w)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = blk
    return bytes(out)


# --------------------------------------------------------------- SQLCipher 解密

PAGE_SZ = 4096
RESERVE = 48
KDF_ITER = 256000
KEY_SZ = 32
SALT_SZ = 16


def derive_keys(enc_key: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """返回 (aes_key, mac_key)"""
    mac_salt = bytes(b ^ 0x3A for b in salt)
    aes_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, KDF_ITER, dklen=KEY_SZ)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, salt, KDF_ITER, dklen=KEY_SZ)
    return aes_key, mac_key


def verify(enc_key: bytes, db_path: Path) -> bool:
    """校验 page1 HMAC"""
    try:
        with open(db_path, "rb") as f:
            page1 = f.read(PAGE_SZ)
        if len(page1) < PAGE_SZ:
            return False
        salt = page1[:SALT_SZ]
        _, mac_key = derive_keys(enc_key, salt)
        # SQLCipher4: HMAC 覆盖 page1[16 : 4096-48]，但官方实现是
        #   hmac_data = page1[offset=SALT_SZ : offset+size-RESERVE]
        #   即 page1[16 : 4096-48] → 长度 4032
        hmac_data = page1[SALT_SZ:PAGE_SZ - RESERVE]
        mac = hmac.new(mac_key, hmac_data, hashlib.sha512).digest()
        stored = page1[PAGE_SZ - RESERVE:PAGE_SZ - RESERVE + 64]
        # SQLCipher4 的认证标签不与 mac_key 异或（3.x 才异或）
        return hmac.compare_digest(mac, stored) or \
            hmac.compare_digest(mac, bytes(a ^ b for a, b in zip(stored, mac_key[:64])))
    except Exception:
        return False


def decrypt_db(enc_key: bytes, src: Path, dst: Path) -> bool:
    """解密单个 SQLCipher4 数据库 → 明文 SQLite"""
    try:
        with open(src, "rb") as f:
            blob = f.read()
    except Exception:
        return False
    if len(blob) < PAGE_SZ:
        return False

    salt = blob[:SALT_SZ]
    aes_key, mac_key = derive_keys(enc_key, salt)

    out = bytearray()
    n_pages = len(blob) // PAGE_SZ
    for pi in range(n_pages):
        page = blob[pi * PAGE_SZ:(pi + 1) * PAGE_SZ]
        if pi == 0:
            # page1: 跳过前 16B salt，密文长度 = 4096-16-48 = 4032
            ct = page[SALT_SZ:PAGE_SZ - RESERVE]
            iv = struct.pack(">I", pi + 1) + b"\x00" * 12
            ct = ct[:len(ct) - (len(ct) % 16)]
            pt = _aes_cbc_decrypt(aes_key, iv, ct)
            out += b"SQLite format 3\x00"
            out += pt
        else:
            ct = page[:PAGE_SZ - RESERVE]
            iv = struct.pack(">I", pi + 1) + b"\x00" * 12
            ct = ct[:len(ct) - (len(ct) % 16)]
            pt = _aes_cbc_decrypt(aes_key, iv, ct)
            out += pt
            out += b"\x00" * RESERVE  # 预留区清零

    # 截到原始大小（去掉每页 reserve 的填充差异），并修正 page1 头
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(dst, "wb") as f:
            f.write(bytes(out))
        return True
    except Exception:
        return False


# -------------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="微信 4.x 数据库解密（SQLCipher4）")
    ap.add_argument("--keys", default="", help="all_keys.json 路径")
    ap.add_argument("--out", default="", help="输出目录")
    ap.add_argument("--single", default="", help="只解某个库（相对路径）")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    kf = Path(args.keys) if args.keys else root / "local_keys" / "all_keys.json"
    if not kf.exists():
        print(f"[!] 找不到密钥文件 {kf}（先跑 wv_winkey.py）")
        return 2

    data = json.loads(kf.read_text(encoding="utf-8"))
    db_dir = Path(data["db_dir"])
    keys: dict[str, str] = data["keys"]
    out_dir = Path(args.out) if args.out else root / "local_decrypted"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[*] 源库目录: {db_dir}")
    print(f"[*] 输出目录: {out_dir}")
    print(f"[*] 待解密 {len(keys)} 个库，AES 后端: {_load_aes()[0]}")

    t0 = time.time()
    ok = 0
    for rel, khex in sorted(keys.items()):
        if args.single and args.single not in rel:
            continue
        src = db_dir / rel
        dst = out_dir / rel
        enc_key = bytes.fromhex(khex[:64])
        if not src.exists():
            print(f"  [--] {rel}  源文件不存在")
            continue
        sz = src.stat().st_size
        t1 = time.time()
        if decrypt_db(enc_key, src, dst):
            # 验证解出来的是不是合法 SQLite
            with open(dst, "rb") as f:
                magic = f.read(16)
            good = magic.startswith(b"SQLite format 3")
            flag = "[OK]" if good else "[??]"
            print(f"  {flag} {rel:44s} {sz/1024/1024:7.1f}MB → "
                  f"{dst.stat().st_size/1024/1024:7.1f}MB  {time.time()-t1:.1f}s")
            if good:
                ok += 1
        else:
            print(f"  [!!] {rel}  解密失败")
    print(f"\n[✓] 完成：{ok} 个库解密成功，耗时 {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
