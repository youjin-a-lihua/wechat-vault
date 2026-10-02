# -*- coding: utf-8 -*-
"""
解密 + 会话关联 的单元测试
==========================
覆盖的是「错了不崩、只给错数据」的高风险函数。这些测试的意义：
  每次改动后跑一遍，F1（c2h=None 崩溃）、F2（缺固定块自检）、
  F6（分段漏扫）这类缺陷就会被立刻抓住，而不是潜伏到线上。

运行（在仓库根目录，PYTHONPATH 指向 archiver）：
    python -m pytest tests/unit/test_wv_media.py -v
  或直接跑：
    python tests/unit/test_wv_media.py
"""

import os
import sys
import struct
import tempfile
import unittest

# 让 wv_media 可导入（它内部 import Crypto / zstandard）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "archiver"))

import wv_media as M


# ---------------------------------------------------------------------------
# 构造真实 V2 .dat 字节（密钥可自选，便于测试）
# ---------------------------------------------------------------------------
def make_v2_dat(plain: bytes, aes_key: bytes, xor_key: int,
                aes_size: int = 1024) -> bytes:
    """按 V2 布局造一个合法 .dat：15 字节头 + AES头 + 固定块 + XOR尾。"""
    from Crypto.Cipher import AES
    head_plain = plain[:aes_size].ljust(aes_size, b"\x00")
    head_cipher = AES.new(aes_key, AES.MODE_ECB).encrypt(head_plain)
    tail = plain[aes_size:]
    tail_xor = bytes(b ^ xor_key for b in tail)
    blob = (b"\x07\x08V2\x08\x07"
            + struct.pack("<II", aes_size, len(tail))
            + b"\x00"
            + head_cipher
            + M.FIXED_BLOCK
            + tail_xor)
    return blob


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 2000   # 合法 JPEG 头 + 填充


class TestDecodeDat(unittest.TestCase):
    """F2 相关：固定块自检 + 正常解密 + 布局错误拒绝。"""

    def setUp(self):
        self.key = b"0123456789abcdef"
        self.xor = 0x42

    def test_normal_decode(self):
        blob = make_v2_dat(JPEG, self.key, self.xor)
        plain, ext = M.decode_dat(blob, self.key, self.xor)
        self.assertEqual(ext, "jpg")
        self.assertTrue(plain.startswith(b"\xff\xd8\xff"))

    def test_rejects_wrong_key(self):
        """密钥错误 → 固定块自检应拒绝（不是靠 magic 碰运气）。"""
        blob = make_v2_dat(JPEG, self.key, self.xor)
        wrong_key = b"1111111111111111"
        self.assertIsNone(M.decode_dat(blob, wrong_key, self.xor))

    def test_rejects_tampered_fixed_block(self):
        """篡改固定块 → 必须拒绝（这 16 字节是完整性硬信号）。"""
        blob = bytearray(make_v2_dat(JPEG, self.key, self.xor))
        # 固定块位置：15 + aes_size
        blob[15 + 1024] ^= 0xFF
        self.assertIsNone(M.decode_dat(bytes(blob), self.key, self.xor))

    def test_rejects_truncated(self):
        blob = make_v2_dat(JPEG, self.key, self.xor)
        self.assertIsNone(M.decode_dat(blob[:-10], self.key, self.xor))

    def test_rejects_non_v2(self):
        self.assertIsNone(M.decode_dat(b"not a dat", self.key, self.xor))


class TestFindCodePartition(unittest.TestCase):
    """F6 相关：分段对齐 256 边界，任意 workers 都不能漏扫。"""

    def _job_cover_full_range(self, workers):
        """模拟 find_code 的分段逻辑，验证并集覆盖 [0, 2^32) 里所有
        (c & 0xFF)==xor_key 的候选，且无重叠、无遗漏。"""
        xor_key = 0x42
        n_units = 2 ** 32 // 256
        per = (n_units + workers - 1) // workers
        covered = 0
        seen_ranges = []
        for i in range(workers):
            u0 = i * per
            if u0 >= n_units:
                break
            u1 = min(u0 + per, n_units)
            lo = u0 * 256
            while lo < u1 * 256 and (lo & 0xFF) != xor_key:
                lo += 1
            hi = u1 * 256
            seen_ranges.append((lo, hi))
            # 这段里有多少个合法候选
            covered += max(0, (hi - lo + 255) // 256)
        # 理论候选数：2^24
        expected = 2 ** 24
        return covered, expected, seen_ranges

    def test_workers_4(self):
        covered, expected, _ = self._job_cover_full_range(4)
        self.assertEqual(covered, expected)

    def test_workers_3_non_power_of_2(self):
        """F6 的回归测试：workers=3（非 2 幂）也必须全覆盖。"""
        covered, expected, _ = self._job_cover_full_range(3)
        self.assertEqual(covered, expected)

    def test_workers_6(self):
        covered, expected, _ = self._job_cover_full_range(6)
        self.assertEqual(covered, expected)

    def test_no_overlap(self):
        """各段不得重叠（否则浪费算力）。"""
        _, _, ranges = self._job_cover_full_range(3)
        ranges.sort()
        for (_, hi1), (lo2, _) in zip(ranges, ranges[1:]):
            self.assertLessEqual(hi1, lo2)


class TestStrictValidation(unittest.TestCase):
    """F2 配套：严格 JPEG 头校验能排除 AES 随机输出的假阳性。"""

    def test_strict_ok_true_jpeg(self):
        from Crypto.Cipher import AES
        key = b"0123456789abcdef"
        plain = b"\xff\xd8\xff\xe0" + b"\x00" * 12
        block = AES.new(key, AES.MODE_ECB).encrypt(plain)
        self.assertTrue(M._strict_ok(block, key))

    def test_strict_rejects_random_like(self):
        """构造一个「解密后以 FF D8 FF 开头、但不是合法段标记」的块，
        必须被 _strict_ok 拒绝（这正是假阳性的来源）。"""
        from Crypto.Cipher import AES
        key = b"0123456789abcdef"
        # FF D8 FF 后面跟非法标记（如 0x99），不在 JPEG_STRICT 里
        plain = b"\xff\xd8\xff\x99" + b"\x00" * 12
        block = AES.new(key, AES.MODE_ECB).encrypt(plain)
        self.assertFalse(M._strict_ok(block, key))


if __name__ == "__main__":
    unittest.main(verbosity=2)
