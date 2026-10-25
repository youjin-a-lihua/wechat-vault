#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wv_sync 会话键（conv_id）的单元测试。

背景：conv_id 原来是 `账号::显示名`。显示名会变——对方改备注、群里改名，
下一次同步就算出个"新"键，于是会话分裂、而 msg_id 又是拿 conv_id 算的，
整段历史被当成新消息重插一遍。这个坑只会随使用时间越来越深，而且
「看起来只是列表里多了一行」，很难被发现。

这里用内存库把迁移过程完整跑一遍（改名 / 重算 msg_id / 合并 / 跳过歧义），
因为一旦迁错就是几万条重复消息，人工在 189 万行里根本查不出来。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "archiver"))

import wv_sync as S  # noqa: E402

ACC = "wxid_self"


def _vault() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    S.ensure_schema(conn)
    return conn


def _msg_id(cid, local_id, ctime):
    return S._recompute_mid(cid, local_id, ctime)


def _legacy_conv(conn, cid, title, msgs):
    """塞一条老式会话（conv_id 用显示名，username 为空）+ 若干消息。"""
    conn.execute(
        "INSERT INTO conv (conv_id, account, title, n_msg) VALUES (?,?,?,?)",
        (cid, ACC, title, len(msgs)))
    for local_id, ctime in msgs:
        conn.execute(
            "INSERT INTO msg (msg_id, conv_id, account, seq, ts, type, content)"
            " VALUES (?,?,?,?,?,?,?)",
            (_msg_id(cid, local_id, ctime), cid, ACC, local_id, ctime,
             "text", "m%d" % local_id))


class TestStableCid(unittest.TestCase):
    def test_key_is_username_not_display_name(self):
        self.assertEqual(S._stable_cid(ACC, "wxid_abc123"), "wxid_self::wxid_abc123")
        self.assertEqual(S._stable_cid(ACC, "123@chatroom"), "wxid_self::123@chatroom")
        cid = S._stable_cid(ACC, "wxid_abc123")
        self.assertNotIn("小明", cid)          # 显示名绝不进键

    def test_rename_does_not_change_key(self):
        """同一个 username，显示名换了 → 键不变（这条就是修复的全部意义）。"""
        self.assertEqual(S._stable_cid(ACC, "wxid_abc123"),
                         S._stable_cid(ACC, "wxid_abc123"))


class TestMigrateConvKeys(unittest.TestCase):
    def setUp(self):
        self.conn = _vault()

    def tearDown(self):
        self.conn.close()

    def _conv(self, cid):
        return self.conn.execute(
            "SELECT conv_id, title, username, n_msg FROM conv WHERE conv_id=?",
            (cid,)).fetchone()

    def test_renames_conv_and_msgs_and_recomputes_msg_id(self):
        old = "%s::小明" % ACC
        _legacy_conv(self.conn, old, "小明", [(1, 1000), (2, 2000)])
        st = S.migrate_conv_keys(self.conn, ACC, {"小明": {"wxid_abc123"}})

        new = "%s::wxid_abc123" % ACC
        self.assertEqual(st["conv"], 1)
        self.assertEqual(st["msg"], 2)
        self.assertEqual(st["skip"], 0)

        row = self._conv(new)
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "小明")        # 显示名保留
        self.assertEqual(row[2], "wxid_abc123")  # 稳定身份已认领
        self.assertEqual(row[3], 2)              # n_msg 按实际条数重算
        self.assertIsNone(self._conv(old))       # 老键没了

        ids = {r[0] for r in self.conn.execute("SELECT msg_id FROM msg")}
        self.assertEqual(ids, {_msg_id(new, 1, 1000), _msg_id(new, 2, 2000)})
        convs = {r[0] for r in self.conn.execute("SELECT conv_id FROM msg")}
        self.assertEqual(convs, {new})

    def test_idempotent(self):
        """第二次跑必须什么都不做（Conv 认领过 username 就不再进 legacy 集合）。"""
        old = "%s::小明" % ACC
        _legacy_conv(self.conn, old, "小明", [(1, 1000)])
        S.migrate_conv_keys(self.conn, ACC, {"小明": {"wxid_abc123"}})
        st = S.migrate_conv_keys(self.conn, ACC, {"小明": {"wxid_abc123"}})
        self.assertEqual(st["conv"], 0)
        self.assertEqual(st["msg"], 0)

    def test_json_prefixed_msg_id_kept(self):
        """导入链路的 msg_id 反推不出原始字段，只迁 conv_id，不重写 id。"""
        old = "%s::小明" % ACC
        self.conn.execute(
            "INSERT INTO conv (conv_id, account, title) VALUES (?,?,?)",
            (old, ACC, "小明"))
        self.conn.execute(
            "INSERT INTO msg (msg_id, conv_id, account, seq, ts)"
            " VALUES (?,?,?,?,?)", ("json-abc", old, ACC, 7, 1000))
        S.migrate_conv_keys(self.conn, ACC, {"小明": {"wxid_abc123"}})
        new = "%s::wxid_abc123" % ACC
        row = self.conn.execute(
            "SELECT msg_id, conv_id FROM msg").fetchone()
        self.assertEqual(row[0], "json-abc")
        self.assertEqual(row[1], new)

    def test_ambiguous_title_skipped(self):
        """显示名撞车又判不出是谁 —— 宁可不迁，也不能猜错。"""
        old = "%s::小明" % ACC
        _legacy_conv(self.conn, old, "小明", [(1, 1000)])
        st = S.migrate_conv_keys(
            self.conn, ACC, {"小明": {"wxid_a", "wxid_b"}})
        self.assertEqual(st["conv"], 0)
        self.assertEqual(st["skip"], 1)
        self.assertIsNotNone(self._conv(old))     # 原样留着

    def test_ambiguous_marks_stuck_so_sync_keeps_old_key(self):
        """判不出归属时，必须把老键交回主循环继续用，否则会重复入库。

        同名两人原本合并在 `账号::小明` 里。迁移若判不出是谁就跳过，
        而主循环又各建一个 `账号::wxid_*` —— msg_id 跟着 cid 变，
        两人整段历史就被再插一遍。
        """
        old = "%s::小明" % ACC
        _legacy_conv(self.conn, old, "小明", [(1, 1000)])
        st = S.migrate_conv_keys(self.conn, ACC, {"小明": {"wxid_a", "wxid_b"}})
        self.assertEqual(st["stuck"].get("小明"), old)
        self.assertEqual(st["legacy"], 0)   # 有候选但判不出 ≠ 无从归属

    def test_unmatched_title_counts_as_legacy_not_stuck(self):
        """显示名在当前微信号里根本不存在 —— 计 legacy，不进 stuck。

        实测这类会话共 12 个，清一色是 JSON 导入留下的历史记录（微信那边
        早已没有这个会话）。它们的 msg_id 是 json- 前缀，本轮同步不碰，
        所以"保持老键"没有分裂风险；把它算进 stuck 会让统计看起来像有
        12 个失败，掩盖真正需要人看一眼的那 2 个歧义。
        """
        old = "%s::早就删掉的人" % ACC
        _legacy_conv(self.conn, old, "早就删掉的人", [(1, 1000)])
        st = S.migrate_conv_keys(self.conn, ACC, {"另一个名字": {"wxid_x"}})
        self.assertEqual(st["conv"], 0)
        self.assertEqual(st["skip"], 1)
        self.assertEqual(st["legacy"], 1)
        self.assertEqual(st["stuck"], {})   # 不得污染 stuck
        self.assertIsNotNone(self._conv(old))

    def test_ambiguous_resolved_by_sender_username(self):
        """同名时拿会话里出现过的 sender_username 认人。"""
        old = "%s::小明" % ACC
        conn = self.conn
        conn.execute("INSERT INTO conv (conv_id, account, title) VALUES (?,?,?)",
                     (old, ACC, "小明"))
        conn.execute(
            "INSERT INTO msg (msg_id, conv_id, account, seq, ts, sender_username)"
            " VALUES (?,?,?,?,?,?)", ("json-1", old, ACC, 1, 1000, "wxid_b"))
        st = S.migrate_conv_keys(conn, ACC, {"小明": {"wxid_a", "wxid_b"}})
        self.assertEqual(st["conv"], 1)
        self.assertIsNotNone(self._conv("%s::wxid_b" % ACC))

    def test_merge_when_new_key_already_exists(self):
        """新键已存在（别的路径先建过）→ 合并过去，重复的由 OR IGNORE + DELETE 收掉。"""
        old = "%s::小明" % ACC
        new = "%s::wxid_abc123" % ACC
        # 老键里有一条
        _legacy_conv(self.conn, old, "小明", [(1, 1000)])
        # 新键里已经有同一条（同 seq/ts → 重算出的 msg_id 必然相同）
        conn = self.conn
        conn.execute("INSERT INTO conv (conv_id, account, title, username)"
                     " VALUES (?,?,?,?)", (new, ACC, "小明", "wxid_abc123"))
        conn.execute(
            "INSERT INTO msg (msg_id, conv_id, account, seq, ts)"
            " VALUES (?,?,?,?,?)", (_msg_id(new, 1, 1000), new, ACC, 1, 1000))

        S.migrate_conv_keys(conn, ACC, {"小明": {"wxid_abc123"}})
        self.assertIsNone(self._conv(old))
        rows = conn.execute("SELECT msg_id, conv_id FROM msg").fetchall()
        self.assertEqual(len(rows), 1)                       # 没留重复
        self.assertEqual(rows[0][1], new)
        self.assertEqual(conn.execute(
            "SELECT n_msg FROM conv WHERE conv_id=?", (new,)).fetchone()[0], 1)

    def test_username_equals_title_is_just_claimed(self):
        """username 恰好等于显示名（如某些群号直显）→ 只补 username，不动键。"""
        cid = "%s::123@chatroom" % ACC
        self.conn.execute(
            "INSERT INTO conv (conv_id, account, title) VALUES (?,?,?)",
            (cid, ACC, "123@chatroom"))
        st = S.migrate_conv_keys(self.conn, ACC, {"123@chatroom": {"123@chatroom"}})
        self.assertEqual(st["conv"], 1)
        self.assertEqual(self._conv(cid)[2], "123@chatroom")

    def test_other_account_untouched(self):
        """多账号隔离：只迁指定账号下的会话。"""
        old = "别的号::小明"
        _legacy_conv(self.conn, old, "小明", [(1, 1000)])
        self.conn.execute("UPDATE conv SET account=? WHERE conv_id=?", ("别的号", old))
        st = S.migrate_conv_keys(self.conn, ACC, {"小明": {"wxid_abc123"}})
        self.assertEqual(st["conv"], 0)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM conv WHERE conv_id=?", (old,)).fetchone())


class TestSchema(unittest.TestCase):
    def test_conv_has_username_column(self):
        conn = _vault()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(conv)")}
        self.assertIn("username", cols)
        self.assertIn("title", cols)
        conn.close()

    def test_upgrades_legacy_db_without_username(self):
        """老库（没有 username 列）跑 ensure_schema 必须不报错，且建出索引。

        这是真机上踩到的崩溃：`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，
        老库跑完建表语句依然没有 username；若接着跑
        `CREATE INDEX ... ON conv(account, username)` 就 `no such column`，
        升级后第一轮同步直接崩，而且崩在迁移之前，一条消息都进不来。
        """
        conn = sqlite3.connect(":memory:")
        # 复刻线上老 schema：没有 username，索引也只有老的那几个
        conn.executescript("""
            CREATE TABLE conv (
                conv_id TEXT PRIMARY KEY, account TEXT DEFAULT '', title TEXT,
                is_group INTEGER DEFAULT 0, members TEXT DEFAULT '[]',
                n_msg INTEGER DEFAULT 0, start_ts INTEGER, end_ts INTEGER,
                sources TEXT DEFAULT '[]', created_at INTEGER, updated_at INTEGER);
            CREATE TABLE msg (
                msg_id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, account TEXT DEFAULT '',
                seq INTEGER, ts INTEGER, time_str TEXT, sender TEXT,
                is_self INTEGER DEFAULT 0, type TEXT, content TEXT, media TEXT,
                media_full TEXT, source_file TEXT, created_at INTEGER);
        """)
        conn.execute("INSERT INTO conv (conv_id, account, title) VALUES (?,?,?)",
                     ("默认账号::老王", "默认账号", "老王"))
        conn.commit()

        S.ensure_schema(conn)  # 不允许抛异常

        cols = {r[1] for r in conn.execute("PRAGMA table_info(conv)")}
        self.assertIn("username", cols)
        self.assertIn("sender_username",
                      {r[1] for r in conn.execute("PRAGMA table_info(msg)")})
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_conv_username", idx)
        # 老数据必须还在（补列不能重建表）
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM conv").fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
