#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wv_woc.py —— 微信数据源适配器（阶段二核心，单容器一体化）

把容器内 Linux 微信（WOC 运行时）落盘的加密聊天库，导出成方舟标准中间格式，
对接 wv_archiver 入库到 vault.db（可全文搜索）。

════════════════════════════════════════════════════════════════════
  技术事实（已核实，2026-10-02 调研定论，禁止臆想）
════════════════════════════════════════════════════════════════════
1. 数据落盘位置（Linux 版微信，微信 4.x）：
       ~/.xwechat/xwechat_files/<账号wxid>/db_storage/
   候选根：~/.xwechat/xwechat_files、~/.xwechat、~/.local/share/xwechat_files
   → 本容器 abc 用户 HOME=/config，故实际根 = /config/.xwechat/...

2. 数据库文件（**SQLCipher 加密**，不是明文）：
       message_0.db      聊天消息（正文、发送方、时间戳）
       media_0.db        媒体文件索引（图片/语音/视频的元数据）
       biz_message_0.db  公众号/业务消息
   → 「明文落盘」是早期错误认知；落盘的是加密库，需 key 解密。

3. 数据库 key：64 位十六进制字符串。
   Linux 上通过 gdb/ptrace 附加「运行中」的微信进程抓取（微信必须运行+登录）。
   依赖：gdb + ptrace_scope 放行 + 容器内 root 或 CAP_SYS_PTRACE。

4. 参考实现（成熟开源，clean-room）：wxchat-export
       github.com/Junt184/wxchat-export
   已完整走通「账号发现 → 抓 key → 读加密库 → 解析会话/消息 → 导出 md/jsonl」。
   支持 Linux x86_64 + 微信 4.1.5；微信二进制候选路径含 /opt/wechat/wechat。
   当前不支持图片/语音/视频正文解密（只文本消息）。

════════════════════════════════════════════════════════════════════
  本模块定位：编排层
════════════════════════════════════════════════════════════════════
  · 不重新实现 gdb 抓 key / SQLCipher 解密（复用 wxchat-export，避免重复造轮子）
  · 职责：账号发现 → 触发导出 → JSONL → 方舟标准化消息 → 交给 wv_archiver
  · key 提取失败时回退「手动 --db-key」路径（与 wxchat-export 一致）

════════════════════════════════════════════════════════════════════
  对接约定（与 wv_parser 的标准消息字段对齐）
════════════════════════════════════════════════════════════════════
  wv_parser 的标准消息 dict 字段：
      ts          int|None   Unix 时间戳
      time_str    str|None   原始时间字符串
      sender      str        发送者显示名
      content     str        正文
      is_self     bool       是否自己发出
      is_group    bool       是否群聊
  conv 表字段：conv_id(=account::title), account, title, is_group, members, n_msg, start_ts, end_ts
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# ──────────────────────────────────────────────────────────────────
# 常量：可被环境变量覆盖（与容器部署对齐）
# ──────────────────────────────────────────────────────────────────
# 微信数据根【2026-10-02 实测确认】：/config/xwechat_files/（abc HOME=/config）
#   ⚠️ 实测与预研有出入：不是 ~/.xwechat/xwechat_files，而是 HOME 下直接 xwechat_files
#   账号目录 = wxid_xxx_<后缀>，其下 db_storage/message/message_0.db 等
WX_DATA_ROOT = Path(os.environ.get("WV_WX_DATA_ROOT", "/config/xwechat_files"))
# 微信二进制
WX_BIN = os.environ.get("WV_WX_BIN", "/data/wechat/opt/wechat/wechat")
# wxchat-export CLI（若安装）
WXCE = os.environ.get("WV_WXCE_BIN", "wxchat-export")


# ──────────────────────────────────────────────────────────────────
# 1) 账号发现
# ──────────────────────────────────────────────────────────────────
def discover_accounts(root: Path | None = None) -> list[str]:
    """扫描数据根，列出账号目录（wxid_*）。

    TODO(C 阶段·实测)：确认容器内 db_storage 的实际目录层级后，
    这里改为精确匹配 <root>/<wxid>/db_storage/ 或 <root>/<wxid>。
    当前只做宽松探测，不臆想精确结构。
    """
    root = root or WX_DATA_ROOT
    accounts: list[str] = []
    if not root.exists():
        return accounts
    for p in sorted(root.iterdir()):
        if p.is_dir() and (p.name.startswith("wxid_") or "db_storage" in [c.name for c in p.iterdir() if c.is_dir()]):
            accounts.append(p.name)
    return accounts


# ──────────────────────────────────────────────────────────────────
# 2) 触发导出（复用 wxchat-export）
# ──────────────────────────────────────────────────────────────────
def run_wxce(*args: str) -> subprocess.CompletedProcess:
    """调 wxchat-export CLI（账号发现/抓 key/导出）。"""
    return subprocess.run([WXCE, *args], capture_output=True, text=True)


def export_account(account: str, out_dir: Path, db_key: str | None = None) -> Path:
    """导出单个账号全部会话为 JSONL。

    返回导出目录（含 manifest.json + sessions/*.jsonl）。
    TODO(C 阶段·实测)：确认 wxchat-export 的 JSONL 字段结构后，
    完善 to_archive_format() 的字段映射。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    args = ["export", "--account", account, "--session", "all",
            "--out", str(out_dir), "--format", "jsonl"]
    if db_key:
        args += ["--db-key", db_key]
    r = run_wxce(*args)
    if r.returncode != 0:
        raise RuntimeError(f"wxchat-export 导出失败: {r.stderr.strip()[:500]}")
    return out_dir


# ──────────────────────────────────────────────────────────────────
# 3) JSONL → 方舟标准化消息
# ──────────────────────────────────────────────────────────────────
def to_archive_format(jsonl_dir: Path, account: str) -> list[dict]:
    """把 wxchat-export 的 sessions/*.jsonl 转成 wv_parser 标准消息 dict。

    TODO(C 阶段·实测)：字段映射需按实测 JSONL 结构校准。
    预期 wxchat-export 每行含：发送者/时间/正文/是否群聊/会话名。
    """
    msgs: list[dict] = []
    sessions_dir = jsonl_dir / "sessions"
    for f in sorted(sessions_dir.glob("*.jsonl")):
        conv_title = f.name.removesuffix(".jsonl")  # <display>__<username>
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                # TODO(C)：据实测映射 rec → {ts,time_str,sender,content,is_self,is_group}
                msgs.append({
                    "conv_title": conv_title,
                    "ts": rec.get("ts"),
                    "time_str": rec.get("time_str"),
                    "sender": rec.get("sender", ""),
                    "content": rec.get("content", ""),
                    "is_self": bool(rec.get("is_self")),
                    "is_group": bool(rec.get("is_group")),
                    "account": account,
                })
    return msgs


# ──────────────────────────────────────────────────────────────────
# 4) 主流程编排
# ──────────────────────────────────────────────────────────────────
def ingest(source_root: Path | None = None, out_dir: Path | None = None,
           db_key: str | None = None) -> int:
    """一条龙：发现账号 → 导出 JSONL → 转标准消息 → （后续）喂归档器。

    TODO(C 阶段·实测)：最后一步「标准消息 → vault.db」尚未接，
    待确认 wxchat-export JSONL 结构与 wv_archiver 入库接口后补全。
    """
    accounts = discover_accounts(source_root)
    if not accounts:
        print("[wv_woc] 未发现账号目录 —— 微信可能尚未登录/迁移。", file=sys.stderr)
        return 1
    print(f"[wv_woc] 发现账号: {accounts}")
    out = out_dir or Path("/data/wechat/.export")
    for acc in accounts:
        print(f"[wv_woc] 导出账号 {acc} …")
        jdir = export_account(acc, out / acc, db_key=db_key)
        msgs = to_archive_format(jdir, acc)
        print(f"[wv_woc]   {acc}: {len(msgs)} 条消息")
    print("[wv_woc] 导出完成。下一步（C 阶段实测后）：接入 wv_archiver 入库。")
    return 0


# ──────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="微信数据源适配器（单容器一体化）")
    sub = ap.add_subparsers(dest="cmd")

    p_accounts = sub.add_parser("accounts", help="列出账号")
    p_accounts.add_argument("--root", default=None, help="微信数据根（默认环境变量）")

    p_ingest = sub.add_parser("ingest", help="导出并转标准消息")
    p_ingest.add_argument("--root", default=None)
    p_ingest.add_argument("--out", default=None)
    p_ingest.add_argument("--db-key", default=None, help="64位hex，跳过 gdb 抓取")

    args = ap.parse_args()
    if args.cmd == "accounts":
        for a in discover_accounts(Path(args.root) if args.root else None):
            print(a)
        return 0
    if args.cmd == "ingest":
        return ingest(Path(args.root) if args.root else None,
                      Path(args.out) if args.out else None,
                      args.db_key)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
