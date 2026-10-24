#!/usr/bin/env python3
"""从微信客户端数据库直接同步消息到 vault.db（自动同步链路的第二步）。

微信 4.x 登录后，消息以 SQLCipher 4 加密库落在
`xwechat_files/<wxid>/db_storage/message/message_*.db`。本模块：

  1. 解密 —— 优先用账号级 passphrase（passphrase.json）逐库派生密钥，
     库被微信重建后依然能解；否则回退到 all_keys.json 的 enc_key 快照
  2. 入库 —— 逐库读 Msg_* 表，转成 vault.db 的 conv/msg 通用结构

与 `wv_woc.py`（吃迁移备份包）互补：本模块吃的是客户端正在用的「活库」，
配合 `wv_autosync.py` 的指纹监视，即构成「微信一有新消息就自动入库」。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import zstandard as zstd
except ImportError:
    zstd = None

try:
    from Crypto.Cipher import AES as _AES
except ImportError:
    _AES = None

log = logging.getLogger("wv_sync")

DEFAULT_ACCOUNT = "默认账号"
MSG_PREFIX = "db-"

_PAGE_SZ = 4096
_RESERVE = 80
_IV_OFF = 4016
_HMAC_OFF = 4032
_SALT_SZ = 16

BASE_TYPE = {
    1: "text", 3: "image", 34: "voice", 37: "friend", 42: "card",
    43: "video", 47: "emoji", 48: "location", 49: "link",
    50: "call", 10000: "system", 10002: "revoke",
}
# local_type 高位是 WCDB 标志位，取低 32 位才是微信定义的消息类型

def load_keys(keys_path: Path) -> dict:
    """读取 all_keys.json，返回 {相对路径: (enc_key_hex, salt_hex)}。"""
    raw = json.loads(keys_path.read_text(encoding="utf-8"))
    out: dict[str, tuple[str, str]] = {}
    if "keys" in raw and isinstance(raw["keys"], dict):
        for k, v in raw["keys"].items():
            out[k] = (v, "")
        return out
    for k, v in raw.items():
        if isinstance(v, dict) and v.get("enc_key"):
            out[k] = (v["enc_key"], v.get("salt", ""))
    return out

def load_passphrase(keys_path: Path) -> str:
    """读取同目录 passphrase.json 里的账号级 password（64 hex）。"""
    cand = keys_path.parent / "passphrase.json"
    if not cand.is_file():
        return ""
    try:
        v = json.loads(cand.read_text(encoding="utf-8")).get("passphrase", "")
        return v if (isinstance(v, str) and len(v) == 64) else ""
    except (ValueError, OSError):
        return ""

def find_account(wechat_files: Path, wxid: str = "") -> Path:
    """在 xwechat_files 下定位账号目录（含 db_storage 的那个）。"""
    cands = [p for p in wechat_files.glob("*_*") if (p / "db_storage").is_dir()]
    if not cands:
        raise SystemExit("未找到账号目录（需要 <wxid>_<后缀>/db_storage）：%s" % wechat_files)
    if wxid:
        hit = [p for p in cands if p.name.startswith(wxid)]
        if hit:
            return hit[0]
    return sorted(cands)[0]

def _mac_key(enc_key: bytes, salt: bytes) -> bytes:
    """SQLCipher 4 的 HMAC 校验 key：PBKDF2(enc_key, salt^0x3a, 2 轮)。"""
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)

def decrypt_one(src: Path, dst: Path, enc_key: str, salt: str = "",
                sqlcipher: str = "", passphrase_hex: str = "") -> bool:
    """纯 Python 解密单个 SQLCipher 4 库（不依赖外部 sqlcipher）。"""
    if _AES is None:
        log.error("缺少 pycryptodome，无法解密")
        return False
    try:
        blob = src.read_bytes()
    except OSError as e:
        log.warning("读取失败 %s：%s", src.name, e)
        return False
    if len(blob) < _PAGE_SZ:
        return False

    salt_bytes = blob[:_SALT_SZ]
    if passphrase_hex:
        pw = passphrase_hex.strip().lower()
        if len(pw) != 64:
            log.error("passphrase 长度异常：%s", src.name)
            return False
        aes_key = hashlib.pbkdf2_hmac("sha512", bytes.fromhex(pw),
                                      salt_bytes, 256000, dklen=32)
    else:
        key_hex = enc_key.strip().lower()
        if len(key_hex) != 64:
            log.error("enc_key 长度异常（需要 64 hex，实际 %d）：%s", len(key_hex), src.name)
            return False
        aes_key = bytes.fromhex(key_hex)

    out = bytearray()
    # SQLCipher 4 页结构（4096B/页，reserve 80B）：
    #   页 0   [0:16) 盐（参与密钥派生） | [16:4016) 密文
    #   页 n>0 [0:4016) 密文 | [4016:4032) IV | [4032:4096) 页 HMAC
    # 每页独立 CBC，IV 就在页尾；reserve 区不参与解密，原位补零即可。
    n_pages = len(blob) // _PAGE_SZ
    for pi in range(n_pages):
        page = blob[pi * _PAGE_SZ:(pi + 1) * _PAGE_SZ]
        iv = page[_IV_OFF:_HMAC_OFF]
        if pi == 0:
            ct = page[_SALT_SZ:_IV_OFF]
            pt = _AES.new(aes_key, _AES.MODE_CBC, iv).decrypt(ct)
            # 明文页 0 要还原 SQLite 头（加密状态下被盐占据）
            out += b"SQLite format 3\x00" + pt + b"\x00" * _RESERVE
        else:
            ct = page[:_IV_OFF]
            pt = _AES.new(aes_key, _AES.MODE_CBC, iv).decrypt(ct)
            out += pt + b"\x00" * _RESERVE
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(bytes(out))
    return True

def decrypt_all(account: Path, plain_out: Path, keys: dict,
                sqlcipher: str = "", passphrase_hex: str = "") -> int:
    """解密 db_storage 下所有库，保持相对路径结构。返回成功数。"""
    db_dir = account / "db_storage"
    ok = 0
    total = 0
    for src in sorted(db_dir.rglob("*.db")):
        rel = src.relative_to(db_dir).as_posix()
        ent = keys.get(rel)
        if not ent:
            log.debug("无密钥，跳过：%s", rel)
            continue
        total += 1
        if decrypt_one(src, plain_out / rel, ent[0], ent[1], passphrase_hex=passphrase_hex):
            ok += 1
    log.info("解密完成：%d/%d", ok, total)
    return ok

def _ro(p: Path) -> sqlite3.Connection:
    """只读模式打开 SQLite，避免干扰微信正在写的库。"""
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True)

def read_contacts(plain: Path) -> dict[int, dict]:
    """contact.id → {username, remark, nick}（real_sender_id 的映射源）。"""
    out: dict[int, dict] = {}
    p = plain / "contact" / "contact.db"
    if not p.is_file():
        log.warning("缺少 contact.db，发送者将显示为数字 ID")
        return out
    c = _ro(p)
    try:
        try:
            rows = c.execute("SELECT id, username, remark, nick_name FROM contact")
        except sqlite3.OperationalError as e:
            log.warning("contact 表不可用（%s），发送者将显示数字 ID", e)
            return out
        for r in rows:
            out[r[0]] = {"username": r[1] or "", "remark": r[2] or "", "nick": r[3] or ""}
    finally:
        c.close()
    return out

def read_sessions(plain: Path) -> dict[str, str]:
    """username → 会话标识（用于 md5 反查）。表缺失时返回空。"""
    out: dict[str, str] = {}
    p = plain / "session" / "session.db"
    if not p.is_file():
        return out
    c = _ro(p)
    try:
        try:
            rows = c.execute("SELECT username FROM SessionTable")
        except sqlite3.OperationalError:
            return out
        for (u,) in rows:
            if u:
                out[u] = u
    finally:
        c.close()
    return out

def _decompress(v, zb) -> str:
    """message_content 可能是 TEXT 或 zstd 压缩的 BLOB（WCDB_CT_* = 4）。"""
    if v is None:
        return ""
    if isinstance(v, bytes):
        if zb is not None:
            try:
                return zb.decompress(v).decode("utf-8", "replace")
            except Exception:
                log.debug("zstd decompress failed, fallback to raw")
                pass
        return v.decode("utf-8", "replace")
    return str(v)

def _strip_sender(text: str) -> tuple[str, str]:
    """群聊 content 形如 `发送者: 正文`，拆出前缀。"""
    i = text.find(": ")
    if 0 < i <= 64 and "\n" not in text[:i]:
        return text[:i], text[i + 2:]
    return "", text

def _xml_field(text: str, tag: str) -> str:
    """从消息 XML 里取一个简单字段（不引入 XML 解析器的开销）。"""
    a = text.find("<%s>" % tag)
    if a < 0:
        return ""
    a += len(tag) + 2
    b = text.find("</%s>" % tag, a)
    return text[a:b] if b > a else ""

def _summarize(base: str, body: str) -> str:
    """非文字消息给出可读摘要，正文留在原 XML 里由媒体链路处理。"""
    if base == "image":
        return "[图片]"
    if base == "voice":
        return "[语音]"
    if base == "video":
        return "[视频]"
    if base == "emoji":
        return "[表情]"
    if base == "link":
        t = _xml_field(body, "title")
        return "[链接] " + t if t else "[链接]"
    if base == "file":
        t = _xml_field(body, "title")
        return "[文件] " + t if t else "[文件]"
    if base == "call":
        return "[通话]"
    if base == "revoke":
        return "[消息已撤回]"
    return body

def ensure_schema(conn: sqlite3.Connection) -> None:
    """确保 vault.db 有 conv/msg 表（与 wv_archiver 的 schema 一致）。

    sender_username 是后加的：老库只有 sender（显示名），而显示名会重名
    （实测 4933 个联系人里有 199 组同名），前端拿它查头像必然串。新库直接建出来，
    老库由 migrate_schema 补列、由回填逻辑补数据。
    """
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS conv (
        conv_id TEXT PRIMARY KEY, account TEXT DEFAULT '', title TEXT,
        is_group INTEGER DEFAULT 0, members TEXT DEFAULT '[]',
        n_msg INTEGER DEFAULT 0, start_ts INTEGER, end_ts INTEGER,
        sources TEXT DEFAULT '[]', created_at INTEGER, updated_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS msg (
        msg_id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, account TEXT DEFAULT '',
        seq INTEGER, ts INTEGER, time_str TEXT, sender TEXT, is_self INTEGER DEFAULT 0,
        type TEXT, content TEXT, media TEXT, media_full TEXT,
        source_file TEXT, created_at INTEGER, sender_username TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_msg_conv_ts ON msg(conv_id, ts);
    CREATE INDEX IF NOT EXISTS idx_msg_ts ON msg(ts);
    CREATE INDEX IF NOT EXISTS idx_msg_type ON msg(type);
    CREATE INDEX IF NOT EXISTS idx_msg_account ON msg(account);
    CREATE INDEX IF NOT EXISTS idx_conv_account ON conv(account);
    """)
    # 老库补列（CREATE TABLE IF NOT EXISTS 对已存在的表不生效）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(msg)")}
    if "sender_username" not in cols:
        conn.execute("ALTER TABLE msg ADD COLUMN sender_username TEXT DEFAULT ''")

def sync_messages(plain: Path, vault: Path, account_name: str = DEFAULT_ACCOUNT,
                  since_ts: int = 0, limit_tables: int = 0,
                  self_wxid: str = "") -> dict:
    """读 plain 库里的全部 Msg_* 表，写入 vault.db。返回统计。"""
    contacts = read_contacts(plain)
    sessions = read_sessions(plain)
    me = self_wxid or (account_name.split("_")[0] if "_" in account_name else account_name)
    self_ids = {i for i, v in contacts.items() if v["username"] == me}

    zb = zstd.ZstdDecompressor() if zstd else None
    conn = sqlite3.connect(str(vault))
    ensure_schema(conn)
    conn.execute("PRAGMA journal_mode=WAL")

    stats = {"conv": 0, "msg": 0, "skip_dup": 0, "tables": 0, "backfill": 0}
    msg_db = plain / "message"
    files = sorted(msg_db.glob("message_[0-9]*.db")) if msg_db.is_dir() else []
    if limit_tables:
        files = files[:limit_tables]

    now = int(time.time())
    for f in files:
        try:
            c = _ro(f)
        except sqlite3.Error as e:
            log.warning("打开失败 %s：%s", f.name, e)
            continue
        try:
            tabs = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg\\_%' ESCAPE '\\'")]
        except sqlite3.Error as e:
            log.warning("读取表清单失败 %s：%s", f.name, e)
            c.close()
            continue
        for t in tabs:
            # 表名 = "Msg_" + md5(username)，只能拿 session.db 的 username
            # 列表逐个算 md5 反查出会话归属
            h = t[4:]
            username = next((u for u in sessions if hashlib.md5(u.encode()).hexdigest() == h), "")
            if not username:
                continue
            ctitle = _display_title(username, contacts, sessions)
            cid = "%s::%s" % (account_name, ctitle)
            stats["tables"] += 1

            rows = c.execute(
                "SELECT local_id, local_type, create_time, real_sender_id, "
                "message_content, WCDB_CT_message_content "
                "FROM %s" % t)
            batch = []
            for local_id, ltype, ctime, sender_id, content, ct in rows:
                if not ctime or ctime < since_ts:
                    continue
                base_id = int(ltype) & 0xFFFFFFFF
                base = BASE_TYPE.get(base_id, "other")
                text = _decompress(content, zb)
                prefix, body = _strip_sender(text)
                is_self = 1 if sender_id in self_ids else 0
                who = contacts.get(sender_id, {})
                sender = who.get("remark") or who.get("nick") or who.get("username") or prefix or str(sender_id)
                # 显示名不唯一（实测 199 组重名），头像必须靠 username 才能精确对上
                sender_user = who.get("username") or ""
                content_out = _summarize(base, body)
                mid = MSG_PREFIX + hashlib.sha1(
                    ("%s|%s|%s" % (cid, local_id, ctime)).encode()).hexdigest()[:12]
                batch.append((mid, cid, account_name, local_id, ctime,
                              time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ctime)),
                              sender, is_self, base, content_out,
                              "/wxdb/%s" % ctitle, now, sender_user))
            if not batch:
                continue
            fresh = []
            backfill = []
            for b in batch:
                row = conn.execute(
                    "SELECT msg_id, sender_username FROM msg"
                    " WHERE conv_id=? AND ts=? AND content=? LIMIT 1",
                    (b[1], b[4], b[9])).fetchone()
                if row:
                    stats["skip_dup"] += 1
                    # 老库没有 sender_username，趁这一轮已经读到明文库把它补上；
                    # 只补空值，不覆盖已有数据
                    if b[12] and not (row[1] or ""):
                        backfill.append((b[12], row[0]))
                else:
                    fresh.append(b[:10] + (None, None, b[10], b[11], b[12]))
            if backfill:
                conn.executemany(
                    "UPDATE msg SET sender_username=? WHERE msg_id=?", backfill)
                stats["backfill"] = stats.get("backfill", 0) + len(backfill)
            if fresh:
                conn.executemany(
                    "INSERT OR IGNORE INTO msg (msg_id, conv_id, account, seq, ts, time_str,"
                    " sender, is_self, type, content, media, media_full, source_file, created_at,"
                    " sender_username) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", fresh)
                stats["msg"] += len(fresh)

            ts_list = [b[4] for b in batch]
            is_group = 1 if username.endswith("@chatroom") else 0
            conn.execute(
                "INSERT INTO conv (conv_id, account, title, is_group, n_msg, start_ts, end_ts,"
                " sources, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(conv_id) DO UPDATE SET"
                "   n_msg = n_msg + excluded.n_msg,"
                "   start_ts = MIN(COALESCE(conv.start_ts, excluded.start_ts), excluded.start_ts),"
                "   end_ts = MAX(COALESCE(conv.end_ts, excluded.end_ts), excluded.end_ts),"
                "   updated_at = excluded.updated_at",
                (cid, account_name, ctitle, is_group, len(fresh),
                 min(ts_list), max(ts_list), json.dumps([str(f)]), now, now))
            stats["conv"] = conn.execute("SELECT COUNT(*) FROM conv").fetchone()[0]
        c.close()

    conn.commit()
    conn.close()
    return stats

def _display_title(username: str, contacts: dict[int, dict], sessions: dict[str, str]) -> str:
    """会话显示名：群聊用群名，单聊用备注/昵称。"""
    for v in contacts.values():
        if v["username"] == username:
            return v["remark"] or v["nick"] or username
    return sessions.get(username, username)

def check_schema(plain: Path) -> tuple[bool, str]:
    """抽检一个消息库，确认表结构与当前解析逻辑匹配。

    微信大版本升级会改表名/列名，而原来的失败表现是「解密完成 0/24」
    或整表静默跳过——用户完全看不出是版本不支持。这里在入库前先探一次，
    把「你的客户端可能不兼容」直接说出来，而不是让人对着空结果猜。
    """
    msg_dir = plain / "message"
    dbs = sorted(msg_dir.glob("message_[0-9]*.db")) if msg_dir.is_dir() else []
    if not dbs:
        return False, "明文库下没有 message_*.db：要么解密失败，要么微信改了目录结构"
    conn = _ro(dbs[0])
    try:
        tabs = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg\\_%' ESCAPE '\\'")]
        if not tabs:
            return False, ("%s 里找不到 Msg_* 表：微信可能改了表命名（当前逻辑对应微信 4.x）"
                           % dbs[0].name)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % tabs[0])}
    finally:
        conn.close()
    missing = sorted({"local_id", "local_type", "create_time",
                      "real_sender_id", "message_content"} - cols)
    if missing:
        return False, ("%s.%s 缺少列 %s：解析逻辑按微信 4.x 的 Msg 表写，"
                       "你的客户端版本可能不兼容。提 issue 时请附上微信版本号"
                       % (dbs[0].name, tabs[0], ", ".join(missing)))
    return True, ""


def main() -> int:
    """CLI 入口：story = 解密 + 入库，decrypt = 只解密不入库。"""
    ap = argparse.ArgumentParser(description="从微信客户端数据库同步消息到 vault.db")
    ap.add_argument("cmd", nargs="?", default="story",
                    choices=["story", "decrypt"], help="story=解密+入库, decrypt=仅解密")
    ap.add_argument("--wechat-files", default=os.environ.get("WV_WECHAT_FILES", "/config/xwechat_files"))
    ap.add_argument("--keys", default=os.environ.get("WV_KEYS_JSON", "/data/wx_backup/all_keys.json"))
    ap.add_argument("--vault", default=os.environ.get("WV_STORE", "/data") + "/vault.db")
    ap.add_argument("--plain-out", default=os.environ.get("WV_PLAIN_DBS",
                                                          "/data/wx_backup/plain_dbs"))
    ap.add_argument("--wxid", default="", help="指定账号（默认自动发现）")
    ap.add_argument("--since", type=int, default=0, help="只同步该时间戳之后的消息")
    ap.add_argument("--limit-tables", type=int, default=0, help="只处理前 N 个 Msg 表（调试）")
    ap.add_argument("--sqlcipher", default=os.environ.get("WV_SQLCIPHER", "sqlcipher"))
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    wechat_files = Path(a.wechat_files)
    if not wechat_files.is_dir():
        log.error("微信数据目录不存在：%s", wechat_files)
        return 2
    account = find_account(wechat_files, a.wxid)
    log.info("账号目录：%s", account)

    keys_path = Path(a.keys)
    if not keys_path.is_file():
        log.error("密钥文件不存在：%s", keys_path)
        return 2
    keys = load_keys(keys_path)
    log.info("密钥：%d 个库", len(keys))
    passphrase_hex = load_passphrase(keys_path)
    if passphrase_hex:
        log.info("检测到 passphrase.json，将用账号级 passphrase 派生各库密钥")

    plain = Path(a.plain_out)
    n = decrypt_all(account, plain, keys, a.sqlcipher, passphrase_hex)
    if n == 0:
        log.error("没有任何库解密成功")
        return 1
    if a.cmd == "decrypt":
        return 0

    ok_schema, why = check_schema(plain)
    if not ok_schema:
        log.error("数据结构检查未通过 —— %s", why)
        return 3

    stats = sync_messages(plain, Path(a.vault), DEFAULT_ACCOUNT, a.since, a.limit_tables,
                          self_wxid=account.name.split("_")[0])
    log.info("同步完成：%d 个会话表 / 新增 %d 条消息 / 跳过重复 %d 条 / 补填发送者 %d 条",
             stats["tables"], stats["msg"], stats["skip_dup"], stats["backfill"])
    return 0

if __name__ == "__main__":
    sys.exit(main())
