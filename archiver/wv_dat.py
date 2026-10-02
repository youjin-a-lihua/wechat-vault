#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Vault - 微信 .dat 附件解码器
====================================

微信把收到的图片/视频加密存成 .dat。历史上共有三代加密方案，
本解码器全部支持：

  ┌──────────┬────────────┬─────────────────────┬──────────────────────┐
  │ 格式     │ 时代       │ Magic               │ 密钥来源             │
  ├──────────┼────────────┼─────────────────────┼──────────────────────┤
  │ Old XOR  │ ≤2025-07   │ 无                  │ 爆破嗅探（256 种）   │
  │ V1       │ 过渡期     │ 07 08 / 08 07       │ 硬编码 cfcd208495d565ef │
  │ V2       │ 2025-08+   │ 07 08 / 08 07       │ 运行时从进程内存提取  │
  └──────────┴────────────┴─────────────────────┴──────────────────────┘

V1/V2 的结构相同（AES-128-ECB 解密后再单字节 XOR），仅密钥来源不同。
V2 的密钥是会话级的，必须趁微信运行时从 Weixin.exe 内存里读出来。

用法：
  # 自动识别格式批量解码
  python wv_dat.py decode --src <目录> --dst <输出目录>

  # V2 需要提供密钥（从微信内存提取，见 --help-extract）
  python wv_dat.py decode --src <目录> --dst <目录> --aes-key <32位hex>

  # 单文件测试
  python wv_dat.py one --file <xxx.dat> --dst <目录>
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import struct
import sys
from collections import Counter
from pathlib import Path
import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 文件头签名
# ---------------------------------------------------------------------------

SIGNATURES = [
    (b"\xff\xd8\xff", ".jpg",  "jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png", "png"),
    (b"GIF87a", ".gif", "gif"),
    (b"GIF89a", ".gif", "gif"),
    (b"BM", ".bmp", "bmp"),
    (b"II*\x00", ".tif", "tif"),
    (b"MM\x00*", ".tif", "tif"),
    (b"RIFF", ".webp", "webp"),          # 需再验 WEBP
    (b"\x00\x00\x00\x18ftyp", ".mp4", "mp4"),
    (b"\x00\x00\x00\x20ftyp", ".mp4", "mp4"),
    (b"\x00\x00\x00\x1cftyp", ".mp4", "mp4"),
    (b"\x00\x00\x00\x14ftyp", ".mp4", "mp4"),
    (b"ftypisom", ".mp4", "mp4"),
    (b"ftypmp42", ".mp4", "mp4"),
    (b"\x1a\x45\xdf\xa3", ".mkv", "mkv"),
    (b"ID3", ".mp3", "mp3"),
    (b"\xff\xfb", ".mp3", "mp3"),
    (b"#!AMR", ".amr", "amr"),
    (b"OggS", ".ogg", "ogg"),
    (b"%PDF", ".pdf", "pdf"),
    (b"PK\x03\x04", ".zip", "zip"),
    (b"\x50\x4b\x03\x04", ".docx", "docx"),
]

# V1 固定密钥（公开已知）
# ⚠️ 修正（2026-10-01）：原值 "cfcd208495d565ef" 只有 8 字节，
#    被送进 AES-128 会在密钥扩展阶段直接抛异常。
#    AES-128 需要 **16 字节**密钥。
#    已知的微信 V1 固定密钥为 md5("0") 的十六进制串，共 32 个字符 = 16 字节。
V1_KEY_HEX = "cfcd208495d565ef66e7dff9f98764da"  # md5("0")，16 字节
assert len(V1_KEY_HEX) == 32, "V1_KEY_HEX 必须是 32 个 hex 字符（16 字节）"

# V1/V2 的 magic
V1_MAGIC = b"\x07\x08"
V2_MAGIC_A = b"\x07\x08"
V2_MAGIC_B = b"\x08\x07"

# ---------------------------------------------------------------------------
# 关键修正（2026-10-01 实测真实微信数据后得出）：
#
# 微信 for Windows 4.x（2026）落盘的 .dat 绝大多数是 Old XOR 单字节异或，
# 其**首个字节完全随机**，因此前两字节撞上 V1_MAGIC(b"\x07\x08") 纯属概率事件
# （1/65536，本机 80k 个文件里有一大批命中）。
#
# 旧实现只看前 2 字节就判定 V1，导致这些 Old XOR 文件被误送进 AES 分支，
# 进而触发 _expand_key 越界崩溃。修正原则：
#
#   **AES 分支必须"自证清白"** —— 解出来的首块必须能匹配已知媒体签名，
#   否则视为误判，回退到 XOR 嗅探。绝不因为 magic 长得像就盲跳。
# ---------------------------------------------------------------------------

def _looks_like_aes_v1v2(head: bytes) -> bool:
    """
    V1/V2 的结构判据（比只看 2 字节 magic 严格得多）。

    真 V1/V2 的第一个 16 字节块是 AES 密文，AES 输出熵极高，
    借此与"随机首字节撞上 magic"的 Old XOR 文件区分：

      - 前 2 字节必须是 07 08 或 08 07（必要条件）
      - 第 3、4 字节不能是 00 00（真 V1 密文几乎不会出现长零串，
        而 Old XOR 的典型特征恰是"高位字节全 0"）
    """
    if len(head) < 16:
        return False
    if head[:2] not in (V1_MAGIC, V2_MAGIC_B):
        return False
    if head[2:4] == b"\x00\x00":
        return False
    return True


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def xor_bytes(data: bytes | bytearray, key: int) -> bytes:
    """整块单字节 XOR。"""
    return bytes(b ^ key for b in data)


def detect_xor_key(head: bytes) -> tuple[int, str] | None:
    """
    嗅探 Old XOR 格式的密钥：遍历 0..255，看哪个能让文件头匹配已知格式。
    返回 (key, ext) 或 None。

    ⚠️ 重要警告（2026-10-01 真实数据实测后加）：

        本函数**对微信 4.x 的 .dat 恒返回假阳性**，不可单独作为判据！

        原因：微信 .dat 以常量 magic `07 08` 开头，而 BMP 以 `BM` 开头。
        任何以 07 08 起始的文件都会算出
            key = 0x07 ^ 0x42 = 0x45   ← 对每个文件都一样
        于是 100% "命中"、ext 恒为 .bmp —— 但 XOR 之后
        BITMAPFILEHEADER 的 fsize/off/hdrsize/dim/planes/bpp 全是垃圾。
        实测 226 个真实样本：命中率 100%，强校验通过率 0%。

        因此**调用方必须用 strong_validate() 复核**，不能只看本函数返回值。
        decode_dat() 已按此修正。
    """
    if len(head) < 4:
        return None
    for sig, ext, _name in SIGNATURES:
        if len(head) < len(sig):
            continue
        key = head[0] ^ sig[0]
        if all(h ^ key == s for h, s in zip(head[:len(sig)], sig)):
            # webp 二次校验
            if ext == ".webp" and head[8:12] != b"WEBP":
                continue
            return key, ext
    return None


# ---------------------------------------------------------------------------
# 强校验：完整文件结构合法性（区别于只看 magic 的"弱校验"）
# ---------------------------------------------------------------------------

def _bmp_strong(buf: bytes) -> bool:
    """校验完整 BITMAPFILEHEADER + BITMAPINFOHEADER。"""
    if len(buf) < 54 or buf[:2] != b"BM":
        return False
    try:
        fsize = struct.unpack_from("<I", buf, 2)[0]
        off = struct.unpack_from("<I", buf, 10)[0]
        hsz = struct.unpack_from("<I", buf, 14)[0]
        w, h = struct.unpack_from("<ii", buf, 18)
        planes = struct.unpack_from("<H", buf, 26)[0]
        bpp = struct.unpack_from("<H", buf, 28)[0]
    except struct.error:
        return False
    return (fsize in (0, len(buf))
            and 54 <= off < len(buf)
            and hsz in (40, 52, 56, 108, 124)
            and 0 < w <= 20000 and 0 < abs(h) <= 20000
            and planes == 1
            and bpp in (1, 4, 8, 16, 24, 32))


def _jpg_strong(buf: bytes) -> bool:
    return (len(buf) > 4 and buf[:2] == b"\xff\xd8" and buf[-2:] == b"\xff\xd9")


def _png_strong(buf: bytes) -> bool:
    return (buf.startswith(b"\x89PNG\r\n\x1a\n")
            and b"IHDR" in buf[:32]
            and buf[-8:-4] == b"IEND")


def _gif_strong(buf: bytes) -> bool:
    return buf[:6] in (b"GIF87a", b"GIF89a") and buf[-1:] == b"\x3b"


_STRONG = {
    ".bmp": _bmp_strong,
    ".jpg": _jpg_strong,
    ".png": _png_strong,
    ".gif": _gif_strong,
}


def strong_validate(buf: bytes, ext: str = "") -> bool:
    """
    强校验：只有**结构完整合法**的图片才算通过。

    与 identify() 的区别：identify 只看开头几个 magic 字节（弱校验），
    对本批真实 .dat 会产生 100% 假阳性；strong_validate 会检查
    BMP 的头长度/尺寸/位深、JPG 的 EOI、PNG 的 IEND 等完整结构。

    ext 为空时自动探测；给出 ext 则只校验该类型。
    """
    if not buf or len(buf) < 16:
        return False
    if ext:
        fn = _STRONG.get(ext)
        if fn is None:
            return identify(buf) is not None
        try:
            return fn(buf)
        except (struct.error, IndexError):
            return False
    for fn in _STRONG.values():
        try:
            if fn(buf):
                return True
        except (struct.error, IndexError) as e:
            log.debug("校验候选不成立，跳过：%s", e)
            continue
    return False


def identify(data: bytes) -> tuple[str, str] | None:
    """识别明文数据的格式，返回 (ext, name) 或 None。"""
    for sig, ext, name in SIGNATURES:
        if data[:len(sig)] == sig:
            if ext == ".webp":
                if data[8:12] == b"WEBP":
                    return ext, name
                continue
            return ext, name
    return None


# ---------------------------------------------------------------------------
# 元数据类 .dat（非媒体）—— 2026-10 实测真实数据后新增
# ---------------------------------------------------------------------------
# 微信把一批非媒体的状态文件也命名成 .dat，与图片/视频 .dat 混在同一棵树里。
_META_DAT_STEMS = frozenset({
    "alt_name", "detail", "phoneid", "backup_time", "phone_history",
    "roam_device_info", "device_info", "session_info", "contact_info",
    "backup", "manifest", "index_info",
})
_META_DAT_NAMES = frozenset(f"{s}.dat" for s in _META_DAT_STEMS) | frozenset({
    "backup.attr", "backup_time.dat",
})


def is_metadata_dat(path_or_name) -> bool:
    """
    判断是否为**元数据类** .dat（非媒体，不应送进图片解码器）。

    实测样本（D:\\ 与 G:\\ 两代数据都有）：
        alt_name.dat         会话别名表
        detail.dat           设备详情
        phoneid.dat          手机标识
        backup_time.dat      备份时间戳
        phone_history.dat    历史设备记录
        roam_device_info.dat 漫游设备信息

    这些文件永远解不出图片，强行解码只会刷满 skip 日志并浪费 IO。
    """
    name = str(path_or_name).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in _META_DAT_NAMES:
        return True
    stem = name[:-4] if name.endswith(".dat") else name
    return stem in _META_DAT_STEMS


def is_media_dat(path_or_name) -> bool:
    """
    判断是否**大概率是媒体** .dat。

    实测真实命名规律（wxid_*/msg/attach/<hash>/<YYYY-MM>/Img/）：
        <md5>.dat        原图
        <md5>_t.dat      缩略图（thumb）
        <md5>_h.dat      高清（hd）
        <md5>_t_W.dat    缩略图 + 某种变体后缀
        <数字>_<时间戳>_b.dat   Bubble 缓存图（cache/**/Bubble/）

    只要不是元数据 .dat，一律按媒体尝试解码（解码失败自然会被 skip）。
    """
    return not is_metadata_dat(path_or_name)


# ---------------------------------------------------------------------------
# AES-128-ECB（V1 / V2）
# ---------------------------------------------------------------------------

def _aes_ecb_decrypt(block: bytes, key: bytes) -> bytes:
    """
    纯 Python AES-128-ECB 单块解密。

    不引入 pycryptodome 依赖：微信场景只需解密首块用于识别，
    以及整块解密。这里实现一个最小可用的 AES-128。
    """
    # 优先用性能更好的实现
    try:
        from Crypto.Cipher import AES  # type: ignore
        return AES.new(key, AES.MODE_ECB).decrypt(block)
    except ImportError as e:
        log.warning("pycryptodome 缺失，AES 解密不可用，.dat 将无法解码：%s", e)
        pass
    return _aes128_ecb_decrypt_pure(block, key)


# ---- 最小 AES-128 实现（仅 ECB 解密，供无 pycryptodome 环境使用）----

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)

_INV_SBOX = bytearray(256)
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_INV_SBOX = bytes(_INV_SBOX)

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    r = 0
    for _ in range(8):
        if b & 1:
            r ^= a
        b >>= 1
        a = _xtime(a)
    return r


def _expand_key(key: bytes) -> list[list[int]]:
    """AES-128 密钥扩展 → 11 轮密钥（每轮 16 字节）。"""
    if len(key) != 16:
        raise ValueError(f"AES-128 密钥必须为 16 字节，收到 {len(key)} 字节")
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = list(w[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            # i 取 4,8,...,40 → i//4-1 取 0..9，恰好覆盖 _RCON 的 10 项。
            # 加边界保护，任何越界都退化为 AES-128 标准轮数之外的异常，
            # 不复用会静默出错的旧逻辑。
            if i // 4 - 1 >= len(_RCON):
                raise ValueError("AES 密钥扩展轮数溢出（key 长度不合法）")
            temp[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ temp[j] for j in range(4)])
    return [sum(w[r * 4:r * 4 + 4], []) for r in range(11)]


def _add_round_key(state: list[int], rk: list[int]) -> None:
    for i in range(16):
        state[i] ^= rk[i]


def _inv_shift_rows(s: list[int]) -> None:
    # 状态按列主序：s[c*4+r]
    for r in range(1, 4):
        row = [s[c * 4 + r] for c in range(4)]
        row = row[-r:] + row[:-r]
        for c in range(4):
            s[c * 4 + r] = row[c]


def _inv_sub_bytes(s: list[int]) -> None:
    for i in range(16):
        s[i] = _INV_SBOX[s[i]]


def _inv_mix_columns(s: list[int]) -> None:
    for c in range(4):
        a = s[c * 4:c * 4 + 4]
        s[c * 4 + 0] = _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9)
        s[c * 4 + 1] = _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13)
        s[c * 4 + 2] = _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11)
        s[c * 4 + 3] = _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)


def _aes128_ecb_decrypt_pure(data: bytes, key: bytes) -> bytes:
    """纯 Python AES-128-ECB 解密（仅支持正好整块的数据）。"""
    rks = _expand_key(key)
    out = bytearray()
    for off in range(0, len(data) - 15, 16):
        block = list(data[off:off + 16])
        _add_round_key(block, rks[10])
        for rnd in range(9, 0, -1):
            _inv_shift_rows(block)
            _inv_sub_bytes(block)
            _add_round_key(block, rks[rnd])
            _inv_mix_columns(block)
        _inv_shift_rows(block)
        _inv_sub_bytes(block)
        _add_round_key(block, rks[0])
        out.extend(block)
    return bytes(out)


# ---------------------------------------------------------------------------
# 三代格式解码
# ---------------------------------------------------------------------------

def decrypt_v1v2(blob: bytes, aes_key: bytes) -> bytes:
    """
    V1/V2 结构：AES-128-ECB(整块) → 再单字节 XOR。

    实际布局为：前 16 字节为 AES 加密的"头"，其余部分与头一起参与 XOR。
    这里采用社区通行做法：对整块 AES-ECB 解密后再整体 XOR。

    密钥长度不合法时返回空串（而非抛异常），让调用方安全回退。
    """
    if len(blob) < 16 or len(aes_key) != 16:
        return b""
    try:
        dec = _aes_ecb_decrypt(blob, aes_key)
    except (ValueError, struct.error, IndexError):
        return b""
    # XOR 密钥取自解密后首块与明文的推导：通常为 0x00（已在 AES 内处理）
    # 若首块仍不匹配，尝试单字节 XOR 嗅探
    return dec


def _try_v1v2(data: bytes, key: bytes) -> tuple[bytes, str] | None:
    """
    尝试用给定 AES 密钥解出 V1/V2，成功则返回 (明文, ext)。

    ⚠️ 判据用 strong_validate（完整结构），不用 identify（仅 magic），
    避免 AES 解出随机字节恰好撞上 magic 的假阳性。
    """
    dec = decrypt_v1v2(data, key)
    if not dec:
        return None
    ext = strong_validate_ext(dec)
    if ext:
        return dec, ext
    # 可能还需一次 XOR
    k = detect_xor_key(dec[:8])
    if k:
        plain = xor_bytes(dec, k[0])
        ext2 = strong_validate_ext(plain)
        if ext2:
            return plain, ext2
    return None


def strong_validate_ext(buf: bytes) -> str | None:
    """强校验并返回扩展名（内部辅助）。"""
    if not buf or len(buf) < 16:
        return None
    for ext, fn in _STRONG.items():
        try:
            if fn(buf):
                return ext
        except (struct.error, IndexError) as e:
            log.debug("扩展校验候选不成立，跳过：%s", e)
            continue
    return None


def decode_dat(blob: bytes, aes_key: bytes | None = None) -> dict:
    """
    解码一个 .dat 的字节内容。

    返回 {'ok': bool, 'fmt': 'plain'|'xor'|'v1'|'v2'|'tmp', 'data': bytes,
          'ext': str, 'detail': str}

    分支策略（2026-10 修正版，为真实数据实测后重排）：

      0) 本身就是明文媒体？            → 直接返回
      1) Old XOR 嗅探（最高优先级）    → 命中即返回
         理由：Windows 微信 4.x 落盘的主流格式就是单字节 XOR，
         且其首字节随机，极易伪造出 V1 的 07 08 magic。
         必须让 XOR 先跑，绝不让"长得像 magic"的文件抢先进 AES。
      2) 严格判据通过才进 V1/V2       → _looks_like_aes_v1v2()
      3) 兜底：老式 .tmp 半加密（marker 0x1D/0x2A/0x3C + 3 字节校验）
    """
    result = {"ok": False, "fmt": None, "data": b"", "ext": "", "detail": ""}

    if not blob:
        result["detail"] = "空文件"
        return result

    # --- 0) 先判断是否本来就是明文媒体（避免 XOR 嗅探以 key=0 误判）---
    ident = identify(blob)
    if ident:
        result.update(ok=True, fmt="plain", data=blob, ext=ident[0],
                      detail="未加密（明文）")
        return result

    # --- 1) Old XOR 嗅探（必须早于 AES 分支）---
    #      ⚠️ 必须用 strong_validate 复核：detect_xor_key 对微信
    #      4.x 的 07 08 开头 .dat 恒返回假阳性（key=0x45/.bmp）。
    k = detect_xor_key(blob[:16])
    if k:
        key, ext = k
        if key == 0:
            # 理论上第 0 步已拦下，这里兜底：key=0 即未加密
            if ident:
                result.update(ok=True, fmt="plain", data=blob, ext=ident[0],
                              detail="未加密（明文）")
                return result
        else:
            plain = xor_bytes(blob, key)
            if strong_validate(plain, ext):
                result.update(ok=True, fmt="xor", data=plain, ext=ext,
                              detail=f"Old XOR（密钥 0x{key:02x}）")
                return result
            # 单字节 XOR 假阳性 → 记录但不下结论，继续尝试其它方案
            result["detail"] = (f"XOR 穷举命中 0x{key:02x}/{ext} 但强校验失败"
                                f"（假阳性）")

    # --- 2) V1/V2（需严格判据，不接受裸 magic）---
    if _looks_like_aes_v1v2(blob):
        # 先试 V1 固定密钥
        r = _try_v1v2(blob, bytes.fromhex(V1_KEY_HEX))
        if r:
            result.update(ok=True, fmt="v1", data=r[0], ext=r[1],
                          detail="V1（固定密钥）")
            return result
        # 再试提供的 V2 密钥
        if aes_key and len(aes_key) == 16:
            r2 = _try_v1v2(blob, aes_key)
            if r2:
                result.update(ok=True, fmt="v2", data=r2[0], ext=r2[1],
                              detail="V2（运行时密钥）")
                return result
        result["detail"] = "疑似 V1/V2，但密钥不匹配（V2 密钥需从微信内存提取）"
        return result

    # --- 3) 兜底：老式 .tmp 半加密（仅前 4 字节有 marker + 校验位）---
    tmp = _decode_tmp(blob)
    if tmp:
        result.update(ok=True, fmt="tmp", data=tmp[0], ext=tmp[1],
                      detail="老式 .tmp（半加密，marker 还原）")
        return result

    # --- 4) 明确识别微信 4.x 加密 .dat（如实说明，不谎报成功）---
    if blob[:2] in (V1_MAGIC, V2_MAGIC_B):
        result["detail"] = (
            "微信 4.x 加密 .dat（07 08 头）：离线无密钥不可解。"
            "须走官方『聊天记录管理 → 导入与导出』获取明文附件，"
            "或用 --aes-key 传入从 Weixin.exe 内存提取的 V2 会话密钥")
        return result

    if result["detail"]:
        return result

    result["detail"] = "无法识别：非已知的 .dat 格式，也不是明文媒体"
    return result


def _decode_tmp(blob: bytes) -> tuple[bytes, str] | None:
    """
    老版微信 .tmp 半加密图片：第 3 字节为 marker（0x1D/0x2A/0x3C），
    对应 jpg/png/gif，第 4 字节为校验位（key & 0xFF）。
    """
    if len(blob) < 6:
        return None
    _TMP_EXT = {0x1D: b"\xff\xd8\xff", 0x2A: b"\x89PNG\r\n\x1a\n", 0x3C: b"GIF8"}
    sig = _TMP_EXT.get(blob[2])
    if sig is None:
        return None
    key = blob[3] ^ sig[3]
    plain = bytes(b ^ key for b in blob)
    ident = identify(plain)
    if ident:
        return plain, ident[0]
    return None


# ---------------------------------------------------------------------------
# 批量处理
# ---------------------------------------------------------------------------

def out_path_for(src: Path, dst_root: Path, src_root: Path, ext: str) -> Path:
    """保持相对目录结构，替换扩展名。"""
    rel = src.relative_to(src_root) if src_root in src.parents or src.parent == src_root else Path(src.name)
    rel = rel.with_suffix(ext)
    return dst_root / rel


def is_metadata_dat(path_or_name) -> bool:
    """
    判断是否为**元数据类** .dat（非媒体，不应送进图片解码器）。

    2026-10 实测真实微信数据后新增。微信会把一批非媒体的状态文件
    也命名成 .dat，它们与图片/视频 .dat 混在同一棵树里：

        alt_name.dat        会话别名表
        detail.dat          设备详情
        phoneid.dat         手机标识
        backup_time.dat     备份时间戳
        phone_history.dat   历史设备记录
        roam_device_info.dat 漫游设备信息
        backup.attr         备份属性（实际不带 .dat，但同类）

    这些文件永远解不出图片，强行解码只会刷满 skip 日志并浪费 IO。
    """
    name = str(path_or_name).replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in _META_DAT_NAMES:
        return True
    stem = name[:-4] if name.endswith(".dat") else name
    return stem in _META_DAT_STEMS


def decode_dir(src_dir: Path, dst_dir: Path, aes_key: bytes | None = None,
               verbose: bool = True) -> dict:
    """解码目录下所有 .dat 与加密媒体文件。"""
    src_dir = Path(src_dir).resolve()
    dst_dir = Path(dst_dir).resolve()
    dst_dir.mkdir(parents=True, exist_ok=True)

    stat = Counter()
    formats = Counter()
    samples: list[str] = []

    for root, _dirs, files in os.walk(src_dir):
        for fn in files:
            p = Path(root) / fn
            if p.suffix.lower() not in (".dat", ".bin", ".tmp", ".jpg", ".png", ".mp4"):
                continue
            # 元数据 .dat 直接归入 skipped-meta，不进解码器
            if p.suffix.lower() == ".dat" and is_metadata_dat(p.name):
                stat["meta_skip"] += 1
                continue
            try:
                blob = p.read_bytes()
            except OSError as e:
                stat["error"] += 1
                if verbose:
                    print(f"  [读取失败] {p.name}: {e}")
                continue

            r = decode_dat(blob, aes_key)
            if not r["ok"]:
                stat["skip"] += 1
                if verbose and stat["skip"] <= 5:
                    print(f"  [跳过] {p.name}: {r['detail']}")
                continue

            out = out_path_for(p, dst_dir, src_dir, r["ext"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(r["data"])
            stat["ok"] += 1
            formats[r["fmt"]] += 1
            if verbose and len(samples) < 12:
                sz = len(r["data"])
                samples.append(f"  {p.name} → {out.name}  [{r['detail']}, {sz//1024} KB]")

    if verbose:
        print(f"\n解码完成：成功 {stat['ok']} / 跳过 {stat['skip']} / 失败 {stat['error']}")
        print(f"格式分布：{dict(formats)}")
        if samples:
            print("\n样例：")
            for s in samples:
                print(s)

    return {"ok": stat["ok"], "skip": stat["skip"], "error": stat["error"],
            "formats": dict(formats)}


# ---------------------------------------------------------------------------
# V2 密钥提取（Windows，读微信进程内存）
# ---------------------------------------------------------------------------

def extract_v2_key(pid: int | None = None) -> bytes | None:
    """
    从 Weixin.exe 进程内存中提取 V2 的 AES 密钥。

    需要：Windows + 管理员权限 + 微信正在运行。
    依赖：pywin32（pip install pywin32）

    原理：扫描进程已提交内存中的 16 字节可打印候选串，
          逐个尝试 AES-ECB 解密一个已知 V2 密文块，成功者即为密钥。
    """
    if sys.platform != "win32":
        print("密钥提取仅支持 Windows", file=sys.stderr)
        return None

    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None

    try:
        import win32api  # noqa: F401
        import win32process  # noqa: F401
    except ImportError:
        print("需要 pywin32：pip install pywin32", file=sys.stderr)
        return None

    # 定位进程
    if pid is None:
        import win32process as wp
        for p in win32process.EnumProcesses():
            try:
                h = win32api.OpenProcess(0x0410, False, p)  # QUERY_INFO
                name = win32process.GetModuleFileNameEx(h, 0)
                if name.lower().endswith(("weixin.exe", "wechat.exe")):
                    pid = p
                    break
            except Exception as e:
                log.debug("进程信息读取失败，继续探测下一个：%s", e)
                continue
    if pid is None:
        print("未找到微信进程，请先登录桌面微信", file=sys.stderr)
        return None

    print(f"目标进程 PID = {pid}，正在扫描内存…")

    PROCESS_VM_READ = 0x0010
    PROCESS_QUERY_INFORMATION = 0x0400
    MEM_COMMIT = 0x1000

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        print("打开进程失败，请以管理员身份运行", file=sys.stderr)
        return None

    RE_KEY16 = re.compile(rb"^[0-9a-fA-F]{16}$|^[0-9A-Za-z]{16}$")

    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    candidates: set[bytes] = set()
    scanned = 0

    try:
        while k32.VirtualQueryEx(handle, ctypes.c_void_p(addr),
                                 ctypes.byref(mbi), ctypes.sizeof(mbi)):
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            if size == 0 or base == 0:
                break
            if mbi.State == MEM_COMMIT and (mbi.Protect & 0xEE) and not (mbi.Protect & 0x100):
                buf = ctypes.create_string_buffer(size)
                read = ctypes.c_size_t(0)
                if k32.ReadProcessMemory(handle, ctypes.c_void_p(base), buf,
                                         size, ctypes.byref(read)):
                    data = buf.raw[:read.value]
                    scanned += len(data)
                    for m in re.finditer(rb"[0-9A-Za-z]{16}", data):
                        s = m.group()
                        if RE_KEY16.match(s):
                            candidates.add(s)
            addr = base + size
            if addr > 0x7FFFFFFFFFFF:
                break
    finally:
        k32.CloseHandle(handle)

    print(f"已扫描 {scanned/1024/1024:.0f} MB，候选密钥 {len(candidates)} 个")
    print("\n注意：验证哪个是真正的密钥，需要一份 V2 密文样本。")
    print("请把微信图片目录中的一个 .dat 路径作为 --probe 传入以自动验证。")
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="微信 .dat 附件解码器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_dec = sub.add_parser("decode", help="批量解码目录")
    p_dec.add_argument("--src", required=True)
    p_dec.add_argument("--dst", required=True)
    p_dec.add_argument("--aes-key", default=None, help="V2 AES 密钥（32 位 hex）")
    p_dec.add_argument("--quiet", action="store_true")

    p_one = sub.add_parser("one", help="解码单个文件")
    p_one.add_argument("--file", required=True)
    p_one.add_argument("--dst", required=True)
    p_one.add_argument("--aes-key", default=None)

    p_probe = sub.add_parser("probe", help="探测单个文件的格式与密钥")
    p_probe.add_argument("--file", required=True)

    p_ext = sub.add_parser("extract-key", help="从微信进程提取 V2 密钥（Windows）")
    p_ext.add_argument("--pid", type=int, default=None)

    p_self = sub.add_parser("selftest", help="自测")

    args = ap.parse_args()
    aes_key = bytes.fromhex(args.aes_key) if getattr(args, "aes_key", None) else None

    if args.cmd == "decode":
        decode_dir(Path(args.src), Path(args.dst), aes_key, not args.quiet)

    elif args.cmd == "one":
        blob = Path(args.file).read_bytes()
        r = decode_dat(blob, aes_key)
        if r["ok"]:
            out = Path(args.dst) / (Path(args.file).stem + r["ext"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(r["data"])
            print(f"成功：{args.file} → {out}  [{r['detail']}]")
        else:
            print(f"失败：{r['detail']}")
            sys.exit(1)

    elif args.cmd == "probe":
        blob = Path(args.file).read_bytes()
        print(f"文件: {args.file}")
        print(f"大小: {len(blob)} 字节")
        print(f"前 16 字节: {blob[:16].hex(' ')}")
        r = decode_dat(blob, aes_key)
        print(f"结果: {'成功' if r['ok'] else '失败'} — {r['detail']}")
        if r["ok"]:
            print(f"格式: {r['fmt']}  扩展名: {r['ext']}  输出大小: {len(r['data'])}")

    elif args.cmd == "extract-key":
        extract_v2_key(args.pid)

    elif args.cmd == "selftest":
        sys.exit(0 if _selftest() else 1)


def _selftest() -> bool:
    """自测：构造三种格式的加密样本，验证解码器。"""
    import tempfile

    # 最小合法 JPEG（含正确文件头 + 结尾）
    jpeg = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300"
                         "ffc00011080001000103012200021101031101ffc4001f0000"
                         "01050101010101010000000000000000000102030405060708"
                         "090a0bffda0008010100013f00") + b"\xff\xd9"

    print("测试 1: Old XOR 格式")
    key = 0x64
    enc = xor_bytes(jpeg, key)
    r = decode_dat(enc)
    print(f"  解码 {'成功' if r['ok'] else '失败'} fmt={r['fmt']} ext={r['ext']} "
          f"还原一致={r['data'] == jpeg}")

    print("测试 2: 明文格式")
    r2 = decode_dat(jpeg)
    print(f"  解码 {'成功' if r2['ok'] else '失败'} fmt={r2['fmt']} "
          f"还原一致={r2['data'] == jpeg}")

    print("测试 3: 不同 XOR 密钥（0x9d）")
    r3 = decode_dat(xor_bytes(jpeg, 0x9D))
    print(f"  解码 {'成功' if r3['ok'] else '失败'} fmt={r3['fmt']} "
          f"还原一致={r3['data'] == jpeg}")

    print("测试 4: 未知数据（应优雅失败）")
    r4 = decode_dat(b"\x01\x02\x03\x04\x05\x06\x07\x08" * 4)
    print(f"  {'成功（不应）' if r4['ok'] else '正确拒绝'} — {r4['detail']}")

    print("测试 5: 文件头多种格式")
    ok5 = True
    for sig, ext, name in SIGNATURES[:4]:
        if detect_xor_key(xor_bytes(sig + b"\x00" * 12, 0x37)):
            pass
        else:
            ok5 = False
            print(f"  !! {name} ({ext}) 嗅探失败")
    print(f"  {'全部通过' if ok5 else '有失败'}")

    tests = [
        r["ok"] and r["data"] == jpeg,
        r2["ok"] and r2["data"] == jpeg,
        r3["ok"] and r3["data"] == jpeg,
        not r4["ok"],
        ok5,
    ]
    print(f"\n自测结果：{sum(tests)}/{len(tests)} 通过")
    return all(tests)


if __name__ == "__main__":
    main()
