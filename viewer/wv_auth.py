#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Vault - 访问认证
=======================

设计目标（对应项目红线「绝不开公网」）：
  1. 默认**关闭**，只要不设密码就是老行为（本机/可信环境零摩擦）
  2. 一旦设了密码，除白名单路径外**所有请求**都要先登录
  3. 密码只存哈希（PBKDF2-HMAC-SHA256，60 万轮 + 每密码独立随机盐）
  4. 会话用签名 Cookie（HMAC-SHA256），带过期时间，无服务端状态
  5. 登录失败限流（防暴力破解）
  6. 支持局域网网段白名单：不在白名单的客户端直接拒绝

不引入第三方依赖 —— argon2/passlib 会增加镜像构建风险，
PBKDF2-SHA256 60 万轮是 OWASP 2023 推荐量级，对本场景足够。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Iterable

# ── 参数 ──
PBKDF2_ROUNDS = 600_000
SALT_BYTES = 16
TOKEN_TTL = 30 * 24 * 3600          # 会话有效期 30 天
COOKIE_NAME = "wv_session"

# 登录限流：同一 IP 连续失败达到阈值后锁定
MAX_FAILS = 5
LOCK_SECONDS = 300


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------

def hash_password(password: str, rounds: int = PBKDF2_ROUNDS) -> str:
    """生成 `pbkdf2_sha256$轮数$盐b64$哈希b64` 格式的密码串。"""
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return "pbkdf2_sha256${}${}${}".format(
        rounds,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    """校验密码。用 compare_digest 防时序攻击。"""
    if not encoded:
        return False
    try:
        algo, rounds_s, salt_b64, hash_b64 = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        rounds = int(rounds_s)
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(hash_b64)
    except (ValueError, IndexError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(dk, expect)


# ---------------------------------------------------------------------------
# 会话令牌（无状态签名 Cookie）
# ---------------------------------------------------------------------------

def _sign(secret: bytes, payload: str) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")


def make_token(secret: str, ttl: int = TOKEN_TTL, extra: str = "") -> str:
    """生成 `过期时间戳.随机数.签名` 形式的令牌。"""
    exp = int(time.time()) + ttl
    nonce = secrets.token_hex(8)
    payload = f"{exp}.{nonce}.{extra}"
    sig = _sign(secret.encode("utf-8"), payload)
    return f"{payload}.{sig}"


def check_token(secret: str, token: str) -> bool:
    """校验令牌签名与有效期。"""
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 4:
        return False
    exp_s, nonce, extra, sig = parts
    payload = f"{exp_s}.{nonce}.{extra}"
    if not hmac.compare_digest(_sign(secret.encode("utf-8"), payload), sig):
        return False
    try:
        return int(exp_s) > time.time()
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 登录限流
# ---------------------------------------------------------------------------

class LoginThrottle:
    """简单的内存限流器：按 IP 计数失败次数。"""

    def __init__(self, max_fails: int = MAX_FAILS, lock_seconds: int = LOCK_SECONDS):
        self.max_fails = max_fails
        self.lock_seconds = lock_seconds
        self._fails: dict[str, list[float]] = {}

    def is_locked(self, ip: str) -> tuple[bool, int]:
        """返回 (是否锁定, 剩余秒数)。"""
        now = time.time()
        hits = [t for t in self._fails.get(ip, []) if now - t < self.lock_seconds]
        self._fails[ip] = hits
        if len(hits) >= self.max_fails:
            return True, int(self.lock_seconds - (now - hits[0])) + 1
        return False, 0

    def record_fail(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.time())

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)


# ---------------------------------------------------------------------------
# 局域网网段白名单
# ---------------------------------------------------------------------------

def _parse_cidr(cidr: str) -> tuple[int, int] | None:
    """把 `a.b.c.d/n` 或单个 IP 解析成 (网络整数, 掩码整数)。"""
    cidr = cidr.strip()
    if not cidr:
        return None
    if "/" not in cidr:
        cidr = cidr + "/32"
    try:
        net_s, bits_s = cidr.split("/")
        bits = int(bits_s)
        if not (0 <= bits <= 32):
            return None
        octets = [int(x) for x in net_s.split(".")]
        if len(octets) != 4 or any(not (0 <= o <= 255) for o in octets):
            return None
        net = 0
        for o in octets:
            net = (net << 8) | o
        mask = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF if bits else 0
        return net & mask, mask
    except (ValueError, IndexError):
        return None


def ip_in_any(ip: str, cidrs: Iterable[str]) -> bool:
    """判断 IP 是否落在任一台网内。IPv6 或解析失败一律放行（交给密码管）。"""
    nets = [n for n in (_parse_cidr(c) for c in cidrs) if n]
    if not nets:
        return True                       # 没配白名单 = 不限制网段
    if ":" in ip:                         # IPv6 不做网段判定（简化）
        return True
    try:
        octets = [int(x) for x in ip.split(".")]
        if len(octets) != 4:
            return True
        v = 0
        for o in octets:
            v = (v << 8) | o
    except (ValueError, IndexError):
        return True
    return any((v & mask) == net for net, mask in nets)


def gen_secret() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# CLI：生成密码哈希
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="生成 WeChat Vault 密码哈希")
    ap.add_argument("password", nargs="?", help="明文密码（不传则交互输入）")
    ap.add_argument("--secret", action="store_true", help="改为生成 WV_SECRET")
    args = ap.parse_args()

    if args.secret:
        print(gen_secret())
    else:
        pw = args.password or input("请输入密码: ")
        if not pw:
            raise SystemExit("密码不能为空")
        print(hash_password(pw))
