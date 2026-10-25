#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路线 A：内存扫描取钥（微信 ≤ 4.0.3.36 有效）。

原理（文档化、非侵入）：WCDB 在进程内存中缓存派生后的库密钥，形态是
ASCII 的 `x'<64hex_enc_key><32hex_salt>'` 或裸 96 hex。用 Windows
文档化 API（OpenProcess / VirtualQueryEx / ReadProcessMemory，均只读）
枚举 Weixin.exe 的可读内存，匹配该形态，再交给**解密裁判**甄别——
解开真实库且 sqlite 能试开才算数，不再用历史上互相矛盾的 HMAC 猜谜。

捕获窗口模式（推荐）：密钥在 DB 打开时才进内存，所以：
  点"开始捕获" → 登录微信/打开聊天 → 高频轮询（间隔 ≤50ms）→ 点"停止"
  → 候选交解密裁判 → 输出契约文件给 keymate 投递。

输出：
  scan_candidates.json   候选原始记录（本机留档，勿外传）
  passphrase.json + all_keys.json   （--db-dir 给全时，直接落契约）

用法：
  python extract_scan.py capture --db-dir <db_storage> --out <目录>
  python extract_scan.py scan --pid 12345 --db-dir <db_storage>
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keymate  # noqa: E402  同目录核心库：解密裁判 + 契约读写

log = logging.getLogger("wv_keymate.scan")

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_READABLE = {0x02, 0x04, 0x20, 0x40}  # READONLY/READWRITE/EXECUTE_READ/EXECUTE_READWRITE
PAGE_GUARD = 0x100
POLL_INTERVAL = 0.05          # 捕获窗口轮询间隔（≤50ms）
MAX_REGION = 512 * 1024 * 1024

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


_API_CACHE: dict = {}


def _api() -> dict:
    """惰性装载 Windows 文档化 API（非 Windows 平台导入本模块不报错）。"""
    if os.name != "nt":
        raise RuntimeError("内存扫描仅支持 Windows 宿主机（Docker 容器内取不了密钥）")
    if not _API_CACHE:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.VirtualQueryEx.restype = ctypes.c_size_t
        k32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p,
                                       ctypes.POINTER(MEMORY_BASIC_INFORMATION),
                                       ctypes.c_size_t]
        k32.ReadProcessMemory.restype = wt.BOOL
        k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k32.CloseHandle.argtypes = [wt.HANDLE]
        _API_CACHE["k32"] = k32
    return _API_CACHE


# --------------------------------------------------------------- 进程定位
def find_weixin_pids() -> list[tuple[int, str]]:
    """返回 [(pid, exe_name)]，只认 Weixin.exe / WeChat.exe。"""
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=20,
                             encoding="gbk", errors="replace").stdout
    except Exception as e:
        log.warning("tasklist 失败：%s", e)
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
            except ValueError:
                continue
    return pids


# ----------------------------------------------------------- 内存读取原语
def iter_readable_regions(handle) -> list[tuple[int, int]]:
    """枚举已提交且可读的内存区域 [(base, size)]（跳过 guard 页与超大块）。"""
    k32 = _api()["k32"]
    regions: list[tuple[int, int]] = []
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    max_addr = 0x7FFFFFFFFFFF
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
                and not (mbi.Protect & PAGE_GUARD)
                and size <= MAX_REGION):
            regions.append((base, size))
        addr = base + size
    return regions


def read_region(handle, base: int, size: int) -> bytes | None:
    k32 = _api()["k32"]
    buf = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(handle, ctypes.c_void_p(base), buf,
                               size, ctypes.byref(read))
    if not ok or read.value == 0:
        return None
    return buf.raw[:read.value]


# ------------------------------------------------------------------ 扫描
def scan_process_for_candidates(pid: int) -> set[str]:
    """扫一遍指定进程的可读内存，返回 96 hex 候选集合（统一小写）。"""
    k32 = _api()["k32"]
    handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        raise RuntimeError("OpenProcess(PID %d) 失败，错误码 %d%s"
                           % (pid, err, "（需要管理员权限）" if err == 5 else ""))
    try:
        regions = iter_readable_regions(handle)
        found: set[str] = set()
        for base, size in regions:
            buf = read_region(handle, base, size)
            if not buf:
                continue
            found |= keymate.extract_candidates(buf)
            if len(found) >= 4096:   # 候选爆炸时先停手，裁判会筛出真的
                break
        return found
    finally:
        k32.CloseHandle(handle)


def capture_window(pid: int, stop: threading.Event,
                   interval: float = POLL_INTERVAL,
                   on_tick=None) -> set[str]:
    """捕获窗口：高频轮询累积候选，直到 stop 置位。返回并集。"""
    found: set[str] = set()
    n = 0
    while not stop.is_set():
        try:
            found |= scan_process_for_candidates(pid)
        except RuntimeError as e:
            log.warning("本轮扫描失败：%s", e)
        n += 1
        if on_tick:
            on_tick(n, len(found))
        stop.wait(interval)   # ≤50ms 一轮；wait 比 sleep 更易被停止打断
    return found


# --------------------------------------------------------------- 产物输出
def write_candidates(out_dir: Path, candidates: set[str], source: str) -> Path:
    """候选原始记录落盘（本机留档；日志里不出现候选本体）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "scan_candidates.json"
    doc = {
        "source": source,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "candidates": sorted(candidates),
    }
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    keymate._chmod_private(p)
    log.info("候选已存档 %s（%d 个）", p, len(candidates))
    return p


def judge_candidates(candidates: set[str], db_dir, passphrase: str = "",
                     source: str = "scan", out_dir=None) -> dict:
    """候选 → 解密裁判 → 契约文件。返回 probe 报告。"""
    extras = [keymate.split_legacy_96hex(c) for c in sorted(candidates)
              if keymate._RE_HEX96.match(c)]
    probe = keymate.probe_db_dir(db_dir, passphrase=passphrase, extras=extras)
    if probe["ok"]:
        dest = Path(out_dir) if out_dir else Path("keymate_out")
        keymate.write_contract(
            dest,
            db_dir=db_dir, keys=probe["keys"],
            passphrase=passphrase or "",
            wxid=keymate.guess_wxid(Path(db_dir)),
            source=source,
        )
    return probe


# --------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="路线 A：内存扫描取钥（微信 ≤ 4.0.3.36；非侵入、只读文档化 API）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_cap = sub.add_parser("capture", help="捕获窗口：开始→登录微信→停止→报告")
    p_cap.add_argument("--db-dir", default=os.environ.get("WV_DB_DIR", ""),
                       help="db_storage 目录（给了就直接跑解密裁判并落契约）")
    p_cap.add_argument("--out", default="keymate_out")
    p_cap.add_argument("--pid", type=int, default=0)
    p_cap.add_argument("--interval", type=float, default=POLL_INTERVAL,
                       help="轮询间隔秒（默认 0.05，即 50ms）")

    p_one = sub.add_parser("scan", help="一次性扫描（无捕获窗口）")
    p_one.add_argument("--db-dir", default=os.environ.get("WV_DB_DIR", ""))
    p_one.add_argument("--out", default="keymate_out")
    p_one.add_argument("--pid", type=int, default=0)
    return ap


def _pick_pid(pid: int) -> int:
    if pid:
        return pid
    pids = find_weixin_pids()
    if not pids:
        raise RuntimeError("未找到运行中的微信进程，请先启动并登录微信")
    if len(pids) > 1:
        log.info("发现多个微信进程：%s，取第一个", [p for p, _ in pids])
    return pids[0][0]


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out_dir = Path(args.out)
    db_dir = Path(args.db_dir) if args.db_dir else None

    try:
        pid = _pick_pid(args.pid)
        log.info("目标进程 PID=%d", pid)

        if args.cmd == "capture":
            log.info("开始捕获（轮询间隔 %dms）。请现在登录微信并打开聊天列表…",
                     int(args.interval * 1000))
            stop = threading.Event()
            input("按回车开始捕获…")
            holder: dict = {"found": set()}

            def _run():
                holder["found"] = capture_window(
                    pid, stop, interval=args.interval,
                    on_tick=lambda n, c: log.info("  第 %d 轮，累计候选 %d 个", n, c)
                    if n % 20 == 0 else None)

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            input("捕获中……登录/触发加载聊天后，按回车停止。")
            stop.set()
            t.join(timeout=30)
            found = holder["found"]
        else:
            found = scan_process_for_candidates(pid)

        log.info("候选共 %d 个", len(found))
        write_candidates(out_dir, found, "scan")

        if db_dir and db_dir.is_dir() and found:
            probe = judge_candidates(found, db_dir, source="scan", out_dir=out_dir)
            print(json.dumps({"ok": probe["ok"], "failed": probe["failed"],
                              "skipped": probe["skipped"]}, ensure_ascii=False, indent=2))
            return 0 if probe["ok"] else 1
        if db_dir:
            log.warning("没有候选可校验（或 db-dir 不存在），跳过裁判")
            return 1
        return 0
    except (ValueError, RuntimeError, OSError) as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
