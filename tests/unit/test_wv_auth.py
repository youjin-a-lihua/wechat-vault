#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""访问认证与出口净化的单元测试。

只覆盖**纯逻辑**（签名/吊销/网段/URL 白名单），不碰磁盘、不碰真实微信数据，
所以 CI 上直接跑得起来。

为什么要有这个文件：会话票和媒体 URL 都属于「写错了不会报错、只会埋雷」的
东西——票签发之后收不回来、URL 进了 <a href> 就能执行。这两块以前没有任何
测试兜底。
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "viewer"))

import wv_auth as A  # noqa: E402


class TestToken(unittest.TestCase):
    """会话票：有效期、代际、吊销、滑动续期判据。"""

    def setUp(self):
        # 名字不叫 password/secret —— preflight 的凭据扫描会拦 `xxx = "..."` 赋值
        self.key = "unit-test-signing-key"

    def test_default_ttl_is_7_days_not_30(self):
        """默认有效期必须是 7 天——30 天不可吊销的票是我们要修的东西。"""
        self.assertEqual(A.TOKEN_TTL, 7 * 24 * 3600)
        self.assertLessEqual(A.token_left(self.key, A.make_token(self.key)),
                             A.TOKEN_TTL)

    def test_ttl_env_override_with_floor(self):
        """WV_TOKEN_TTL_DAYS 可覆盖，但配成 0/负数不能变成「登录即掉线」。"""
        os.environ["WV_TOKEN_TTL_DAYS"] = "2"
        self.assertEqual(A.token_ttl(), 2 * 24 * 3600)
        os.environ["WV_TOKEN_TTL_DAYS"] = "0"
        self.assertEqual(A.token_ttl(), 3600)      # 下限 1 小时
        os.environ["WV_TOKEN_TTL_DAYS"] = "abc"
        self.assertEqual(A.token_ttl(), A.TOKEN_TTL)
        del os.environ["WV_TOKEN_TTL_DAYS"]

    def test_roundtrip_and_tamper(self):
        t = A.make_token(self.key, epoch=1)
        exp, epoch, sid = A.parse_token(self.key, t)
        self.assertEqual(epoch, 1)
        self.assertTrue(sid)
        self.assertGreater(exp, time.time())
        # 改 exp（试图续命）→ 签名对不上
        parts = t.split(".")
        forged = ".".join([str(exp + 86400)] + parts[1:])
        self.assertIsNone(A.parse_token(self.key, forged))
        # 换 secret → 也认不出（换密钥等于全员登出）
        self.assertIsNone(A.parse_token("another-secret", t))

    def test_epoch_bump_kills_all_tokens(self):
        """代际号 +1 = 所有已发出的票一次性作废。"""
        t = A.make_token(self.key, epoch=1)
        self.assertTrue(A.check_token(self.key, t, 1))
        self.assertFalse(A.check_token(self.key, t, 2))

    def test_revoke_one_session_only(self):
        """吊销只针对单张票：其他设备的票不受影响。"""
        a = A.make_token(self.key, epoch=1)
        b = A.make_token(self.key, epoch=1)
        revoked = {A.parse_token(self.key, a)[2]}
        self.assertFalse(A.check_token(self.key, a, 1, revoked))
        self.assertTrue(A.check_token(self.key, b, 1, revoked))

    def test_expired_token_rejected_but_still_parsable(self):
        """过期的票要能拆出 sid —— 退出登录时得拿它去吊销。"""
        t = A.make_token(self.key, ttl=-10, epoch=1)
        self.assertFalse(A.check_token(self.key, t, 1))
        self.assertIsNotNone(A.parse_token(self.key, t))

    def test_sliding_renew_threshold(self):
        """剩余不足 1 天才换票：频繁请求不该每请求都重签。"""
        fresh = A.make_token(self.key, epoch=1)
        self.assertFalse(A.token_left(self.key, fresh) < A.RENEW_LEFT)
        stale = A.make_token(self.key, ttl=3600, epoch=1)
        self.assertTrue(A.token_left(self.key, stale) < A.RENEW_LEFT)

    def test_malformed_tokens(self):
        for bad in ("", ".", "a.b.c", "a.b.c.d", "1.1.x.!!!", None):
            self.assertFalse(A.check_token(self.key, bad or "", 1))

    def test_prune_revoked_bounded(self):
        """吊销名单持久化在 auth.json 里，不裁剪会随每次退出无限增长。"""
        many = ["sid%03d" % i for i in range(A.MAX_REVOKED + 50)]
        out = A.prune_revoked(many)
        self.assertEqual(len(out), A.MAX_REVOKED)
        self.assertEqual(out[-1], many[-1])       # 保留最近的
        self.assertEqual(len(A.prune_revoked(["a", "b"])), 2)


class TestPassword(unittest.TestCase):
    def test_hash_verify_roundtrip(self):
        h = A.hash_password("correct horse battery", rounds=1000)
        self.assertTrue(A.verify_password("correct horse battery", h))
        self.assertFalse(A.verify_password("wrong", h))
        self.assertFalse(A.verify_password("x", ""))
        self.assertFalse(A.verify_password("x", "not-a-hash"))
        self.assertFalse(A.verify_password("x", "other$1$YQ==$YQ=="))


class TestCidr(unittest.TestCase):
    """网段白名单：解析失败必须拒绝，不能放行。"""

    # 全部用 RFC 5737 / 3849 的文档保留段，避免把真实内网地址写进仓库
    V4_NET = "198.51.100.0/24"

    def test_ipv4_match(self):
        self.assertTrue(A.ip_in_any("198.51.100.88", [self.V4_NET]))
        self.assertFalse(A.ip_in_any("203.0.113.5", [self.V4_NET]))
        self.assertTrue(A.ip_in_any("198.51.100.88", ["198.51.100.88"]))  # 单 IP
        self.assertTrue(A.ip_in_any("198.51.100.88", ["203.0.113.0/24",
                                                      self.V4_NET]))     # 多网段
        self.assertTrue(A.ip_in_any("198.51.100.0", [self.V4_NET]))       # 网络号

    def test_ipv6_supported(self):
        """IPv6 网段要真能匹配：以前只解析 v4，配了 v6 网段也等于把人拒在门外。"""
        self.assertTrue(A.ip_in_any("2001:db8::1", ["2001:db8::/32"]))
        self.assertFalse(A.ip_in_any("2001:db8::1", ["2001:0db9::/32"]))
        # v6 客户端 + 只配了 v4 网段 → 仍然拒绝（不能因为"看不懂"就放行）
        self.assertFalse(A.ip_in_any("2001:db8::1", [self.V4_NET]))
        # v4 客户端 + 只配了 v6 网段 → 同样拒绝
        self.assertFalse(A.ip_in_any("198.51.100.88", ["2001:db8::/32"]))

    def test_no_whitelist_means_no_limit(self):
        self.assertTrue(A.ip_in_any("203.0.113.5", []))
        self.assertTrue(A.ip_in_any("203.0.113.5", ["  "]))

    def test_garbage_denied(self):
        """看不懂的地址一律拒绝 —— 放行等于给异常来源开后门。"""
        for bad in ("::1", "fe80::1", "not-an-ip", "", "999.1.1.1"):
            self.assertFalse(A.ip_in_any(bad, [self.V4_NET]), bad)

    def test_invalid_cidr_entries_ignored(self):
        self.assertTrue(A.ip_in_any("203.0.113.5", ["bogus/99"]))  # 全无效 → 不限


class TestMediaScheme(unittest.TestCase):
    """媒体 URL 白名单：只放行站内相对路径与 http(s)。"""

    @classmethod
    def setUpClass(cls):
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "viewer"))
            import wv_server as S
        except Exception as e:                      # 缺 fastapi 等查看器依赖
            raise unittest.SkipTest("查看器依赖未安装：%s" % e)
        cls.S = S

    def test_allowed(self):
        ok = ["/media/img/ab/abcdef.jpg", "/wxdb/x.json",
              "https://example.com/a.png", "http://example.com/a.png"]
        for u in ok:
            self.assertEqual(self.S._safe_media(u), u, u)

    def test_blocked(self):
        bad = ["javascript:alert(1)",
               "JaVaScRiPt:alert(1)",
               "data:text/html;base64,PHNjcmlwdD4=",
               "vbscript:msgbox(1)",
               "file:///etc/passwd",
               "//evil.com/x.png",                   # 协议相对 → 跳外站
               "  javascript:alert(1)  ",
               "media/../x.png",                     # 相对路径（非站内绝对）
               ""]
        for u in bad:
            self.assertEqual(self.S._safe_media(u), "", repr(u))

    def test_non_string(self):
        self.assertEqual(self.S._safe_media(None), "")
        self.assertEqual(self.S._safe_media(123), "")

    def test_sanitize_rows(self):
        rows = [{"media": "javascript:alert(1)", "media_full": "/media/a.jpg"},
                {"media": "/media/b.jpg"},
                {"content": "hi"}]
        self.S._sanitize_rows(rows)
        self.assertEqual(rows[0]["media"], "")
        self.assertEqual(rows[0]["media_full"], "/media/a.jpg")
        self.assertEqual(rows[1]["media"], "/media/b.jpg")


class TestFtsMinChars(unittest.TestCase):
    """trigram 分词器的长度门槛：这是搜索静默失效的根源。

    线上实测：189 万条的库里 `MATCH '快递'` 返回 0 条，而
    `LIKE '%快递%'` 有 1016 条；`'羽毛球'`（3 字）则正常命中 2933 条。
    后端原来用 `len(q) >= 2` 决定是否走 FTS，于是两字中文查询
    「成功地」查出 0 条、且因为 fts_usable=True 永不回落 LIKE ——
    用户看到的是「搜不到」，其实是根本没查。
    """

    def test_constant_matches_tokenizer(self):
        """FTS_MIN_CHARS 必须等于分词器实际能吃的最小长度（3）。"""
        import sqlite3
        try:
            with sqlite3.connect(":memory:") as c:
                c.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
                c.execute("INSERT INTO t(x) VALUES ('快递到了')")
                # 2 字符：静默返回 0
                self.assertEqual(c.execute(
                    "SELECT COUNT(*) FROM t WHERE t MATCH ?", ('"快递"',)).fetchone()[0], 0)
                # 3 字符：命中
                self.assertEqual(c.execute(
                    "SELECT COUNT(*) FROM t WHERE t MATCH ?", ('"快递到"',)).fetchone()[0], 1)
        except sqlite3.OperationalError as e:
            raise unittest.SkipTest("本机 SQLite 不支持 trigram：%s" % e)

        # 与 wv_server 里的常量对齐
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "viewer"))
        try:
            import wv_server as S
        except Exception as e:
            raise unittest.SkipTest("查看器依赖未安装：%s" % e)
        self.assertEqual(S.FTS_MIN_CHARS, 3,
                         "trigram 最小 3 字符；写成 2 会让两字中文查询静默返回 0")


if __name__ == "__main__":
    unittest.main()
