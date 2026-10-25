#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""密钥链路修复的单元测试（G1 / G2 / G3 + 解密裁判）。

背景（2026-10 密钥链路审计，这四个 bug 让「拿到微信密钥也喂不进 vault」）：
  G1  all_keys.json 旧格式 96hex 被整段当 enc_key，decrypt_one 因长度 != 64 拒绝
  G2  有 passphrase 时未登记的库仍被跳过 —— 微信新增 message_23.db 永远解不开
  G3  all_keys.json 缺失直接退出 —— 只有 passphrase.json 的用户根本起不来
  裁判 HMAC 公式（salt XOR 0x3A / 轮数 2）错一处就是「扫描 0 命中」假阴性，
      故改为解密裁判：按 decrypt_one 页模型解 page 0，明文是合法 SQLite 才算命中。
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "archiver"))

import wv_sync as S  # noqa: E402

try:
    from Crypto.Cipher import AES as _TestAES
    _HAS_CRYPTO = True
except ImportError:
    _TestAES = None
    _HAS_CRYPTO = False

PAGE_SZ = 4096
RESERVE = 80
IV_OFF = 4016
HMAC_OFF = 4032

KEY_A = bytes(range(32))                       # 真密钥
KEY_B = bytes(range(32, 64))                   # 假密钥
SALT = bytes(range(16))
IV = bytes(range(16, 32))

PASS_OK = "ab" * 32                            # 合法 64hex passphrase
HEX96 = "aa" * 32 + "bb" * 16                  # 旧格式：enc_key ++ salt 拼接


def make_plain_page0() -> bytes:
    """合法 SQLite page 0 明文（reserved=80，SQLCipher 4 布局）。"""
    page = bytearray(PAGE_SZ)
    page[0:16] = b"SQLite format 3\x00"
    page[16:18] = struct.pack(">H", PAGE_SZ)   # page size
    page[18] = 1                               # write version
    page[19] = 1                               # read version
    page[20] = RESERVE                         # reserved bytes per page
    page[21] = 64                              # max payload fraction（SQLite 固定值）
    page[22] = 32                              # min payload fraction
    page[23] = 32                              # leaf payload fraction
    struct.pack_into(">I", page, 24, 1)        # change counter
    struct.pack_into(">I", page, 28, 1)        # db size in pages
    struct.pack_into(">I", page, 40, 1)        # schema cookie
    struct.pack_into(">I", page, 44, 4)        # schema format
    struct.pack_into(">I", page, 56, 1)        # text encoding = UTF-8
    struct.pack_into(">I", page, 92, 1)        # version-valid-for
    struct.pack_into(">I", page, 96, 3045000)
    return bytes(page)


def make_plain_empty_db() -> bytes:
    """一页空库明文（sqlite_master 零行），可被 sqlite3 直接打开。"""
    page = bytearray(make_plain_page0())
    page[100] = 0x0D                           # leaf table btree
    struct.pack_into(">H", page, 105, PAGE_SZ - RESERVE)  # cell content start
    return bytes(page)


def encrypt_page0(plain_page: bytes, key: bytes) -> bytes:
    """按 SQLCipher 4 页模型造加密页 0：salt || AES-CBC(pt) || IV || HMAC。"""
    pt = plain_page[16:IV_OFF]
    ct = _TestAES.new(key, _TestAES.MODE_CBC, IV).encrypt(pt)
    return SALT + ct + IV + b"\x00" * (PAGE_SZ - HMAC_OFF)


# ─────────────────────────────────────────────────────────────── G1 ──

class TestLoadKeys96HexSplit(unittest.TestCase):
    """G1：96hex 旧格式必须自动切成 enc_key(64) + salt(32)。"""

    def _load(self, payload: dict) -> dict:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "all_keys.json"
            p.write_text(json.dumps(payload), encoding="utf-8")
            return S.load_keys(p)

    def test_legacy_96hex_string_split(self):
        out = self._load({"keys": {"message/message_0.db": HEX96}})
        self.assertEqual(out["message/message_0.db"], ("aa" * 32, "bb" * 16))

    def test_contract_dict_passthrough(self):
        out = self._load({"db_dir": "x", "keys": {
            "message/message_0.db": {"enc_key": "aa" * 32, "salt": "bb" * 16}}})
        self.assertEqual(out["message/message_0.db"], ("aa" * 32, "bb" * 16))

    def test_legacy_96hex_in_dict_enc_key_split(self):
        """字典形态里 enc_key 误写成 96hex 也要切开（防御性兼容）。"""
        out = self._load({"keys": {
            "message/message_0.db": {"enc_key": HEX96, "salt": ""}}})
        self.assertEqual(out["message/message_0.db"], ("aa" * 32, "bb" * 16))

    def test_top_level_dict_form(self):
        """wv_keymem 输出风格：无 keys 包裹、条目是字典。"""
        out = self._load({"message/message_0.db": {
            "enc_key": "cc" * 32, "salt": "dd" * 16}})
        self.assertEqual(out["message/message_0.db"], ("cc" * 32, "dd" * 16))

    def test_bare_64hex_string(self):
        out = self._load({"keys": {"a.db": "ee" * 32}})
        self.assertEqual(out["a.db"], ("ee" * 32, ""))

    def test_garbage_entry_skipped(self):
        out = self._load({"keys": {"a.db": "zz", "b.db": 123,
                                   "c.db": {"enc_key": "nothex"}}})
        self.assertEqual(out, {})

    def test_db_dir_key_ignored(self):
        out = self._load({"db_dir": "C:/x", "keys": {"a.db": HEX96}})
        self.assertNotIn("db_dir", out)
        self.assertIn("a.db", out)


# ─────────────────────────────────────────────────────────────── G2 ──

@unittest.skipUnless(_HAS_CRYPTO, "需要 pycryptodome")
class TestDecryptAllWithPassphrase(unittest.TestCase):
    """G2：有 passphrase 时，未登记的库也要走 passphrase 分支解密。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.account = self.root / "wxid_test_1"
        self.db_dir = self.account / "db_storage"
        (self.db_dir / "message").mkdir(parents=True)
        self.plain = self.root / "plain"

    def tearDown(self):
        self._td.cleanup()

    def _add_db(self, rel: str) -> None:
        p = self.db_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(PAGE_SZ * 2))

    def test_unregistered_db_decrypted_with_passphrase(self):
        """微信日后新增的 message_23.db 不在 all_keys.json 里也必须能解。"""
        self._add_db("message/message_23.db")
        n = S.decrypt_all(self.account, self.plain, {}, passphrase_hex=PASS_OK)
        self.assertEqual(n, 1)
        self.assertTrue((self.plain / "message" / "message_23.db").is_file())

    def test_unregistered_db_skipped_without_passphrase(self):
        """没有 passphrase 也没有登记密钥 —— 只能跳过（原有行为）。"""
        self._add_db("message/message_23.db")
        n = S.decrypt_all(self.account, self.plain, {}, passphrase_hex="")
        self.assertEqual(n, 0)

    def test_registered_and_unregistered_both_decrypted(self):
        self._add_db("message/message_0.db")
        self._add_db("message/message_23.db")
        keys = {"message/message_0.db": ("aa" * 32, "bb" * 16)}
        n = S.decrypt_all(self.account, self.plain, keys, passphrase_hex=PASS_OK)
        self.assertEqual(n, 2)
        self.assertTrue((self.plain / "message" / "message_0.db").is_file())
        self.assertTrue((self.plain / "message" / "message_23.db").is_file())


# ─────────────────────────────────────────────────────────────── G3 ──

class TestKeyMaterialBootstrap(unittest.TestCase):
    """G3：passphrase.json 可用时，keys 文件缺失/为空都放行。"""

    def _write(self, td: str, name: str, payload: dict) -> Path:
        p = Path(td) / name
        p.write_text(json.dumps(payload), encoding="utf-8")
        return p

    def test_missing_keys_with_passphrase_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, "passphrase.json",
                        {"passphrase": PASS_OK, "wxid": "wxid_x"})
            keys, pw, err = S.load_key_material(Path(td) / "all_keys.json")
            self.assertEqual(err, "")
            self.assertEqual(keys, {})
            self.assertEqual(pw, PASS_OK)

    def test_missing_both_reports_error(self):
        with tempfile.TemporaryDirectory() as td:
            keys, pw, err = S.load_key_material(Path(td) / "all_keys.json")
            self.assertNotEqual(err, "")
            self.assertEqual(keys, {})
            self.assertEqual(pw, "")

    def test_empty_keys_file_with_passphrase_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "all_keys.json").write_text("", encoding="utf-8")
            self._write(td, "passphrase.json", {"passphrase": PASS_OK})
            keys, pw, err = S.load_key_material(Path(td) / "all_keys.json")
            self.assertEqual(err, "")
            self.assertEqual(keys, {})
            self.assertEqual(pw, PASS_OK)

    def test_empty_keys_file_without_passphrase_errors(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "all_keys.json").write_text("{}", encoding="utf-8")
            keys, pw, err = S.load_key_material(Path(td) / "all_keys.json")
            self.assertNotEqual(err, "")

    def test_keys_present_without_passphrase_is_ok(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, "all_keys.json", {
                "db_dir": "x",
                "keys": {"message/message_0.db": {"enc_key": "aa" * 32,
                                                  "salt": "bb" * 16}}})
            keys, pw, err = S.load_key_material(Path(td) / "all_keys.json")
            self.assertEqual(err, "")
            self.assertEqual(pw, "")
            self.assertEqual(len(keys), 1)

    def test_passphrase_wrong_length_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            self._write(td, "passphrase.json", {"passphrase": "abc"})
            keys, pw, err = S.load_key_material(Path(td) / "all_keys.json")
            self.assertNotEqual(err, "")   # 64hex 校验不过 = 等于没有
            self.assertEqual(pw, "")


# ────────────────────────────────────────────────────── 解密裁判 ──

@unittest.skipUnless(_HAS_CRYPTO, "需要 pycryptodome")
class TestDecryptJudge(unittest.TestCase):
    """解密裁判正反用例：真密钥必须过，假密钥必须拒。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _enc_db(self, name: str, plain: bytes, key: bytes) -> Path:
        p = self.root / name
        p.write_bytes(encrypt_page0(plain, key) + b"\x00" * PAGE_SZ)
        return p

    def test_page0_judge_accepts_true_key(self):
        db = self._enc_db("t.db", make_plain_page0(), KEY_A)
        self.assertTrue(S.verify_key_by_decrypt(db, KEY_A.hex()))

    def test_page0_judge_rejects_wrong_key(self):
        db = self._enc_db("t.db", make_plain_page0(), KEY_A)
        self.assertFalse(S.verify_key_by_decrypt(db, KEY_B.hex()))

    def test_page0_judge_rejects_garbage_file(self):
        p = self.root / "g.db"
        p.write_bytes(os.urandom(PAGE_SZ))
        self.assertFalse(S.verify_key_by_decrypt(p, KEY_A.hex()))

    def test_page0_judge_rejects_short_file(self):
        p = self.root / "s.db"
        p.write_bytes(b"short")
        self.assertFalse(S.verify_key_by_decrypt(p, KEY_A.hex()))

    def test_page0_judge_rejects_bad_hex(self):
        db = self._enc_db("t.db", make_plain_page0(), KEY_A)
        self.assertFalse(S.verify_key_by_decrypt(db, "zz"))
        self.assertFalse(S.verify_key_by_decrypt(db, "aa" * 16))   # 32hex 太短

    def test_full_mode_opened_by_sqlite3(self):
        """金标准：解全文件后 sqlite3.connect() 能试开才认。"""
        db = self._enc_db("full.db", make_plain_empty_db(), KEY_A)
        self.assertTrue(S.verify_key_by_decrypt(db, KEY_A.hex(), full=True))
        self.assertFalse(S.verify_key_by_decrypt(db, KEY_B.hex(), full=True))

    def test_looks_like_sqlite_page0_fields(self):
        self.assertTrue(S.looks_like_sqlite_page0(make_plain_page0()))
        bad = bytearray(make_plain_page0())
        bad[22] = 99                               # 破坏载荷分数固定值
        self.assertFalse(S.looks_like_sqlite_page0(bytes(bad)))
        self.assertFalse(S.looks_like_sqlite_page0(b"\x00" * PAGE_SZ))

    def test_verify_page0_key_bytes_level(self):
        page0 = encrypt_page0(make_plain_page0(), KEY_A)
        self.assertTrue(S.verify_page0_key(page0, KEY_A))
        self.assertFalse(S.verify_page0_key(page0, KEY_B))


class TestKeymemDerive(unittest.TestCase):
    """wv_keymem.derive 的派生公式（2 轮 + salt^0x3A），防止回退到 256000 轮。"""

    def test_mac_uses_two_rounds_and_xor_salt(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
        import wv_keymem as K
        import hashlib
        aes, mac = K.derive(KEY_A, SALT)
        self.assertEqual(aes, KEY_A)               # enc_key 已是最终 AES key
        mac_salt = bytes(b ^ 0x3A for b in SALT)
        self.assertEqual(
            mac, hashlib.pbkdf2_hmac("sha512", KEY_A, mac_salt, 2, dklen=32))
        self.assertEqual(K.MAC_KDF_ITER, 2)

    @unittest.skipUnless(_HAS_CRYPTO, "需要 pycryptodome")
    def test_verify_key_delegates_to_judge(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
        import wv_keymem as K
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "t.db"
            db.write_bytes(encrypt_page0(make_plain_page0(), KEY_A))
            self.assertTrue(K.verify_key(KEY_A, db))
            self.assertFalse(K.verify_key(KEY_B, db))


if __name__ == "__main__":
    unittest.main()
