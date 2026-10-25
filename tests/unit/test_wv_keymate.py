#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wv-keymate 密钥管家的单元测试。

覆盖四条验收线：
  1. 密钥契约的读写往返（passphrase.json / all_keys.json）
  2. 96hex 兼容切分（老 all_keys 的 values 形态与 dict 形态）
  3. 解密裁判的正反用例（真密钥解开且 sqlite 能开 / 假密钥拒绝）
  4. 私网地址白名单拒绝公网

解密裁判的夹具用「reserve=80 的 SQLCipher 风格库」：先手工铺一个空库
（页尾 80 字节是 reserve，sqlite 不往那写），再用 sqlite3 建表填充，
最后按 SQLCipher 4 页模型加密——这样正例走的是真实「解密→sqlite 试开」
路径，而不是绕过裁判的假断言。
"""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                "plugins", "wv-keymate"))

import keymate as K  # noqa: E402

try:
    from Crypto.Cipher import AES
except ImportError:  # pragma: no cover
    AES = None

PW = "a1b2c3d4" * 8                      # 64 hex 账号级 passphrase
ENC_KEY = "0f1e2d3c4b5a69788796a5b4c3d2e1f0" \
          "00112233445566778899aabbccddeeff"  # 64 hex 已派生 AES key
WRONG_PW = "00" * 32
HEX96 = ENC_KEY + "00112233445566778899aabbccddeeff"


# ------------------------------------------------------------- 夹具工具
def _make_reserve80_db(path: Path, rows: int = 5) -> None:
    """铺一个 reserve=80、页 4096 的可用 sqlite 库（SQLCipher 明文形态）。"""
    page_sz = 4096
    hdr = bytearray(100)
    hdr[0:16] = b"SQLite format 3\x00"
    struct.pack_into(">H", hdr, 16, page_sz)
    hdr[18] = 1
    hdr[19] = 1
    hdr[20] = 80          # reserved bytes per page —— SQLCipher 的关键
    hdr[21] = 64
    hdr[22] = 32
    hdr[23] = 32
    struct.pack_into(">I", hdr, 24, 1)
    struct.pack_into(">I", hdr, 28, 1)
    struct.pack_into(">I", hdr, 40, 1)
    struct.pack_into(">I", hdr, 44, 4)
    hdr[56] = 1
    struct.pack_into(">I", hdr, 92, 1)
    struct.pack_into(">I", hdr, 96, 3045000)
    page = bytearray(page_sz)
    page[:100] = hdr
    page[100] = 0x0D                       # sqlite_master 空叶子页
    struct.pack_into(">H", page, 105, page_sz - 80)
    path.write_bytes(bytes(page))

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA page_size=4096")
        conn.execute("CREATE TABLE Msg (id INTEGER PRIMARY KEY, body TEXT)")
        conn.executemany("INSERT INTO Msg VALUES (?,?)",
                         [(i, "hello %d" % i) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()


def _encrypt_db(plain: bytes, aes_key: bytes, salt: bytes) -> bytes:
    """按 SQLCipher 4 页模型加密（decrypt_sqlcipher4 的逆运算）。"""
    out = bytearray()
    n = len(plain) // K.PAGE_SZ
    for pi in range(n):
        page = plain[pi * K.PAGE_SZ:(pi + 1) * K.PAGE_SZ]
        iv = os.urandom(16)
        if pi == 0:
            ct = AES.new(aes_key, AES.MODE_CBC, iv).encrypt(page[16:K.IV_OFF])
            out += salt + ct + iv + b"\x00" * 64
        else:
            ct = AES.new(aes_key, AES.MODE_CBC, iv).encrypt(page[:K.IV_OFF])
            out += ct + iv + b"\x00" * 64
    return bytes(out)


def _derive(passphrase_hex: str, salt: bytes) -> bytes:
    return K.derive_aes_key(passphrase_hex, salt)


@unittest.skipIf(AES is None, "需要 pycryptodome")
class TestDecryptJudge(unittest.TestCase):
    """解密裁判的正反用例。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="wv_keymate_test_"))
        self.salt = bytes.fromhex("aabbccddeeff00112233445566778899")

    def _enc_fixture(self, name: str, aes_key: bytes) -> Path:
        plain = self.td / (name + ".plain.db")
        _make_reserve80_db(plain)
        blob = plain.read_bytes()
        enc = self.td / (name + ".db")
        enc.write_bytes(_encrypt_db(blob, aes_key, self.salt))
        return enc

    def test_passphrase_opens_real_db(self):
        """正例：passphrase 派生出的密钥必须真的解开库、sqlite 能开。"""
        aes = _derive(PW, self.salt)
        db = self._enc_fixture("ok", aes)
        self.assertTrue(K.judge_db(db, passphrase_hex=PW))
        # 顺带确认裁判不是空转：解开的库里真有数据
        salt = K.read_salt(db)
        plain = K.decrypt_sqlcipher4(db.read_bytes(), _derive(PW, salt))
        self.assertTrue(K.try_open_sqlite(plain))
        self.assertIsNotNone(plain)

    def test_wrong_passphrase_rejected(self):
        """反例：错 passphrase 必须被拒。"""
        aes = _derive(PW, self.salt)
        db = self._enc_fixture("bad_pw", aes)
        self.assertFalse(K.judge_db(db, passphrase_hex=WRONG_PW))

    def test_enc_key_opens_real_db(self):
        """正例：已派生 enc_key 直接可用（不再走 KDF）。"""
        aes = bytes.fromhex(ENC_KEY)
        db = self._enc_fixture("ok_ek", aes)
        self.assertTrue(K.judge_db(db, enc_key_hex=ENC_KEY))

    def test_wrong_enc_key_rejected(self):
        """反例：错 enc_key 必须被拒。"""
        aes = bytes.fromhex(ENC_KEY)
        db = self._enc_fixture("bad_ek", aes)
        self.assertFalse(K.judge_db(db, enc_key_hex="11" * 32))

    def test_garbage_blob_rejected(self):
        """反例：非库文件（含伪造头）必须被拒。"""
        p = self.td / "garbage.db"
        p.write_bytes(b"SQLite format 3\x00" + os.urandom(8192 - 16))
        self.assertFalse(K.judge_db(p, passphrase_hex=PW))
        self.assertFalse(K.try_open_sqlite(b"SQLite format 3\x00" + os.urandom(4000)))

    def test_short_file_rejected(self):
        p = self.td / "tiny.db"
        p.write_bytes(b"\x00" * 100)
        self.assertFalse(K.judge_db(p, passphrase_hex=PW))


# ------------------------------------------------------------ 96hex 兼容
class TestLegacy96Hex(unittest.TestCase):
    """旧 all_keys 的 96hex 拼接与 dict 两种形态都要能切分归一。"""

    def test_split_96hex(self):
        got = K.split_legacy_96hex(HEX96.upper())
        self.assertEqual(got["enc_key"], ENC_KEY)
        self.assertEqual(got["salt"], "00112233445566778899aabbccddeeff")

    def test_split_96hex_bad_len(self):
        with self.assertRaises(ValueError):
            K.split_legacy_96hex(ENC_KEY)   # 64 不是 96

    def test_normalize_entry_forms(self):
        salt = "00112233445566778899aabbccddeeff"
        cases = [
            HEX96,                                    # 老：96hex 拼接字符串
            HEX96.upper(),                            # 大小写不敏感
            {"enc_key": ENC_KEY, "salt": salt},       # 新契约 dict
            {"enc_key": HEX96},                       # dict 里塞拼接（也要切）
            ENC_KEY,                                  # 裸 enc_key（salt 留空）
        ]
        for val in cases:
            ent = K.normalize_entry(val)
            self.assertIsNotNone(ent, val)
            self.assertEqual(ent["enc_key"], ENC_KEY, val)
        self.assertEqual(K.normalize_entry(cases[0])["salt"], salt)
        self.assertEqual(K.normalize_entry(cases[2])["salt"], salt)
        self.assertEqual(K.normalize_entry(cases[3])["salt"], salt)
        self.assertEqual(K.normalize_entry(ENC_KEY)["salt"], "")

    def test_normalize_entry_rejects_junk(self):
        self.assertIsNone(K.normalize_entry("zz" * 32))
        self.assertIsNone(K.normalize_entry({"enc_key": "short"}))
        self.assertIsNone(K.normalize_entry(12345))

    def test_parse_all_keys_old_and_new(self):
        old = {"db_dir": "/data/x", "keys": {"message/message_0.db": HEX96}}
        new = {"db_dir": "/data/x",
               "keys": {"message/message_0.db": {"enc_key": ENC_KEY, "salt": "00112233445566778899aabbccddeeff"}}}
        flat = {"message/message_0.db": HEX96}   # 更老的扁平形态
        for doc in (old, new, flat):
            got = K.parse_key_payload(doc)
            self.assertIn("message/message_0.db", got["keys"], doc)
            self.assertEqual(got["keys"]["message/message_0.db"]["enc_key"], ENC_KEY)

    def test_parse_passphrase_forms(self):
        got = K.parse_key_payload(PW)
        self.assertEqual(got["passphrase"], PW.lower())
        got2 = K.parse_key_payload({"passphrase": PW, "wxid": "wxid_abc",
                                    "source": "scan", "wechat_version": "4.0.3.36"})
        self.assertEqual(got2["passphrase"], PW.lower())
        self.assertEqual(got2["meta"]["wxid"], "wxid_abc")
        self.assertEqual(got2["meta"]["source"], "scan")

    def test_parse_bare_96_is_universal(self):
        got = K.parse_key_payload(HEX96)
        self.assertEqual(got["universal"]["enc_key"], ENC_KEY)
        self.assertEqual(got["passphrase"], "")


# -------------------------------------------------------------- 契约往返
class TestContractRoundtrip(unittest.TestCase):
    """passphrase.json / all_keys.json 写出来再读回去必须一致。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="wv_keymate_contract_"))
        self.keys = {
            "message/message_0.db": {"enc_key": ENC_KEY,
                                     "salt": "00112233445566778899aabbccddeeff"},
            "session/session.db": {"enc_key": ENC_KEY, "salt": "ffeeddccbbaa99887766554433221100"},
        }

    def test_write_then_read(self):
        written = K.write_contract(
            self.td, db_dir="/data/xwechat_files/wxid_abc_1f2e/db_storage",
            keys=self.keys, passphrase=PW, wxid="wxid_abc",
            source="scan", wechat_version="4.0.3.36")
        self.assertIn("passphrase.json", written)
        self.assertIn("all_keys.json", written)

        bundle = K.load_contract(self.td)
        self.assertEqual(bundle["passphrase"], PW)
        self.assertEqual(bundle["keys"], self.keys)
        self.assertEqual(bundle["meta"]["wxid"], "wxid_abc")
        self.assertEqual(bundle["meta"]["source"], "scan")

        # 契约字段名/形态也要钉死（消费端按这个读）
        pdoc = json.loads((self.td / "passphrase.json").read_text(encoding="utf-8"))
        self.assertEqual(len(pdoc["passphrase"]), 64)
        self.assertEqual(pdoc["source"], "scan")
        self.assertTrue(pdoc["captured_at"])
        adoc = json.loads((self.td / "all_keys.json").read_text(encoding="utf-8"))
        self.assertEqual(adoc["keys"]["message/message_0.db"]["enc_key"], ENC_KEY)
        self.assertEqual(len(adoc["keys"]["message/message_0.db"]["salt"]), 32)

    def test_96hex_normalized_on_write(self):
        """写契约时旧 96hex 值也要被切成 dict 形态（统一出口）。"""
        doc = K.build_all_keys_doc("/data/x", {"a/b.db": HEX96})
        self.assertEqual(doc["keys"]["a/b.db"],
                         {"enc_key": ENC_KEY, "salt": "00112233445566778899aabbccddeeff"})

    def test_passphrase_doc_source_clamped(self):
        doc = K.build_passphrase_doc(PW, source="whatever")
        self.assertEqual(doc["source"], "manual")


# -------------------------------------------------------------- 私网白名单
class TestPrivateHost(unittest.TestCase):
    """绝不开公网：私网白名单放行/拒绝。"""

    def test_private_allowed(self):
        for host in ("192.168.1.10", "10.0.0.5", "172.16.0.1", "172.31.255.255",
                     "127.0.0.1", "localhost", "127.1.2.3"):
            self.assertTrue(K.is_private_host(host), host)

    def test_public_rejected(self):
        for host in ("8.8.8.8", "1.1.1.1", "172.15.0.1", "172.32.0.1",
                     "192.169.0.1", "11.0.0.1", "169.254.1.1", "0.0.0.0",
                     "", "example.com"):
            self.assertFalse(K.is_private_host(host), host)

    def test_ensure_raises_on_public(self):
        with self.assertRaises(ValueError) as ctx:
            K.ensure_private_host("8.8.8.8")
        self.assertIn("绝不开公网", str(ctx.exception))
        self.assertEqual(K.ensure_private_host("192.168.0.1"), "192.168.0.1")

    def test_deliver_and_trigger_refuse_public(self):
        """投递/触发同步的入口必须在动网络前就拒绝公网目标。"""
        td = Path(tempfile.mkdtemp(prefix="wv_keymate_net_"))
        (td / "all_keys.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            K.deliver_keys(td, "8.8.8.8")
        with self.assertRaises(ValueError):
            K.trigger_ingest("8.8.8.8")

    def test_mask_secret_never_leaks(self):
        m = K.mask_secret(PW)
        self.assertIn(PW[:6], m)
        self.assertNotIn(PW[6:20], m)
        self.assertIn("len=64", m)


# ---------------------------------------------------------------- 探测整合
@unittest.skipIf(AES is None, "需要 pycryptodome")
class TestProbeIntegration(unittest.TestCase):
    """probe_db_dir：passphrase 与 enc_key 两条路都要能逐库命中。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="wv_keymate_probe_"))
        self.db_dir = self.td / "db_storage"
        (self.db_dir / "message").mkdir(parents=True)
        (self.db_dir / "session").mkdir(parents=True)
        self.salt1 = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.salt2 = bytes.fromhex("ffeeddccbbaa99887766554433221100")
        self._put("message/message_0.db", self.salt1, _derive(PW, self.salt1))
        self._put("session/session.db", self.salt2, _derive(PW, self.salt2))

    def _put(self, rel: str, salt: bytes, aes: bytes):
        plain = self.td / (rel.replace("/", "_") + ".plain")
        _make_reserve80_db(plain)
        (self.db_dir / rel).write_bytes(
            _encrypt_db(plain.read_bytes(), aes, salt))

    def test_passphrase_covers_all_dbs(self):
        rep = K.probe_db_dir(self.db_dir, passphrase=PW)
        self.assertEqual(rep["ok"], ["message/message_0.db", "session/session.db"])
        self.assertEqual(rep["failed"], [])
        # 命中后 keys 里登记的是**派生出的** enc_key（契约口径）
        self.assertEqual(len(rep["keys"]), 2)
        for rel, ent in rep["keys"].items():
            self.assertEqual(len(ent["enc_key"]), 64, rel)

    def test_enc_key_snapshot_fallback(self):
        keys = {
            "message/message_0.db": {"enc_key": ENC_KEY, "salt": self.salt1.hex()},
        }
        # 两个库用同一 enc_key 加密（微信全库同 AES key 的形态）
        self._put("message/message_0.db", self.salt1, bytes.fromhex(ENC_KEY))
        self._put("session/session.db", self.salt2, bytes.fromhex(ENC_KEY))
        rep = K.probe_db_dir(self.db_dir, keys=keys)
        self.assertIn("message/message_0.db", rep["ok"])
        self.assertIn("session/session.db", rep["ok"])

    def test_wrong_passphrase_reports_failure(self):
        rep = K.probe_db_dir(self.db_dir, passphrase=WRONG_PW)
        self.assertEqual(rep["ok"], [])
        self.assertEqual(rep["failed"], ["message/message_0.db", "session/session.db"])


if __name__ == "__main__":
    unittest.main()
