#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Vault - 增量归档核心
============================

职责：
  1. 扫描收件箱 / 归档源目录，识别新增或变更的聊天记录文件
  2. 计算内容指纹（SHA-256），跳过已归档的重复文件
  3. 把消息**增量**写入持久化 SQLite 主库（不随服务启动重建）
  4. 生成 MANIFEST.json 完整性清单
  5. 支持冷热分层搬运（热数据留 SSD，冷数据搬到 HDD）

设计要点：
  - 幂等：同一文件反复归档不会产生重复数据
  - 只增不改：已归档的记录永不覆盖，仅追加新消息
  - 可追溯：每条消息记录来源文件与内容哈希
  - 零依赖：只用标准库 + sqlite3

用法：
  python wv_archiver.py scan  --source <待归档目录> --store <库目录>
  python wv_archiver.py stats --store <库目录>
  python wv_archiver.py verify --store <库目录>
  python wv_archiver.py archive --source <目录> --dest <目录>   # 冷归档搬运
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "parser"))
import wv_parser as P  # noqa: E402
import logging

log = logging.getLogger(__name__)

# 媒体解码器（可选依赖：同目录的 wv_dat.py）
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import wv_dat as D  # noqa: E402
except Exception:  # pragma: no cover
    D = None

CST = timezone(timedelta(hours=8))
SUPPORTED_EXT = {".csv", ".json", ".html", ".htm", ".txt"}

# ---------------------------------------------------------------------------
# 候选文件把关（2026-10-01 实测真实数据后新增）
# ---------------------------------------------------------------------------
# 踩过的坑：微信目录树里散落着大量 .txt，但它们**不是**聊天记录导出：
#
#   clash 配置     port: 7890 / socks-port: / proxies: ...      （19851f698aa.yaml.txt）
#   json 配置      {"cache_time": 9200, "api_site": {...}}      （{(1).txt）
#   网页快照       01.html / 02.html / 03.html（3KB 的壳子）
#   转发推广文      2026综合津贴申领步骤.txt
#
# 旧逻辑对 SUPPORTED_EXT 一网打尽，结果这些全被当成"会话"入库，
# 统计里出现「会话 01」「会话 {(1)」「会话 19851f698aa.yaml」——
# 用户看到的是一堆垃圾，而不是聊天记录。
#
# 新逻辑：文件**必须自证是聊天记录**才能入库。

_CHAT_TS_PAT = re.compile(
    # 完整日期：2026-10-01 / 2026/10/1 / 2026年10月1日
    # 注意：日期部分必须**带分隔符且数字有界**，否则 "2026年度综合工薪补贴"
    # 会被 [年] 后的 \d{1,2} 误吞而当成时间戳（真实踩过的假阳性）。
    r"(20\d{2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}"
    r"|20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|20\d{2}\s*年\s*\d{1,2}\s*月(?![^\d]?\d)"      # 只到"月"也认，但不能续数字
    # 时间：14:30 / 下午2:30 / 上午 09:15
    r"|(?:上午|下午|凌晨|中午|晚上|早上|傍晚)?\s*\d{1,2}\s*[:：]\s*\d{2})"
)
_CHAT_SENDER_PAT = re.compile(r"^.{1,40}[:：]\s*$")
_CONFIG_FIRST_PAT = re.compile(
    r"^(port|socks-port|redir-port|mixed-port|allow-lan|mode|log-level|"
    r"external-controller|proxies|proxy-groups|rules|dns|profile|"
    r"experimental|tun|sniffer|hosts|interface-name)\s*:"
)


def looks_like_chat_export(p: Path) -> bool:
    """
    判断文件是否像**微信聊天记录导出**（用于 scan 阶段把关）。

    只读前 64KB，成本可控。

    判据分两条路走，避免误杀正规导出：

      ┌ 结构化导出（json / html / csv）─────────────────────────┐
      │ 这些格式**本来就以 { [ <!DOCTYPE 开头**，所以不能靠首字符排除。│
      │ 改为检查"有没有聊天记录的结构特征"：                       │
      │   json → 含 "sender"/"time_str"/"content"/"talker" 任一键   │
      │   html → 含 class="message" / class="time" / class="sender" │
      │   csv  → 表头含 sender/time/content/talker 任一列           │
      └──────────────────────────────────────────────────────────┘

      ┌ 纯文本（txt / md / log）─────────────────────────────────┐
      │  ✗ 二进制（含 \\x00）                                      │
      │  ✗ yaml/ini 配置首行（port: 7890 之类）                    │
      │  ✗ 行数 < 4                                                │
      │  ✓ >= 2 个时间戳行，或 >=1 时间行 + >=1「发件人：」行       │
      └──────────────────────────────────────────────────────────┘
    """
    suffix = p.suffix.lower()
    try:
        with p.open("rb") as fh:
            raw = fh.read(65536)
    except OSError:
        return False

    if not raw:
        return False
    if b"\x00" in raw[:1024]:
        return False

    text = raw.decode("utf-8", errors="replace").lstrip("\ufeff \t\r\n")
    head = text[:600]

    # ---- 结构化导出：靠内容特征，不靠首字符 ----
    if suffix in (".json",):
        return any(k in text for k in
                   ('"sender"', '"time_str"', '"content"', '"talker"',
                    '"is_self"', '"msg_id"'))
    if suffix in (".html", ".htm"):
        if any(k in text for k in
               ('class="message"', "class='message'", 'class="time"',
                'class="sender"', 'class="content"', "bubble")):
            return True
        # 没有消息结构 → 当普通网页，继续走文本判据兜底
        head = text[:2000]

    if suffix == ".csv":
        first_line = text.split("\n", 1)[0].strip().strip('"')
        cols = {c.strip().strip('"').lower()
                for c in re.split(r"[,;\t]", first_line)}
        # 微信 4.x 官方导出的真实表头（2026-10 实测样本确认）：
        #   localId,TalkerId,Type,IsSender,CreateTime,StrTime,StrContent,NickName,Remark
        WX_COLS = {
            "localid", "talkerid", "issender", "createtime",
            "strtime", "strcontent", "nickname", "remark",
        }
        # 通用/英文列名 + 中文列名
        GENERIC = {"sender", "time", "time_str", "content", "talker",
                   "from", "timestamp", "text",
                   "消息", "发送者", "时间", "内容", "发送时间"}
        # 命中 >= 2 个微信官方列名，或命中 1 个通用列名即可
        if len(cols & WX_COLS) >= 2 or (cols & GENERIC):
            return True
        return False

    # ---- 纯文本路径 ----
    if head.startswith("<?xml"):
        return False
    first = head.split("\n", 1)[0].strip()
    if _CONFIG_FIRST_PAT.match(first):
        return False
    if re.match(r"^[A-Za-z_][\w-]{0,30}:\s*\S", first) and \
       not _CHAT_TS_PAT.search(first):
        return False

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False

    probe = lines[:200]
    ts = sum(1 for ln in probe if _CHAT_TS_PAT.search(ln))
    snd = sum(1 for ln in probe if _CHAT_SENDER_PAT.match(ln))

    # ---- 判据（2026-10-01 经真实样本双向校准）----
    #
    # 假阳性陷阱：一份 6 行推广文恰好第 0 行以「：」结尾、第 5 行含真实日期，
    # 旧的 `ts>=1 and snd>=1` 直接放行 → 被当成会话「2026综合津贴申领步骤」。
    #
    # 假阴性陷阱：`工作群.txt` 只有 3 行（标题 + 1 条消息 + 内容），
    # 是**合法的**极小导出，门槛设太高压根收不进来。
    #
    # 校准后的判据分三档，按证据强度递进：
    #   ① ts >= 2                      → 有多条带时间的消息，直接确认为聊天
    #   ② ts >= 1 且每行平均长度 >= 8   → 模板化的"标题+单条消息"短导出
    #   ③ ts >= 3                      → 长文件（冗余保险，密度天然满足）
    # 其余一律拒绝。
    if ts >= 2:
        return True
    if ts >= 3:
        return True
    if ts >= 1:
        avg_len = sum(len(ln) for ln in probe) / len(probe)
        # 推广文/配置文件的特征是"少数长行 + 无消息结构"，
        # 平均行长明显偏大时不再放行
        return avg_len <= 40
    return False


# 多账号隔离：直接躺在收件箱根目录的文件归属此账号
DEFAULT_ACCOUNT = "默认账号"
MEDIA_EXT = {".dat", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".mp4", ".amr", ".silk", ".mp3", ".wav"}

# ---------------------------------------------------------------------------
# 数据库 schema
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- 归档的源文件（用于增量判断与追溯）
CREATE TABLE IF NOT EXISTS source_file (
    path        TEXT PRIMARY KEY,   -- 源文件绝对路径
    sha256      TEXT NOT NULL,      -- 内容指纹
    size        INTEGER,            -- 字节数
    mtime       REAL,               -- 修改时间
    account     TEXT DEFAULT '',    -- 所属微信号（多账号隔离的关键）
    conv_id     TEXT,               -- 解析出的会话 ID（全局唯一，含账号前缀）
    n_msg       INTEGER DEFAULT 0,  -- 文件内消息条数（含与库中重复的）
    n_added     INTEGER DEFAULT 0,  -- 本文件净贡献的新消息数（去重后）
    fmt         TEXT,               -- csv/json/html/txt
    archived_at INTEGER,            -- 首次归档时间
    updated_at  INTEGER             -- 最近更新时间
);

-- 微信号（多账号隔离）
CREATE TABLE IF NOT EXISTS account (
    account     TEXT PRIMARY KEY,   -- 微信号标识（来自 inbox 子目录名）
    alias       TEXT,               -- 显示名（可自定义）
    n_conv      INTEGER DEFAULT 0,
    n_msg       INTEGER DEFAULT 0,
    first_seen  INTEGER,
    last_seen   INTEGER
);

-- 会话
CREATE TABLE IF NOT EXISTS conv (
    conv_id     TEXT PRIMARY KEY,   -- 全局唯一：account::title
    account     TEXT DEFAULT '',    -- 所属微信号
    title       TEXT,               -- 会话名（原始）
    is_group    INTEGER DEFAULT 0,
    members     TEXT DEFAULT '[]',
    n_msg       INTEGER DEFAULT 0,
    start_ts    INTEGER,
    end_ts      INTEGER,
    sources     TEXT DEFAULT '[]',  -- 该会话数据来自哪些文件
    created_at  INTEGER,
    updated_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_conv_account ON conv(account);

-- 消息（全量历史，只增不改）
CREATE TABLE IF NOT EXISTS msg (
    msg_id      TEXT PRIMARY KEY,
    conv_id     TEXT NOT NULL,
    account     TEXT DEFAULT '',    -- 所属微信号（冗余，方便直接过滤）
    seq         INTEGER,
    ts          INTEGER,
    time_str    TEXT,
    sender      TEXT,
    is_self     INTEGER DEFAULT 0,
    type        TEXT,
    content     TEXT,
    media       TEXT,            -- 展示用媒体相对路径（通常是缩略图）
    media_full  TEXT,            -- 原图相对路径（WxAM 解码产物；点开大图时用）
    source_file TEXT,
    created_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_msg_conv_ts ON msg(conv_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_ts      ON msg(ts);
CREATE INDEX IF NOT EXISTS idx_msg_type    ON msg(type);
CREATE INDEX IF NOT EXISTS idx_msg_account ON msg(account);

-- 全文索引（trigram，适合中文）；account 不索引但随行存储，便于过滤
CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(
    content, sender, conv_id UNINDEXED, msg_id UNINDEXED, account UNINDEXED,
    tokenize='trigram'
);

-- 归档日志
CREATE TABLE IF NOT EXISTS archive_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT,       -- scan / import / move / verify
    detail      TEXT,
    n_ok        INTEGER DEFAULT 0,
    n_skip      INTEGER DEFAULT 0,
    n_fail      INTEGER DEFAULT 0,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def now_ts() -> int:
    return int(time.time())


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    """流式计算文件 SHA-256（大文件不爆内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def migrate_schema(conn: sqlite3.Connection, verbose: bool = False) -> list[str]:
    """把旧版本建的库原地升级到当前 schema（幂等）。

    为什么需要：`CREATE TABLE IF NOT EXISTS` 对**已存在**的表不生效。
    早期版本的表没有 account 列（多账号隔离是后加的），直接查会
    `no such column: account`；而 SCHEMA 里的 `CREATE INDEX ... ON conv(account)`
    同样会失败。所以必须**先补列，再跑 SCHEMA**。
    """
    changes: list[str] = []
    tables = _existing_tables(conn)

    def cols(table: str) -> set[str]:
        try:
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            return set()

    def add_col(table: str, col: str, decl: str) -> None:
        if table in tables and col not in cols(table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            changes.append(f"{table}.{col}")

    add_col("source_file", "account", "TEXT DEFAULT ''")
    add_col("source_file", "conv_id", "TEXT")
    add_col("source_file", "n_added", "INTEGER DEFAULT 0")
    add_col("conv", "account", "TEXT DEFAULT ''")
    add_col("msg", "account", "TEXT DEFAULT ''")

    # FTS 表若缺 account 列，必须整表重建（FTS5 不支持 ADD COLUMN）
    fts_cols = cols("msg_fts")
    if fts_cols and "account" not in fts_cols:
        conn.executescript("""
            DROP TABLE IF EXISTS msg_fts;
            CREATE VIRTUAL TABLE msg_fts USING fts5(
                content, sender, conv_id UNINDEXED, msg_id UNINDEXED,
                account UNINDEXED, tokenize='trigram'
            );
            INSERT INTO msg_fts (content, sender, conv_id, msg_id, account)
                SELECT content, sender, conv_id, msg_id,
                       COALESCE(account, '') FROM msg;
        """)
        changes.append("msg_fts 重建")

    # 旧库里 account 可能为空 → 回填默认账号
    if "account" in cols("conv"):
        conn.execute("UPDATE conv SET account=? WHERE account IS NULL OR account=''",
                     (DEFAULT_ACCOUNT,))
    if "account" in cols("msg"):
        conn.execute("UPDATE msg SET account=? WHERE account IS NULL OR account=''",
                     (DEFAULT_ACCOUNT,))

    if changes:
        conn.commit()
        if verbose:
            print(f"  [迁移] 数据库 schema 升级: {', '.join(changes)}")
    return changes


def open_store(store_dir: str | Path, verbose: bool = False) -> sqlite3.Connection:
    """打开（必要时创建）归档库。旧库会自动原地迁移，不丢数据。"""
    store = Path(store_dir)
    store.mkdir(parents=True, exist_ok=True)
    db = store / "vault.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    # ① 先迁移旧表（补列 / 重建 FTS）
    migrate_schema(conn, verbose=verbose)
    # ② 再跑完整 SCHEMA（此时 CREATE INDEX ON conv(account) 才成立）
    conn.executescript(SCHEMA)
    # 记录库根目录，供媒体解码输出定位 <store>/media
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('store_dir', ?)",
                 (str(store.resolve()),))
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# 核心：增量导入
# ---------------------------------------------------------------------------

def _media_store_dir(conn: sqlite3.Connection) -> Path | None:
    """取媒体输出目录（<store>/media），由 meta 表记录 store 根。"""
    row = conn.execute("SELECT v FROM meta WHERE k='store_dir'").fetchone()
    if not row:
        return None
    return Path(row["v"]) / "media"


def decode_media_for_conv(conn: sqlite3.Connection, msgs: list[dict],
                          aes_key: bytes | None = None) -> int:
    """
    把消息里引用的 .dat / 媒体文件解码到 <store>/media/，并回写 media 字段为
    相对路径（可直接被查看器通过 /media/<rel> 访问）。

    返回成功解码的文件数。无解码器或无可解码媒体时静默返回 0。
    """
    if D is None:
        return 0
    out_root = _media_store_dir(conn)
    if out_root is None:
        return 0
    n_ok = 0
    cache: dict[str, str | None] = {}
    for m in msgs:
        src = m.get("media")
        if not src:
            continue
        sp = Path(src)
        if not sp.is_absolute():
            # 相对路径：相对源文件所在目录解析
            base = m.get("_src_dir")
            if base:
                sp = Path(base) / src
        if not sp.exists() or sp.suffix.lower() not in MEDIA_EXT:
            continue
        key = str(sp)
        if key in cache:
            if cache[key]:
                m["media"] = cache[key]
            continue
        try:
            blob = sp.read_bytes()
            if sp.suffix.lower() == ".dat":
                res = D.decode_dat(blob, aes_key)
                if not res.get("ok"):
                    cache[key] = None
                    continue
                data = res["data"]
                ext = (res["ext"] or "bin").lstrip(".").lower()
            else:
                data = blob
                ext = sp.suffix.lstrip(".").lower()
            out_root.mkdir(parents=True, exist_ok=True)
            # 优先用干净文件名；仅当同名文件内容不同才加哈希后缀，避免互相覆盖
            dst = out_root / f"{sp.stem}.{ext}"
            if dst.exists():
                try:
                    if dst.read_bytes() != data:
                        h8 = hashlib.sha1(str(sp).encode("utf-8")).hexdigest()[:8]
                        dst = out_root / f"{sp.stem}_{h8}.{ext}"
                except Exception as e:
                    log.warning("媒体写入失败，该文件未落盘：%s", e)
                    pass
            if not dst.exists():
                dst.write_bytes(data)
            rel = f"/media/{dst.name}"     # 可直接作为前端 <img src> 使用
            m["media"] = rel
            cache[key] = rel
            n_ok += 1
        except Exception:
            cache[key] = None
    return n_ok


def _account_of(conn: sqlite3.Connection, path: Path, source_root: Path | None) -> str:
    """
    从文件在收件箱中的相对路径推断所属微信号。

    约定（多账号隔离的核心）：
        inbox/<账号名>/xxx.txt   → account = "<账号名>"
        inbox/xxx.txt            → account = "默认账号"

    这样不同微信号各投一个子目录，天然互不干扰，同名会话也不会撞车。
    """
    if source_root is None:
        return DEFAULT_ACCOUNT
    try:
        rel = Path(path).resolve().relative_to(Path(source_root).resolve())
    except ValueError:
        return DEFAULT_ACCOUNT
    # 直接躺在收件箱根 → 归入默认账号
    if len(rel.parts) < 2:
        return DEFAULT_ACCOUNT
    # 取第一级子目录名作为账号名（更深层的目录仍属同一账号）
    name = rel.parts[0].strip()
    if not name or name.startswith((".", "_", "~")):
        return DEFAULT_ACCOUNT
    return name[:64]


def refresh_account(conn: sqlite3.Connection, account: str) -> None:
    """重算某账号的会话数 / 消息数 / 首末时间，写入 account 表。"""
    row = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(n_msg),0) m, "
        "MIN(start_ts) a, MAX(end_ts) b FROM conv WHERE account=?",
        (account,),
    ).fetchone()
    conn.execute(
        "INSERT INTO account (account, alias, n_conv, n_msg, first_seen, last_seen) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(account) DO UPDATE SET "
        "  n_conv=excluded.n_conv, n_msg=excluded.n_msg, "
        "  first_seen=excluded.first_seen, last_seen=excluded.last_seen",
        (account, account, row["c"], row["m"], row["a"], row["b"]),
    )


def import_file(conn: sqlite3.Connection, path: Path, verbose: bool = True,
                source_root: Path | None = None) -> dict:
    """
    把一个聊天记录文件增量导入归档库。

    source_root: 收件箱根目录，用于推断文件所属账号（多账号隔离）。
                 不传则全部归入 DEFAULT_ACCOUNT。

    返回 {'status': 'new'|'skip'|'updated'|'error', 'n_msg': int, ...}
    """
    st = path.stat()
    fhash = sha256_file(path)
    abs_path = str(path.resolve())
    ts_now = now_ts()
    account = _account_of(conn, path, source_root)

    row = conn.execute(
        "SELECT sha256, conv_id, n_msg FROM source_file WHERE path=?", (abs_path,)
    ).fetchone()

    if row and row["sha256"] == fhash:
        return {"status": "skip", "reason": "内容未变化", "conv_id": row["conv_id"]}

    # 解析
    try:
        conv = P.parse_file(str(path))
    except Exception as e:
        conn.execute(
            "INSERT INTO archive_log (action, detail, n_fail, created_at) VALUES (?,?,?,?)",
            ("import", f"{abs_path}: {e}", 1, ts_now),
        )
        conn.commit()
        return {"status": "error", "error": str(e)}

    msgs = conv["messages"]
    # 会话 ID 全局唯一化：account::原标题。
    # 不同微信号可能有同名好友/同名群，不加前缀就会互相覆盖。
    raw_cid = conv["conv_id"]
    cid = f"{account}::{raw_cid}"

    # 为媒体解码器标注源文件目录（相对 media 路径的解析基准）
    for m in msgs:
        m["_src_dir"] = str(path.parent)
    # 解码媒体（.dat → 可读图片等），失败静默跳过，不影响归档
    decode_media_for_conv(conn, msgs)

    is_new = row is None
    n_added = 0

    # 会话 upsert
    old_conv = conn.execute("SELECT sources, n_msg FROM conv WHERE conv_id=?", (cid,)).fetchone()
    sources = json.loads(old_conv["sources"]) if old_conv and old_conv["sources"] else []
    if abs_path not in sources:
        sources.append(abs_path)

    # 增量写入消息：已存在的 msg_id 跳过
    existing = {
        r[0] for r in conn.execute(
            "SELECT msg_id FROM msg WHERE conv_id=?", (cid,)
        )
    }
    total_before = len(existing)
    for m in msgs:
        if m["msg_id"] in existing:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO msg (msg_id, conv_id, account, seq, ts, time_str, sender, "
            "is_self, type, content, media, source_file, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m["msg_id"], cid, account, m["seq"], m["ts"], m["time_str"], m["sender"],
             int(m["is_self"]), m["type"], m["content"], m["media"], abs_path, ts_now),
        )
        if m["content"] and m["type"] in ("text", "link", "system", "other"):
            conn.execute(
                "INSERT INTO msg_fts (content, sender, conv_id, msg_id, account) "
                "VALUES (?,?,?,?,?)",
                (m["content"], m["sender"], cid, m["msg_id"], account),
            )
        n_added += 1

    # 重算会话统计
    agg = conn.execute(
        "SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM msg WHERE conv_id=?", (cid,)
    ).fetchone()

    # 合并成员
    senders = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT sender FROM msg WHERE conv_id=? AND sender NOT IN ('我','对方','')",
            (cid,)
        )
    }
    is_group = 1 if len(senders) > 2 else 0

    conn.execute(
        "INSERT INTO conv (conv_id, account, title, is_group, members, n_msg, start_ts, end_ts, "
        "sources, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(conv_id) DO UPDATE SET "
        "  account=excluded.account, "
        "  title=excluded.title, is_group=excluded.is_group, members=excluded.members, "
        "  n_msg=excluded.n_msg, start_ts=excluded.start_ts, end_ts=excluded.end_ts, "
        "  sources=excluded.sources, updated_at=excluded.updated_at",
        (cid, account, conv["title"], is_group, json.dumps(sorted(senders), ensure_ascii=False),
         agg["n"], agg["a"], agg["b"], json.dumps(sources, ensure_ascii=False),
         ts_now, ts_now),
    )

    # 记录源文件
    conn.execute(
        "INSERT INTO source_file (path, sha256, size, mtime, account, conv_id, n_msg, n_added, "
        "fmt, archived_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, size=excluded.size, "
        "  mtime=excluded.mtime, account=excluded.account, "
        "  conv_id=excluded.conv_id, n_msg=excluded.n_msg, "
        "  n_added=excluded.n_added, fmt=excluded.fmt, updated_at=excluded.updated_at",
        (abs_path, fhash, st.st_size, st.st_mtime, account, cid, len(msgs), n_added,
         conv["source"], ts_now, ts_now),
    )

    # 刷新账号汇总（多账号侧栏用）
    refresh_account(conn, account)

    conn.execute(
        "INSERT INTO archive_log (action, detail, n_ok, created_at) VALUES (?,?,?,?)",
        ("import", f"{abs_path} → {cid}", n_added, ts_now),
    )
    conn.commit()

    status = "new" if is_new else ("updated" if n_added else "skip")
    if verbose:
        tag = {"new": "新增", "updated": "更新", "skip": "跳过"}[status]
        print(f"  [{tag}] {path.name:28s} → {cid:20s} +{n_added} 条 "
              f"(该会话共 {agg['n']} 条)")

    return {
        "status": status, "conv_id": cid, "n_msg": len(msgs),
        "n_added": n_added, "total": agg["n"], "dup_in_file": total_before,
    }


def scan(conn: sqlite3.Connection, source_dir: Path, verbose: bool = True,
         exclude_dirs: tuple[str, ...] = ()) -> dict:
    """扫描目录下所有聊天记录文件并增量导入。

    exclude_dirs: 要跳过的顶层子目录名。典型用法是排除存档轨投放口
                  （inbox/raw），那里的 .bak / 无扩展名分片不是聊天记录，
                  属于 wv_raw.py 的职责。
    """
    if not source_dir.is_dir():
        raise NotADirectoryError(source_dir)

    excluded = {d.strip().strip("/") for d in exclude_dirs if d and d.strip()}

    files: list[Path] = []
    rejected: Counter = Counter()
    for root, dirs, fns in os.walk(source_dir):
        # 跳过归档库自身的文件
        if Path(root).resolve() == Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve().parent:
            continue
        # 顶层子目录命中排除 → 从遍历中剪枝
        rel = Path(root).resolve().relative_to(Path(source_dir).resolve())
        if rel.parts and rel.parts[0] in excluded:
            dirs[:] = []
            continue
        for fn in sorted(fns):
            # 跳过隐藏文件、临时文件、同步脚本自身产物
            if fn.startswith((".", "~$")) or fn.startswith("_"):
                continue
            if fn.lower() in ("sync.log", "thumbs.db", "desktop.ini"):
                continue
            p = Path(root) / fn
            if p.suffix.lower() not in SUPPORTED_EXT:
                continue
            # ★ 把关：必须是聊天记录导出，不能是配置/网页/垃圾文本
            if not looks_like_chat_export(p):
                rejected[p.suffix.lower()] += 1
                continue
            files.append(p)

    if verbose:
        print(f"发现 {len(files)} 个候选文件"
              + (f"（已排除目录: {', '.join(sorted(excluded))}）" if excluded else ""))
        if rejected:
            print(f"  ↳ 已滤除 {sum(rejected.values())} 个非聊天记录文件："
                  f"{dict(rejected)}")

    stat = {"new": 0, "skip": 0, "updated": 0, "error": 0, "added": 0}
    for p in files:
        r = import_file(conn, p, verbose, source_root=source_dir)
        stat[r["status"]] = stat.get(r["status"], 0) + 1
        stat["added"] += r.get("n_added", 0)

    return stat


# ---------------------------------------------------------------------------
# MANIFEST 完整性清单
# ---------------------------------------------------------------------------

def write_manifest(conn: sqlite3.Connection, store_dir: Path) -> Path:
    """生成 MANIFEST.json：记录所有源文件的指纹，用于校验与迁移。"""
    rows = conn.execute(
        "SELECT path, sha256, size, mtime, conv_id, n_msg, n_added, fmt, archived_at "
        "FROM source_file ORDER BY path"
    ).fetchall()

    files = [dict(r) for r in rows]
    total_bytes = sum(f["size"] or 0 for f in files)

    convs = [dict(r) for r in conn.execute(
        "SELECT conv_id, title, n_msg, start_ts, end_ts FROM conv ORDER BY conv_id")]
    total_msg = conn.execute("SELECT COUNT(*) FROM msg").fetchone()[0]

    manifest = {
        "vault_version": "1.0",
        "generated_at": datetime.now(CST).isoformat(),
        "generated_ts": now_ts(),
        "summary": {
            "n_source_files": len(files),
            "n_conversations": len(convs),
            "n_messages": total_msg,
            "n_messages_in_files": sum(f["n_msg"] or 0 for f in files),
            "n_messages_added": sum(f["n_added"] or 0 for f in files),
            "total_bytes": total_bytes,
            "_note": ("n_messages 为库中实际去重后的消息总数；"
                      "n_messages_in_files 为各源文件条数之和（同一消息可能出现在"
                      "多个导出文件中，故该值可能大于前者）；"
                      "n_messages_added 为各文件净贡献（去重后）之和，"
                      "仅在首次全量导入时与 n_messages 相等。"),
        },
        "files": files,
        "conversations": convs,
    }

    out = store_dir / "MANIFEST.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def verify_manifest(conn: sqlite3.Connection, store_dir: Path, verbose: bool = True) -> dict:
    """校验源文件指纹是否仍与入库时一致（检测篡改/损坏）。"""
    rows = conn.execute("SELECT path, sha256 FROM source_file").fetchall()
    ok = miss = bad = 0
    problems: list[str] = []

    for r in rows:
        p = Path(r["path"])
        if not p.exists():
            miss += 1
            problems.append(f"缺失: {p}")
            continue
        actual = sha256_file(p)
        if actual != r["sha256"]:
            bad += 1
            problems.append(f"指纹不符: {p}")
        else:
            ok += 1

    if verbose:
        print(f"校验完成：正常 {ok} / 缺失 {miss} / 变更 {bad}")
        for x in problems[:20]:
            print("   ", x)

    return {"ok": ok, "missing": miss, "changed": bad, "problems": problems}


# ---------------------------------------------------------------------------
# 冷归档搬运
# ---------------------------------------------------------------------------

def archive_move(source_dir: Path, dest_dir: Path, after_days: int = 7,
                 verbose: bool = True) -> dict:
    """
    把 source_dir 里超过 after_days 天未修改的文件搬到 dest_dir，保持目录结构。
    用于热（SSD）→ 冷（HDD）分层。
    """
    source_dir = Path(source_dir).resolve()
    dest_dir = Path(dest_dir).resolve()
    cutoff = time.time() - after_days * 86400

    if source_dir == dest_dir:
        raise ValueError("源目录与目标目录相同")

    moved = skipped = 0
    for root, _dirs, fns in os.walk(source_dir):
        rel = Path(root).relative_to(source_dir)
        for fn in sorted(fns):
            p = Path(root) / fn
            if p.stat().st_mtime > cutoff:
                skipped += 1
                continue
            target = dest_dir / rel / fn
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                # 已存在同名 → 校验指纹，一致则删除源文件
                if sha256_file(p) == sha256_file(target):
                    p.unlink()
                    moved += 1
                    continue
                target = target.with_suffix(target.suffix + f".{int(p.stat().st_mtime)}")
            shutil.move(str(p), str(target))
            moved += 1
            if verbose:
                print(f"  → {rel / fn}")

    if verbose:
        print(f"搬运完成：移动 {moved} / 跳过（近期）{skipped}")
    return {"moved": moved, "skipped": skipped}


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def show_stats(conn: sqlite3.Connection) -> None:
    total = conn.execute("SELECT COUNT(*) FROM msg").fetchone()[0]
    if not total:
        print("归档库为空")
        return

    n_conv = conn.execute("SELECT COUNT(*) FROM conv").fetchone()[0]
    n_file = conn.execute("SELECT COUNT(*) FROM source_file").fetchone()[0]
    span = conn.execute("SELECT MIN(ts) a, MAX(ts) b FROM msg").fetchone()
    n_self = conn.execute("SELECT COUNT(*) FROM msg WHERE is_self=1").fetchone()[0]

    print("=" * 62)
    print("  WeChat Vault · 归档库统计")
    print("=" * 62)
    print(f"  会话数      {n_conv}")
    print(f"  源文件数    {n_file}")
    print(f"  消息总数    {total}")
    print(f"  我发出      {n_self}  ({n_self/total*100:.1f}%)")
    print(f"  对方发出    {total-n_self}  ({(total-n_self)/total*100:.1f}%)")
    print(f"  时间跨度    {P.fmt_time(span['a'])} → {P.fmt_time(span['b'])}")
    print()

    # 账号分布（多账号隔离的可视化确认）
    accs = list(conn.execute(
        "SELECT account, n_conv, n_msg FROM account ORDER BY n_msg DESC"))
    if len(accs) > 1:
        print(f"  账号分布（共 {len(accs)} 个微信号，互不干扰）：")
        for r in accs:
            print(f"    {r['account']:20s} {r['n_conv']:5d} 会话 / {r['n_msg']:7d} 条")
        print()

    print("  消息类型分布：")
    for r in conn.execute(
        "SELECT type, COUNT(*) n FROM msg GROUP BY type ORDER BY n DESC LIMIT 10"
    ):
        bar = "█" * max(1, int(r["n"] / total * 40))
        print(f"    {r['type']:10s} {r['n']:7d}  {bar}")
    print()

    print("  会话排行（按消息数）：")
    for r in conn.execute(
        "SELECT title, n_msg, is_group, start_ts, end_ts FROM conv "
        "ORDER BY n_msg DESC LIMIT 12"
    ):
        tag = "群" if r["is_group"] else "  "
        print(f"    [{tag}] {r['title']:22s} {r['n_msg']:7d} 条   "
              f"{P.fmt_time(r['start_ts'])[:10]} ~ {P.fmt_time(r['end_ts'])[:10]}")
    print("=" * 62)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    """统一日志配置：级别取 WV_LOG_LEVEL（默认 INFO），已配置则不覆盖。

    为什么需要它：本项目原先 153 处 print、0 处 logging —— 无法分级、无法过滤、
    无法接入告警。改用 logging 后，默认行为是 WARNING 以上打到 stderr，
    但 info/debug 需要显式配置级别，故在此统一初始化。
    """
    import logging as _lg
    if _lg.getLogger().handlers:
        return
    lvl = (os.environ.get("WV_LOG_LEVEL") or "INFO").upper()
    _lg.basicConfig(
        level=getattr(_lg, lvl, _lg.INFO),
        format="%(asctime)s %(levelname).1s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main():
    _setup_logging()
    ap = argparse.ArgumentParser(description="WeChat Vault 归档器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="扫描并增量导入")
    p_scan.add_argument("--source", required=True, help="聊天记录所在目录")
    p_scan.add_argument("--store", required=True, help="归档库目录")
    p_scan.add_argument("--exclude-dir", action="append", default=[],
                        help="要跳过的顶层子目录（可重复，如 --exclude-dir raw）")
    # 详细输出默认开启（便于在容器日志里回溯）；-v 显式打开，-q 静音
    p_scan.add_argument("-v", "--verbose", action="store_true", default=True)
    p_scan.add_argument("-q", "--quiet", action="store_true",
                        help="静音（只输出汇总错误）")

    p_stat = sub.add_parser("stats", help="显示统计")
    p_stat.add_argument("--store", required=True)

    p_ver = sub.add_parser("verify", help="校验完整性")
    p_ver.add_argument("--store", required=True)

    p_man = sub.add_parser("manifest", help="生成 MANIFEST")
    p_man.add_argument("--store", required=True)

    p_arc = sub.add_parser("archive", help="冷归档搬运")
    p_arc.add_argument("--source", required=True)
    p_arc.add_argument("--dest", required=True)
    p_arc.add_argument("--days", type=int, default=7)

    p_mir = sub.add_parser("mirror", help="镜像归档库到冷存储（纯 Python）")
    p_mir.add_argument("--src", required=True, help="热数据目录")
    p_mir.add_argument("--dst", required=True, help="冷存储目录")
    p_mir.add_argument("--no-delete", action="store_true", help="不删除冷端多余文件")

    args = ap.parse_args()

    if args.cmd == "scan":
        t0 = time.time()
        verbose = not getattr(args, "quiet", False)
        conn = open_store(args.store, verbose=verbose)
        if verbose:
            print(f"归档库: {Path(args.store).resolve() / 'vault.db'}")
            print(f"扫描源: {Path(args.source).resolve()}\n")
        st = scan(conn, Path(args.source), verbose=verbose,
                  exclude_dirs=tuple(args.exclude_dir or []))
        mf = write_manifest(conn, Path(args.store))
        print(f"\n完成：新增 {st['new']} / 更新 {st['updated']} / 跳过 {st['skip']} "
              f"/ 失败 {st['error']}，共入库 {st['added']} 条消息（{time.time()-t0:.1f}s）")
        print(f"清单: {mf}")
        conn.close()

    elif args.cmd == "stats":
        conn = open_store(args.store)
        show_stats(conn)
        conn.close()

    elif args.cmd == "verify":
        conn = open_store(args.store)
        verify_manifest(conn, Path(args.store))
        conn.close()

    elif args.cmd == "manifest":
        conn = open_store(args.store)
        mf = write_manifest(conn, Path(args.store))
        print(f"已生成: {mf}")
        conn.close()

    elif args.cmd == "archive":
        archive_move(Path(args.source), Path(args.dest), args.days)

    elif args.cmd == "mirror":
        # 复用同目录的 wv_mirror.py
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import wv_mirror as M
        print(f"冷备镜像: {args.src} → {args.dst}")
        st = M.mirror(Path(args.src), Path(args.dst), delete=not args.no_delete)
        print(f"完成：复制 {st['copied']} / 跳过 {st['skipped']} / 删除 {st['deleted']} "
              f"/ 错误 {st['errors']}（{st['bytes']} 字节）")


if __name__ == "__main__":
    main()
