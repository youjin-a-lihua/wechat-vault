#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路线 B：进程快照取钥（新版微信的非侵入方案）。

为什么需要它：微信 > 4.0.3.36 后，内存扫描的存活窗口变短，单次瞬时扫描
容易扑空。本路线用 Windows **文档化**调试 API `MiniDumpWriteDump`
（来自系统调试辅助库 dbghelp）把 Weixin.exe 的进程内存一次性落成
minidump 快照，然后**离线**扫快照找密钥候选，最后交解密裁判甄别。

对微信进程只做只读取快照：不改微信、不往进程里写任何东西、不改变其执行
路径，权限需求与路线 A 相同（同用户或管理员）。这是对「注入类取钥」的
直接替代设计——代价只是多落一个本地快照文件，收益是时机可以从容选择。

时机（关键）：密钥在 DB 打开时才进内存。所以要在登录初期、或触发加载
聊天记录之后尽快取快照；登录后放着不动再抓，可能已经看不到密钥材料。

产物：快照默认写到本机临时目录，扫完即删（--keep 可留档）；命中后按契约
落 passphrase.json / all_keys.json，交 keymate.py 投递。

用法：
  python extract_dump.py dump --db-dir <db_storage> --out <目录>
  python extract_dump.py scan-dump --dump <已有快照> --db-dir <db_storage>
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import keymate  # noqa: E402  同目录核心库：解密裁判 + 契约读写
from extract_scan import find_weixin_pids  # noqa: E402

log = logging.getLogger("wv_keymate.dump")

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
CREATE_ALWAYS = 2
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# MINIDUMP_TYPE 里的私有读写内存位（文档化枚举值 0x200）：
# 包含进程私有可读写内存页——堆上的密钥材料就在其中。
MINI_WITH_RW_MEM = 0x00000200

_API_CACHE: dict = {}


def _api() -> dict:
    """惰性装载文档化 API（非 Windows 平台导入本模块不报错）。"""
    if os.name != "nt":
        raise RuntimeError("进程快照仅支持 Windows 宿主机（Docker 容器内取不了密钥）")
    if not _API_CACHE:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.CreateFileW.restype = wt.HANDLE
        k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                    wt.DWORD, wt.DWORD, wt.HANDLE]
        # 系统调试辅助库：提供 MiniDumpWriteDump
        dbg = ctypes.WinDLL("dbghelp", use_last_error=True)
        dbg.MiniDumpWriteDump.restype = wt.BOOL
        dbg.MiniDumpWriteDump.argtypes = [wt.HANDLE, wt.DWORD, wt.HANDLE,
                                          ctypes.c_int, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_void_p]
        _API_CACHE["k32"] = k32
        _API_CACHE["dbg"] = dbg
    return _API_CACHE


# --------------------------------------------------------------- 取快照
def create_minidump(pid: int, out_path: Path) -> Path:
    """把目标进程的内存一次性落成 minidump（只读，不改动目标进程）。"""
    api = _api()
    k32, dbg = api["k32"], api["dbg"]

    handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        raise RuntimeError("OpenProcess(PID %d) 失败，错误码 %d%s"
                           % (pid, err, "（需要管理员权限）" if err == 5 else ""))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    h_file = k32.CreateFileW(str(out_path), GENERIC_WRITE, FILE_SHARE_READ, None,
                             CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
    if not h_file or h_file == INVALID_HANDLE_VALUE:
        k32.CloseHandle(handle)
        raise RuntimeError("无法创建快照文件：%s" % out_path)

    try:
        ok = dbg.MiniDumpWriteDump(handle, pid, h_file, MINI_WITH_RW_MEM,
                                   None, None, None)
        if not ok:
            err = ctypes.get_last_error()
            raise RuntimeError("MiniDumpWriteDump 失败，错误码 %d" % err)
    finally:
        k32.CloseHandle(h_file)
        k32.CloseHandle(handle)

    size = out_path.stat().st_size
    log.info("快照完成：%s（%.1f MB）", out_path, size / 1024 / 1024)
    return out_path


# ------------------------------------------------------------- 离线扫描
def scan_dump_for_candidates(dump_path: Path, chunk: int = 8 * 1024 * 1024) -> set[str]:
    """离线扫快照字节流，捞 96 hex 候选。分块读，快照可能上 GB。"""
    found: set[str] = set()
    tail = b""
    with open(dump_path, "rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            data = tail + buf
            found |= keymate.extract_candidates(data)
            # 留 96+16 字节尾巴，跨块的候选不丢
            tail = data[-(96 + 16):]
    log.info("快照扫描完成，候选 %d 个", len(found))
    return found


def judge_candidates(candidates: set[str], db_dir, source: str = "dump",
                     out_dir=None) -> dict:
    """候选 → 解密裁判 → 契约文件。"""
    extras = [keymate.split_legacy_96hex(c) for c in sorted(candidates)
              if keymate._RE_HEX96.match(c)]
    probe = keymate.probe_db_dir(db_dir, extras=extras)
    if probe["ok"]:
        dest = Path(out_dir) if out_dir else Path("keymate_out")
        keymate.write_contract(
            dest,
            db_dir=db_dir, keys=probe["keys"],
            wxid=keymate.guess_wxid(Path(db_dir)),
            source=source,
        )
    return probe


# --------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="路线 B：进程快照取钥（文档化 MiniDumpWriteDump，离线扫快照）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_d = sub.add_parser("dump", help="取快照 + 离线扫描 + 解密裁判")
    p_d.add_argument("--db-dir", default=os.environ.get("WV_DB_DIR", ""),
                     help="db_storage 目录（给了就直接跑解密裁判并落契约）")
    p_d.add_argument("--out", default="keymate_out")
    p_d.add_argument("--pid", type=int, default=0)
    p_d.add_argument("--dump-out", default="", help="快照落盘路径（默认临时目录）")
    p_d.add_argument("--keep", action="store_true", help="扫完保留快照文件")

    p_s = sub.add_parser("scan-dump", help="只离线扫描已有快照")
    p_s.add_argument("--dump", required=True, help="已有 minidump 路径")
    p_s.add_argument("--db-dir", default=os.environ.get("WV_DB_DIR", ""))
    p_s.add_argument("--out", default="keymate_out")
    return ap


def _pick_pid(pid: int) -> int:
    if pid:
        return pid
    pids = find_weixin_pids()
    if not pids:
        raise RuntimeError("未找到运行中的微信进程，请先启动并登录微信")
    return pids[0][0]


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out_dir = Path(args.out)
    db_dir = Path(args.db_dir) if args.db_dir else None
    dump_path = None

    try:
        if args.cmd == "dump":
            pid = _pick_pid(args.pid)
            log.info("目标进程 PID=%d（建议登录初期/触发加载聊天后立即抓）", pid)
            if args.dump_out:
                dump_path = Path(args.dump_out)
            else:
                fd, tmp = tempfile.mkstemp(prefix="wv_keymate_", suffix=".dmp")
                os.close(fd)
                dump_path = Path(tmp)
            create_minidump(pid, dump_path)
            found = scan_dump_for_candidates(dump_path)
            if not args.keep:
                try:
                    dump_path.unlink()
                    log.info("快照已删除（--keep 可保留）")
                except OSError:
                    pass
        else:
            dump_path = Path(args.dump)
            if not dump_path.is_file():
                raise ValueError("快照文件不存在：%s" % dump_path)
            found = scan_dump_for_candidates(dump_path)

        log.info("候选共 %d 个", len(found))
        keymate_dir = out_dir
        keymate_dir.mkdir(parents=True, exist_ok=True)
        cand_doc = {
            "source": "dump",
            "candidates": sorted(found),
        }
        cand_path = keymate_dir / "dump_candidates.json"
        cand_path.write_text(json.dumps(cand_doc, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        keymate._chmod_private(cand_path)

        if db_dir and db_dir.is_dir() and found:
            probe = judge_candidates(found, db_dir, source="dump", out_dir=out_dir)
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
