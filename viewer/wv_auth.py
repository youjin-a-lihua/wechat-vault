#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Vault - 访问认证
=======================

设计目标（对应项目红线「不开公网」）：
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
import ipaddress
import os
import secrets
import time
from typing import Iterable

# ── 参数 ──
PBKDF2_ROUNDS = 600_000
SALT_BYTES = 16
# 会话有效期：7 天。
# 原来是 30 天，问题是签发出去就再也收不回来——浏览器里那份 Cookie
# 只要还在有效期内就一直有效，清不掉。7 天 + 滑动续期（见 RENEW_LEFT）
# 在体验上和 30 天一样「不用来回登录」，但把一份泄漏令牌的有效窗口
# 从一个月压到一周以内。可用 WV_TOKEN_TTL_DAYS 覆盖（最短 1 小时）。
TOKEN_TTL = 7 * 24 * 3600
# 剩余有效期低于这个值，且请求还在正常发生 → 重新发一张新票（滑动续期）。
# 判据是「还在用」而不是「时间到了」：人走了令牌自然过期，人还在就无缝续。
RENEW_LEFT = 24 * 3600
COOKIE_NAME = "wv_session"
# 吊销名单上限：sid 只保留最近这些条。令牌最长活 7 天，
# 超过这个长度的旧 sid 就算留着也永远等不到一个有效令牌来匹配它。
MAX_REVOKED = 200

def token_ttl() -> int:
    """环境变量可覆盖的 TTL（天 → 秒），下限 1 小时，防止配成 0 直接登出。"""
    import os
    raw = os.environ.get("WV_TOKEN_TTL_DAYS", "")
    if not raw:
        return TOKEN_TTL
    try:
        days = float(raw)
    except ValueError:
        return TOKEN_TTL
    return max(3600, int(days * 24 * 3600))

# 登录限流：同一 IP 连续失败达到阈值后锁定
MAX_FAILS = 5
LOCK_SECONDS = 300

# 密码哈希

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

# 会话令牌（无状态签名 Cookie）

def _sign(secret: bytes, payload: str) -> str:
    """HMAC-SHA256 签名 → URL 安全 base64（去 padding）。"""
    return base64.urlsafe_b64encode(
        hmac.new(secret, payload.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")

def make_token(secret: str, ttl: int = 0, epoch: int = 1, sid: str = "") -> str:
    """生成 `过期时间戳.代际号.会话号.签名` 形式的令牌。

    epoch（代际）：整数，写进 auth.json。把它 +1 就等于「把所有已发出的
                   票一次性作废」——怀疑票泄漏、或改了密码时用。
    sid（会话号）：每张票一个随机数，退出登录时把它记进吊销名单，
                   只让这一张票失效，其他设备不受影响。
    """
    exp = int(time.time()) + (ttl or token_ttl())
    sid = sid or secrets.token_hex(8)
    payload = f"{exp}.{int(epoch)}.{sid}"
    sig = _sign(secret.encode("utf-8"), payload)
    return f"{payload}.{sig}"

def parse_token(secret: str, token: str) -> tuple[int, int, str] | None:
    """校验签名并拆出 (过期时间戳, 代际号, 会话号)；任何一项不对都返回 None。

    只验签名和格式，**不看有效期**：调用方可能想拿过期票的 sid 去吊销它，
    那时不能因为过期就拆不出来。有效期判断在 check_token 里。
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    exp_s, epoch_s, sid, sig = parts
    payload = f"{exp_s}.{epoch_s}.{sid}"
    if not hmac.compare_digest(_sign(secret.encode("utf-8"), payload), sig):
        return None
    try:
        return int(exp_s), int(epoch_s), sid
    except ValueError:
        return None

def check_token(secret: str, token: str, epoch: int = 1,
                revoked: Iterable[str] | None = None) -> bool:
    """校验令牌：签名 + 有效期 + 代际号一致 + 未被吊销。"""
    p = parse_token(secret, token)
    if not p:
        return False
    exp, ep, sid = p
    if ep != int(epoch):
        return False
    if revoked and sid in revoked:
        return False
    return exp > time.time()

def token_left(secret: str, token: str) -> int:
    """令牌剩余有效秒数（无效/已过期返回 0）。滑动续期据此判断要不要换票。"""
    p = parse_token(secret, token)
    if not p:
        return 0
    return max(0, p[0] - int(time.time()))

def prune_revoked(revoked: Iterable[str]) -> list[str]:
    """裁剪吊销名单：只留最近 MAX_REVOKED 条。

    名单是持久化在 auth.json 里的，不裁剪会随着每次退出登录无限增长。
    """
    out = list(revoked)
    return out[-MAX_REVOKED:] if len(out) > MAX_REVOKED else out

# 登录限流

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
        """记一次登录失败（窗口外的旧记录会被 is_locked 清掉）。"""
        self._fails.setdefault(ip, []).append(time.time())

    def reset(self, ip: str) -> None:
        """登录成功后清空该 IP 的失败记录。"""
        self._fails.pop(ip, None)

# 局域网网段白名单

def parse_net(cidr: str):
    """把 `a.b.c.d/n`、`单个 IP`、`IPv6 网段` 解析成 ip_network；不认识返回 None。"""
    cidr = (cidr or "").strip()
    if not cidr:
        return None
    # 单个 IP（不带 /）：ipaddress 认得，但补成 /32 /128 更明确
    try:
        if "/" not in cidr:
            ip = ipaddress.ip_address(cidr)
            cidr = "%s/%d" % (cidr, 32 if ip.version == 4 else 128)
        return ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return None

def ip_in_any(ip: str, cidrs: Iterable[str]) -> bool:
    """判断 IP 是否落在任一台网内（IPv4 / IPv6 都支持）。

    注意「解析失败/格式不认识」的默认方向：这里选**拒绝**（return False），
    而不是放行。原因是白名单一旦配上，就意味着"只信这些网段"；
    遇到看不懂的地址却放行，等于给所有异常来源开后门。

    IPv6：以前的实现只解析 IPv4 网段，v6 客户端一律按「不在白名单」处理，
    于是配了 v6 网段也没用、局域网走 IPv6 访问会被整片拒绝。
    现在用 ipaddress 统一处理，两个版本各自匹配各自的网段。
    """
    nets = [n for n in (parse_net(c) for c in cidrs) if n]
    if not nets:
        return True                       # 没配白名单 = 不限制网段
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return any(addr.version == n.version and addr in n for n in nets)

def gen_secret() -> str:
    """生成会话签名密钥（WV_SECRET；每个实例独立，泄漏即全员登出）。"""
    return secrets.token_urlsafe(32)

# CLI：生成密码哈希

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
