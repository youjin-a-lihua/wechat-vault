#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wv_winkey.py —— Windows 微信 4.x 密钥提取器（纯 Python，无需第三方依赖）

路径说明（对标微备份 WxBackup 的技术路线）：
    WCDB（微信的 SQLCipher 封装）在进程内存中缓存派生后的 raw key，
    格式为 ASCII:  x'<64hex_enc_key><32hex_salt>'
    共 96 个 hex 字符（48 字节明文密钥材料）+ 包裹字符。

    我们扫描 Weixin.exe 的私有可读内存，匹配该模式，
    再用 HMAC-SHA512(page1, salt, enc_key) 校验 page 1 的末尾 64 字节，
    只有校验通过才认定为真密钥——避免把随机内存当成密钥。

图片 .dat 的 V2 密钥同理（AES-128-ECB），单独扫描。

用法：
    python wv_winkey.py                # 提取数据库密钥 + 图片密钥
    python wv_winkey.py --db-only      # 只提数据库密钥
    python wv_winkey.py --img-only     # 只提图片密钥
    python wv_winkey.py --pid 31552    # 指定 PID

输出：
    all_keys.json      { "db_dir": "...", "keys": { "message/message_0.db": "<96hex>", ... } }
    image_key.json     { "aes_key": "<32hex>" }
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac
import json
import os
import re
import struct
import sys
from pathlib import Path
import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- Windows API

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

MEM_COMMIT = 0x1000
PAGE_READABLE = {0x02, 0x04, 0x20, 0x40}  # READONLY / READWRITE / EXECUTE_READ / EXECUTE_READWRITE
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01

k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                               ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
k32.ReadProcessMemory.restype = wt.BOOL
k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.CloseHandle.argtypes = [wt.HANDLE]


# ------------------------------------------------------------ 找微信进程 PID

def _log(msg: str) -> None:
    """同时输出到 stdout 和日志文件（沙箱/重定向下仍留痕）"""
    print(msg, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:  # 可安全忽略：日志自身写失败，不能因此中断主流程
        pass


LOG_PATH = Path(__file__).resolve().parent.parent / "local_keys" / "extract.log"


def find_weixin_pids() -> list[tuple[int, str]]:
    """返回 [(pid, exe_name)]，只取 Weixin.exe / WeChat.exe"""
    import subprocess
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
            encoding="gbk", errors="replace",
        ).stdout
    except Exception as e:
        _log(f"[!] tasklist 失败: {e}")
        return []

    pids: list[tuple[int, str]] = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        name, pid_s = parts[0], parts[1]
        if name.lower() in ("weixin.exe", "wechat.exe"):
            try:
                pids.append((int(pid_s), name))
            except ValueError as e:
                log.debug("PID 字段非数字，跳过：%s", e)
                pass
    return pids


# -------------------------------------------------------------- 内存扫描核心

def iter_readable_regions(handle) -> list[tuple[int, int]]:
    """枚举进程中所有已提交且可读的内存区域 [(base, size)]"""
    regions: list[tuple[int, int]] = []
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    max_addr = 0x7FFFFFFFFFFF  # 用户态上限
    while addr < max_addr:
        got = k32.VirtualQueryEx(handle, ctypes.c_void_p(addr),
                                 ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not got:
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize or 0
        if size == 0:
            break
        if (mbi.State == MEM_COMMIT
                and (mbi.Protect & 0xFF) in PAGE_READABLE
                and not (mbi.Protect & PAGE_GUARD)):
            # 跳过超大区域里明显非数据的（>512MB 单块通常不是 key 缓存）
            if size <= 512 * 1024 * 1024:
                regions.append((base, size))
        addr = base + size
    return regions


def read_region(handle, base: int, size: int) -> bytes | None:
    buf = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(handle, ctypes.c_void_p(base), buf,
                               size, ctypes.byref(read))
    if not ok or read.value == 0:
        return None
    return buf.raw[:read.value]


# 内存中缓存的 raw key 形态：x'<64hex><32hex>'  —— 96 个 hex 字符
KEY_PATTERN = re.compile(rb"x'([0-9a-fA-F]{96})'")
BARE_PATTERN = re.compile(rb"(?<![0-9a-fA-F])([0-9a-fA-F]{96})(?![0-9a-fA-F])")
IMG_KEY_PATTERN = re.compile(rb"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])")


def scan_process_for_db_keys(pid: int) -> dict[str, str]:
    """
    扫描指定进程，返回 { db_relative_path: 96hex_key }
    校验方式：HMAC-SHA512 验证各 .db 的 page 1
    """
    handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                             False, pid)
    if not handle:
        err = ctypes.get_last_error()
        _log(f"[!] OpenProcess(PID {pid}) 失败，错误码 {err}"
              f"{'（需要管理员权限）' if err == 5 else ''}")
        return {}

    _log(f"[*] 已打开 PID {pid}，开始枚举内存区域…")
    try:
        regions = iter_readable_regions(handle)
    except Exception as e:
        k32.CloseHandle(handle)
        _log(f"[!] 枚举内存失败: {e}")
        return {}
    _log(f"[*] 可读内存区域 {len(regions)} 块，共 "
          f"{sum(s for _, s in regions) / 1024 / 1024:.0f} MB")

    # 候选密钥集合
    candidates: set[str] = set()
    scanned = 0
    for base, size in regions:
        buf = read_region(handle, base, size)
        scanned += 1
        if not buf:
            continue
        for m in KEY_PATTERN.finditer(buf):
            candidates.add(m.group(1).decode())
        # 裸 96hex 也收（微信某些版本不带 x'' 包裹）
        if len(candidates) < 4096:
            for m in BARE_PATTERN.finditer(buf):
                candidates.add(m.group(1).decode())
    k32.CloseHandle(handle)

    _log(f"[*] 内存扫描完成（{scanned} 块），候选密钥 {len(candidates)} 个")
    return candidates


def verify_key_against_db(enc_key_hex: str, salt_hex: str, db_path: Path) -> bool:
    """
    SQLCipher 4 校验：page 1 的末尾 64 字节 = HMAC-SHA512(
        key = PBKDF2(enc_key, salt, 256000, 32, sha512),
        data = page1[16:4096-64+16] ... 简化：直接对 page1 前 (page_size-64) 做 HMAC
    )
    实际 SQLCipher4 格式：
      page1 = salt(16) || encrypted_rest
      HMAC 覆盖 = page1[16 : page_size-64]  （即 salt 之后的密文部分，含 reserve 前的数据）
      存于 page1[page_size-64 : page_size]
      且存储的 HMAC 会被 enc_key 再异或一次
    """
    try:
        page_size = 4096
        with open(db_path, "rb") as f:
            page1 = f.read(page_size)
        if len(page1) < page_size:
            return False

        salt = bytes.fromhex(salt_hex)
        enc_key = bytes.fromhex(enc_key_hex)

        mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, salt, 256000, dklen=32)
        # SQLCipher4: HMAC 输入为 page1[16 : page_size-64+16]
        mac = hmac.new(mac_key, page1[16:page_size - 64 + 16], hashlib.sha512).digest()
        stored = bytes(a ^ b for a, b in zip(page1[page_size - 64:page_size], mac_key[:64]))
        return hmac.compare_digest(mac, stored)
    except Exception:
        return False


_PROBE_CTX: dict = {}


def _probe_chunk(chunk: list[str]) -> str | None:
    """模块级函数（必须，ProcessPoolExecutor 才能 pickle）"""
    salt = _PROBE_CTX["salt"]
    page1 = _PROBE_CTX["page1"]
    want = _PROBE_CTX["want"]
    for cand in chunk:
        try:
            enc = bytes.fromhex(cand[:64])
            mk = hashlib.pbkdf2_hmac("sha512", enc, salt, 256000, dklen=32)
        except Exception as e:
            log.debug("候选派生失败，跳过：%s", e)
            continue
        m = hmac.new(mk, page1[16:4096 - 64 + 16], hashlib.sha512).digest()
        st = bytes(a ^ b for a, b in zip(want, mk[:64]))
        if hmac.compare_digest(m, st):
            return cand
    return None


def match_keys_to_dbs(candidates: set[str], db_dir: Path) -> dict[str, str]:
    """
    高效匹配：SQLCipher4 的 HMAC 校验只需 mac_key = PBKDF2(enc_key, salt)。

    关键优化——PBKDF2 的密码是 enc_key、盐是 salt：
      mac_key = PBKDF2-HMAC-SHA512(enc_key, salt, 256000, 32)
      对某个固定库（salt 固定），每个候选 enc_key 都要算一次 PBKDF2（这是 256000 轮，
      极慢）。但 HMAC 本身很快。

      因此我们把 「HMAC 校验」提前算好：对每个库，遍历全部候选算 PBKDF2 → HMAC。
      477 候选 × 23 库 × 256000 轮 ≈ 28 亿次哈希 —— 太慢！

    真正可用的做法：先只用**一个库**（页最小、最可能命中的 session.db）
    把候选缩到 1 个，然后一key通吃（微信全部库共用同一 enc_key，salt 每库不同）。
    """
    dbs = sorted(db_dir.rglob("*.db"))
    dbs = [d for d in dbs if not d.name.endswith("-wal") and not d.name.endswith("-shm")]
    _log(f"[*] 待匹配数据库 {len(dbs)} 个")

    if not dbs or not candidates:
        return {}

    # 挑一个探测库：prefer session.db（最小、结构稳定）
    probe = None
    for d in dbs:
        if d.name == "session.db":
            probe = d
            break
    if probe is None:
        probe = min(dbs, key=lambda p: p.stat().st_size)

    _log(f"[*] 用探测库缩小候选范围: {probe.relative_to(db_dir).as_posix()}")
    with open(probe, "rb") as f:
        page1 = f.read(4096)
    salt = page1[:16]
    want = page1[4096 - 64:4096]

    keys = list(candidates)
    _log(f"[*] 候选 {len(keys)} 个，并行做 PBKDF2(256000) 校验…")

    master: str | None = None
    import time
    from concurrent.futures import ProcessPoolExecutor
    t0 = time.time()

    # 拆成 N 份，多进程并行（PBKDF2 是纯 CPU 计算，GIL 无法并行）
    nproc = min(os.cpu_count() or 4, 8)
    chunks = [keys[i::nproc] for i in range(nproc)]

    _PROBE_CTX["salt"] = salt
    _PROBE_CTX["page1"] = page1
    _PROBE_CTX["want"] = want

    try:
        with ProcessPoolExecutor(max_workers=nproc) as ex:
            for r in ex.map(_probe_chunk, chunks):
                if r:
                    master = r
                    break
    except Exception as e:
        _log(f"[!] 并行校验异常，退回单进程: {e}")
        master = _probe_chunk(keys)

    if master is not None:
        _log(f"  [OK] 命中 master key（{nproc} 进程并行，耗时 {time.time() - t0:.1f}s）")
    else:
        _log(f"[!] {len(keys)} 个候选均未通过探测库校验（{time.time() - t0:.0f}s）")
        return {}

    # 拿到 master key 后，全部库都能用（salt 不同，但 enc_key 同一个）
    result: dict[str, str] = {}
    for db in dbs:
        rel = db.relative_to(db_dir).as_posix()
        try:
            with open(db, "rb") as f:
                head = f.read(16)
        except Exception as e:
            log.debug("库头读取失败，跳过该库：%s", e)
            continue
        if verify_key_against_db(master[:64], head.hex(), db):
            result[rel] = master
            _log(f"  [OK] {rel}")
        else:
            _log(f"  [--] {rel}  未通过（可能空库/未加密）")
    _log(f"[*] master key = {master[:32]}…{master[64:]}（enc_key + salt）")
    return result


def scan_image_key(pid: int, img_sample: Path | None) -> str | None:
    """扫描内存中的 32hex，用真实 .dat 的 V2 结构校验"""
    handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        _log("[!] OpenProcess 失败（图片密钥扫描）")
        return None

    regions = iter_readable_regions(handle)
    cands: set[str] = set()
    for base, size in regions:
        buf = read_region(handle, base, size)
        if not buf:
            continue
        for m in IMG_KEY_PATTERN.finditer(buf):
            cands.add(m.group(1).decode())
    k32.CloseHandle(handle)
    _log(f"[*] 图片密钥候选 {len(cands)} 个")

    if img_sample and img_sample.exists():
        for c in cands:
            if verify_v2_dat(bytes.fromhex(c), img_sample):
                _log(f"  [OK] 图片 V2 密钥校验通过: {c}")
                return c
    return None


def verify_v2_dat(aes_key: bytes, dat_path: Path) -> bool:
    """V2 结构校验：[6B sig][4B aes_size LE][4B xor_size LE][1B pad][AES][raw][XOR]"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "archiver"))
        from wv_dat import _aes128_ecb_decrypt_pure as aes128_ecb_decrypt
    except Exception:
        return False
    try:
        blob = dat_path.read_bytes()
        if len(blob) < 15 or blob[:2] != b"\x07\x08":
            return False
        aes_size = struct.unpack("<I", blob[6:10])[0]
        if aes_size <= 0 or 15 + aes_size > len(blob):
            return False
        dec = aes128_ecb_decrypt(blob[15:15 + aes_size], aes_key)
        return dec[:3] in (b"\xff\xd8\xff", b"\x89PN", b"GIF", b"BM", b"RIFF", b"\x00\x00\x00")
    except Exception:
        return False


# ------------------------------------------------------------------- 主流程

def detect_db_dir() -> Path | None:
    """自动定位微信 4.x db_storage"""
    guesses: list[Path] = []
    for drive in ("G:", "D:", "C:", "E:", "F:", "H:"):
        for base in (rf"{drive}\We chat and QQ\Wechat\xwechat_files",
                     rf"{drive}\xwechat_files",
                     rf"{drive}\Documents\xwechat_files",
                     rf"{drive}\WeChat Files"):
            p = Path(base)
            if p.exists():
                guesses.extend(p.glob("*/db_storage"))
    # 选 message 目录最大的那个
    best, best_sz = None, -1
    for g in guesses:
        m = g / "message"
        if m.exists():
            sz = sum(f.stat().st_size for f in m.glob("*.db"))
            if sz > best_sz:
                best, best_sz = g, sz
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description="微信 4.x 密钥提取（微备份路线）")
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--db-dir", default="")
    ap.add_argument("--db-only", action="store_true")
    ap.add_argument("--img-only", action="store_true")
    args = ap.parse_args()

    out_dir = Path(__file__).resolve().parent.parent / "local_keys"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 找进程 ----
    pids = find_weixin_pids()
    if not pids:
        _log("[!] 未找到运行中的 Weixin.exe —— 请先启动微信并登录")
        return 2
    _log(f"[*] 找到微信进程: {pids}")

    db_dir = Path(args.db_dir) if args.db_dir else detect_db_dir()
    if db_dir:
        _log(f"[*] 数据库目录: {db_dir}")
    else:
        _log("[!] 未自动定位到 db_storage，请用 --db-dir 指定")

    rc = 0

    if not args.img_only:
        target_pids = [args.pid] if args.pid else [p for p, _ in pids]
        merged: dict[str, str] = {}
        for pid in target_pids:
            cands = scan_process_for_db_keys(pid)
            if not cands or not db_dir:
                continue
            hits = match_keys_to_dbs(cands, db_dir)
            merged.update(hits)
            if len(merged) >= len(list(db_dir.rglob("*.db"))):
                break
        if merged:
            kf = out_dir / "all_keys.json"
            payload = {"db_dir": str(db_dir), "keys": merged}
            kf.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
            try:
                os.chmod(kf, 0o600)
            except Exception:  # 可安全忽略：chmod 失败只影响密钥文件权限，另有提示
                pass
            _log(f"\n[✓] 数据库密钥已保存: {kf}  （{len(merged)} 个库）")
        else:
            _log("\n[!] 未提取到任何有效数据库密钥")
            rc = 1

    if not args.db_only:
        # 找一个真实 .dat 样本
        sample = None
        if db_dir:
            root = db_dir.parent
            for pat in ("msg/attach/*/*/Img/*.dat", "msg/attach/*/*/Img/*/*.dat"):
                for f in root.glob(pat):
                    if f.stat().st_size > 2048:
                        sample = f
                        break
                if sample:
                    break
        _log(f"\n[*] 图片密钥扫描样本: {sample}")
        for pid in ([args.pid] if args.pid else [p for p, _ in pids]):
            ik = scan_image_key(pid, sample)
            if ik:
                (out_dir / "image_key.json").write_text(
                    json.dumps({"aes_key": ik}, indent=2), encoding="utf-8")
                _log(f"[✓] 图片密钥已保存: {out_dir / 'image_key.json'}")
                break
        else:
            _log("[!] 未提取到图片 V2 密钥（可能该批 .dat 是旧 XOR 或 V1 格式）")

    return rc


if __name__ == "__main__":
    sys.exit(main())
