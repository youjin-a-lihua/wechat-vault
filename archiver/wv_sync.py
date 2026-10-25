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
import re
import shutil
import sqlite3
import struct
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

def _norm_key_entry(v) -> tuple[str, str]:
    """把 all_keys.json 单条值规约成 (enc_key_hex, salt_hex)。

    契约（与其他工程师共享）：
      · enc_key = 64 hex = **已派生的** AES key，直接用于 AES，不再走 KDF
      · salt    = 32 hex = 该库前 16 字节
    向后兼容旧工具输出的 96hex 拼接字符串（enc_key ++ salt），自动切开。
    """
    if isinstance(v, dict):
        enc = str(v.get("enc_key") or "").strip().lower()
        salt = str(v.get("salt") or "").strip().lower()
    elif isinstance(v, str):
        enc, salt = v.strip().lower(), ""
    else:
        return "", ""
    if len(enc) == 96:
        # 旧格式：96 hex = enc_key(64) ++ salt(32) 拼接 → 自动切开
        if not salt:
            salt = enc[64:]
        enc = enc[:64]
    if len(enc) != 64:
        return "", ""
    return enc, salt


def load_keys(keys_path: Path) -> dict:
    """读取 all_keys.json，返回 {相对路径: (enc_key_hex, salt_hex)}。

    同时接受两种形态：
      · 契约字典：{"keys": {rel: {"enc_key": "<64hex>", "salt": "<32hex>"}}}
      · 旧工具输出：{"keys": {rel: "<96hex 拼接>"}} —— 自动切成 enc_key/salt
    文件缺失或损坏时返回空 dict（是否放行由 load_key_material 决定）。
    """
    try:
        raw = json.loads(keys_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("密钥文件读取/解析失败，按空处理：%s", e)
        return {}
    if not isinstance(raw, dict):
        log.warning("密钥文件顶层不是 JSON 对象，按空处理：%s", keys_path)
        return {}
    src = raw["keys"] if isinstance(raw.get("keys"), dict) else raw
    out: dict[str, tuple[str, str]] = {}
    for k, v in src.items():
        if not isinstance(k, str) or k == "db_dir":
            continue
        enc, salt = _norm_key_entry(v)
        if enc:
            out[k] = (enc, salt)
        elif v not in ("", None):
            log.warning("忽略无法识别的密钥条目：%s", k)
    return out

def load_key_material(keys_path: Path) -> tuple[dict, str, str]:
    """装载密钥材料：all_keys.json + 同目录 passphrase.json。

    G3 契约：passphrase.json 可用时，keys 文件缺失/为空都放行（keys 置空 dict），
    「放进去就用」；两者都没有才算没有密钥材料，此时 err 非空即失败原因。
    """
    keys: dict[str, tuple[str, str]] = {}
    if keys_path.is_file():
        keys = load_keys(keys_path)
    else:
        log.debug("密钥文件不存在，按空处理：%s", keys_path)
    passphrase_hex = load_passphrase(keys_path)
    if not keys and not passphrase_hex:
        return {}, "", ("既没有可用的 %s，同目录也没有可用的 passphrase.json —— "
                        "至少要提供一种密钥材料" % keys_path.name)
    return keys, passphrase_hex, ""

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

def _self_wxid(account: Path) -> str:
    """账号目录名 → 真实 wxid（判断「这条是不是我发的」要用它）。

    目录形如 `wxid_og6rczn7wbzm22_2c37`，尾部 `_2c37` 是设备后缀，
    真实 wxid 是 `wxid_og6rczn7wbzm22`。不能简单 split("_")[0] —— 那只会
    拿到字符串 "wxid"，导致 self_ids 恒为空集、**所有消息都被判成对方发的**，
    前端表现为左右角色全错（实测 55.9 万条消息中 is_self=1 的为 0）。
    """
    name = account.name
    m = re.match(r"^(wxid_[0-9A-Za-z]+)_[0-9a-fA-F]{3,10}$", name)
    if m:
        return m.group(1)
    if name.startswith("wxid_"):
        return name
    # 自定义微信号：去掉尾部设备后缀即可
    return re.sub(r"_[0-9a-fA-F]{3,10}$", "", name) or name


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


# ────────────────────────────────────────────────────────── 解密裁判 ──
# 历史教训：HMAC 公式猜谜（salt 是否 XOR 0x3A、轮数是 2 还是 256000）
# 错任何一处就是「扫描 0 命中」的假阴性，而且没人能从结果反推出公式哪版对。
# 这里以实证解密器 decrypt_one 的页模型为唯一裁判：把 page 0 解出来，
# 看明文是不是合法 SQLite 页头。公式再错也错不了——解得开就是真密钥。

def _aes_cbc_decrypt(aes_key: bytes, iv: bytes, ct: bytes) -> bytes:
    """AES-CBC 解密（无 padding），供解密裁判使用。

    优先 pycryptodome；缺失时退回 tools/wv_wxdb 的纯 Python AES-256，
    保证 wv_winkey/wv_keymem 这类零依赖抓 key 工具在裸机上也能跑裁判。
    """
    if _AES is not None:
        return _AES.new(aes_key, _AES.MODE_CBC, iv).decrypt(ct)
    tools_dir = str(Path(__file__).resolve().parent.parent / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from wv_wxdb import _aes_cbc_decrypt as pure_cbc
    return pure_cbc(aes_key, iv, ct)


def decrypt_page0_blob(page0: bytes, aes_key: bytes) -> bytes | None:
    """按 decrypt_one 的页模型解 page 0，返回重建后的明文页（失败返回 None）。"""
    if len(page0) < _PAGE_SZ or len(aes_key) != 32:
        return None
    iv = page0[_IV_OFF:_HMAC_OFF]
    ct = page0[_SALT_SZ:_IV_OFF]
    try:
        pt = _aes_cbc_decrypt(aes_key, iv, ct)
    except Exception as e:  # 纯实现/后端异常都不该把工具带崩
        log.debug("page0 解密失败：%s", e)
        return None
    return b"SQLite format 3\x00" + pt + b"\x00" * _RESERVE


def looks_like_sqlite_page0(page: bytes) -> bool:
    """验证重建页是合法 SQLite page 0（对照 SQLite 头固定字段）。"""
    if len(page) < 100 or not page.startswith(b"SQLite format 3\x00"):
        return False
    (psz,) = struct.unpack(">H", page[16:18])
    if psz == 1:
        psz = 65536
    if psz < 512 or psz > 65536 or psz & (psz - 1):
        return False
    if page[18] not in (1, 2) or page[19] not in (1, 2):
        return False
    # SQLite 格式规定的固定载荷分数（64/32/32），随机明文几乎不可能凑齐
    if page[21] != 64 or page[22] != 32 or page[23] != 32:
        return False
    if struct.unpack(">I", page[44:48])[0] not in (1, 2, 3, 4):
        return False
    return struct.unpack(">I", page[56:60])[0] in (1, 2, 3)


def verify_page0_key(page0: bytes, enc_key: bytes) -> bool:
    """解密裁判核心：page0 密文 + AES key → 明文是否合法 SQLite 页。"""
    page = decrypt_page0_blob(page0, enc_key)
    return page is not None and looks_like_sqlite_page0(page)


def verify_key_by_decrypt(db_path: Path, enc_key_hex: str, full: bool = False) -> bool:
    """解密裁判入口：验证 enc_key 是否为该库密钥（唯一裁判，不看 HMAC）。

    full=False：只解 page 0 校验页头（微秒级，用于内存候选批量筛选）；
    full=True ：解全文件后 sqlite3.connect() 试开（金标准，用于最终确认）。
    """
    key_hex = enc_key_hex.strip().lower()
    if len(key_hex) != 64:
        return False
    try:
        enc_key = bytes.fromhex(key_hex)
    except ValueError:
        return False
    try:
        with open(db_path, "rb") as f:
            page0 = f.read(_PAGE_SZ)
    except OSError as e:
        log.warning("裁判读库失败 %s：%s", db_path, e)
        return False
    if len(page0) < _PAGE_SZ:
        return False
    if not verify_page0_key(page0, enc_key):
        return False
    if not full:
        return True
    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "judge.db"
        if not decrypt_one(Path(db_path), dst, key_hex):
            return False
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % dst, uri=True)
            try:
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
                return True
            finally:
                conn.close()
        except sqlite3.Error as e:
            log.debug("解密裁判全量试开失败 %s：%s", db_path, e)
            return False

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
            if not passphrase_hex:
                log.debug("无密钥，跳过：%s", rel)
                continue
            # G2：有账号级 passphrase 时，未登记的库也要解。
            # 微信日后新增的 message_23.db 这类库不在 all_keys.json 里，
            # 静默跳过会让它们永远解不开；decrypt_one 的 passphrase 分支
            # 会用「文件自带 salt + 账号 passphrase」自行派生密钥。
            ent = ("", "")
        total += 1
        if decrypt_one(src, plain_out / rel, ent[0], ent[1], passphrase_hex=passphrase_hex):
            ok += 1
        elif rel.startswith("session"):
            # session.db 决定「哪个 Msg 表属于哪个会话」。它解不开时，
            # 后面的同步会 tables=0、但 exit code 仍是 0、指纹照常推进 ——
            # 结果是**静默永久停止同步**，没有任何日志说明原因。
            # 这里单独喊一声，让人知道是密钥问题而不是"没有新消息"。
            log.error("session.db 解密失败（%s）：会话归属将无法确定，本轮不会同步任何消息。"
                      "若此前能同步，说明账号级密钥已失效，请重新定标密钥", rel)
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

def _strip_sender(text: str, known: set[str] | None = None) -> tuple[str, str]:
    """群聊正文形如 `发送者: 内容`，拆出前缀。

    微信的写法**不统一**：有时是「冒号+空格」，有时是「冒号+换行」
    （实测 `wxid_xxx:\\n正文`），所以不能只找 `": "`。

    但也不能见冒号就拆——正文里「时间: 12:30」会被误伤。做法是传
    该库 Name2Id 里的 username 集合做白名单，前缀命中才认；没有白名单时
    退回到「像账号名」的保守正则（纯字母数字/下划线/@/点/横线）。
    """
    head, sep, rest = text.partition(":")
    if not sep or not head or len(head) > 64 or "\n" in head:
        return "", text
    if known is not None:
        if head not in known:
            return "", text
    elif not re.match(r"^[0-9A-Za-z_@.\-]+$", head):
        return "", text
    return head, rest.lstrip("\n").lstrip()

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

    **顺序很重要**：`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，所以老库
    跑完建表语句后依然没有新列；此时若接着跑 `CREATE INDEX ... ON conv(account,
    username)` 就会 `no such column: username`，整个同步直接崩。必须先补列、再建
    索引——`wv_archiver.migrate_schema` 里为同一件事写过这条注释，这里是第二次踩。
    """
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS conv (
        conv_id TEXT PRIMARY KEY, account TEXT DEFAULT '', title TEXT,
        -- username = wxid / xxx@chatroom，会话的**稳定身份**（conv_id 的键）。
        -- title 是显示名，随时会变，绝不能拿来当键。
        username TEXT DEFAULT '',
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
    """)
    # 老库补列（CREATE TABLE IF NOT EXISTS 对已存在的表不生效）—— 必须在建索引之前
    cols = {r[1] for r in conn.execute("PRAGMA table_info(msg)")}
    if "sender_username" not in cols:
        conn.execute("ALTER TABLE msg ADD COLUMN sender_username TEXT DEFAULT ''")
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(conv)")}
    if "username" not in ccols:
        # 老库的 conv_id 是 account::显示名，username 列还没建；
        # 建出来后由 migrate_conv_keys 逐条认领（填了 username 才算迁完）
        conn.execute("ALTER TABLE conv ADD COLUMN username TEXT DEFAULT ''")

    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_msg_conv_ts ON msg(conv_id, ts);
    -- 覆盖 /api/messages 的完整排序键：只有 (conv_id, ts) 时，
    -- ts 之后的 seq/msg_id 两个排序键要走 TEMP B-TREE，
    -- 等于把整个会话（实测最大 18 万条）取出来再排序取前 200
    -- （SQLite 的 executescript 只认 -- 注释，写 # 会直接 unrecognized token）
    CREATE INDEX IF NOT EXISTS idx_msg_conv_ts_full ON msg(conv_id, ts, seq, msg_id);
    CREATE INDEX IF NOT EXISTS idx_msg_ts ON msg(ts);
    CREATE INDEX IF NOT EXISTS idx_msg_type ON msg(type);
    CREATE INDEX IF NOT EXISTS idx_msg_account ON msg(account);
    CREATE INDEX IF NOT EXISTS idx_conv_account ON conv(account);
    -- conv.username：会话的**稳定身份**（wxid / xxx@chatroom）。
    -- conv_id 的键就是它，显示名只是 conv.title 里的一个可变字段。
    CREATE INDEX IF NOT EXISTS idx_conv_username ON conv(account, username);
    """)
    # 全文索引：同步链路自己建的库（没走过 wv_archiver 的导入路径）以前**没有**
    # msg_fts，于是 /api/search 永远走 LIKE 全表扫描 —— 189 万行上敲一个词
    # 就是一次全库扫。建出来，由 ensure_fts 负责补齐内容。
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5("
        "content, sender, conv_id UNINDEXED, msg_id UNINDEXED, "
        "account UNINDEXED, tokenize='trigram')")
    # 老库补列（CREATE TABLE IF NOT EXISTS 对已存在的表不生效）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(msg)")}
    if "sender_username" not in cols:
        conn.execute("ALTER TABLE msg ADD COLUMN sender_username TEXT DEFAULT ''")
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(conv)")}
    if "username" not in ccols:
        # 老库的 conv_id 是 account::显示名，username 列还没建；
        # 建出来后由 migrate_conv_keys 逐条认领（填了 username 才算迁完）
        conn.execute("ALTER TABLE conv ADD COLUMN username TEXT DEFAULT ''")

def read_name2id(msg_db: Path) -> dict[int, str]:
    """message 库自己的 id → username。

    `real_sender_id` 指向本库 `Name2Id` 的 rowid，**不是 contact.db 的 id**。
    这两套 id 空间完全不同（实测自己的 contact.id=85，而在各 message 库的
    Name2Id 里 rowid=3）。以前拿 contact.db 的 id 去解，等于查错了表：
    sender 靠消息体前缀兜底才勉强看着对，而 is_self 几乎全错——
    55.9 万条同步消息里被判定为「我发的」只有 145 条。
    """
    out: dict[int, str] = {}
    try:
        conn = _ro(msg_db)
        try:
            for rowid, name in conn.execute("SELECT rowid, user_name FROM Name2Id"):
                if name:
                    out[rowid] = name
        finally:
            conn.close()
    except sqlite3.Error as e:
        log.warning("读取 %s 的 Name2Id 失败（%s）：该库发送者回退到前缀解析",
                    msg_db.name, e)
    return out


# 参与全文索引的消息类型（与 wv_archiver 的口径一致）
FTS_TYPES = ("text", "link", "system", "other")

def rebuild_fts(conn: sqlite3.Connection) -> int:
    """整体重建全文索引，返回写入行数。

    只在"msg_id 被批量改写过"（会话键迁移）之后调用：那种情况下旧索引里的
    msg_id 全部失效，逐行改不如整表重建。索引从 msg 全量回填，不丢内容。
    """
    n = conn.execute(
        "SELECT COUNT(*) FROM msg WHERE type IN (%s)"
        " AND COALESCE(content,'')<>''" % ",".join("?" * len(FTS_TYPES)),
        FTS_TYPES).fetchone()[0]
    conn.execute("DROP TABLE IF EXISTS msg_fts")
    conn.execute(
        "CREATE VIRTUAL TABLE msg_fts USING fts5("
        "content, sender, conv_id UNINDEXED, msg_id UNINDEXED, "
        "account UNINDEXED, tokenize='trigram')")
    conn.execute(
        "INSERT INTO msg_fts (content, sender, conv_id, msg_id, account) "
        "SELECT content, sender, conv_id, msg_id, COALESCE(account,'') FROM msg "
        "WHERE type IN (%s) AND COALESCE(content,'')<>''"
        % ",".join("?" * len(FTS_TYPES)), FTS_TYPES)
    log.info("全文索引已整体重建：%d 条", n)
    return n

def ensure_fts(conn: sqlite3.Connection) -> int:
    """补齐缺失的全文索引行，返回补了几条。

    为什么需要它：FTS 不是"建了就有"——只有走过写入路径的消息才会进索引。
    实测这套库里 msg_fts 有 93.7 万行、而可索引消息有 117 万行，差 23 万：
    那些消息搜不到，而前端不会提示"索引不全"，只会表现为"我记得有这条，
    搜不出来"。

    做法是先建一张临时表存已有 msg_id（带主键索引），再用 NOT IN 补缺的。
    直接用 `WHERE msg_id NOT IN (SELECT msg_id FROM msg_fts)` 不行 ——
    FTS5 表的列是 UNINDEXED，那个子查询会退化成逐行扫全表，O(n²)。

    幂等：补完再跑就是 0 条。
    """
    try:
        need = conn.execute(
            "SELECT COUNT(*) FROM msg WHERE type IN (%s)"
            " AND COALESCE(content,'')<>''" % ",".join("?" * len(FTS_TYPES)),
            FTS_TYPES).fetchone()[0]
        have = conn.execute("SELECT COUNT(*) FROM msg_fts").fetchone()[0]
    except sqlite3.Error as e:
        log.warning("全文索引检查失败，本轮跳过：%s", e)
        return 0
    if have >= need:
        return 0

    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _fts_have"
                 "(msg_id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM _fts_have")
    conn.execute("INSERT INTO _fts_have SELECT msg_id FROM msg_fts")
    cur = conn.execute(
        "INSERT INTO msg_fts (content, sender, conv_id, msg_id, account) "
        "SELECT m.content, m.sender, m.conv_id, m.msg_id, COALESCE(m.account,'') "
        "FROM msg m WHERE m.type IN (%s) AND COALESCE(m.content,'')<>'' "
        "AND m.msg_id NOT IN (SELECT msg_id FROM _fts_have)"
        % ",".join("?" * len(FTS_TYPES)), FTS_TYPES)
    conn.execute("DROP TABLE IF EXISTS _fts_have")
    n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else (need - have)
    log.info("全文索引补齐：%d 条（索引 %d → 目标 %d）", n, have, need)
    return n

def _stable_cid(account_name: str, username: str) -> str:
    """会话 ID：**account::username**，不用显示名。

    原来这里是 `account::显示名`（备注/昵称/群名）。显示名是会变的——
    对方改个备注、群里改个名，下一次同步就会算出一个"新"的 conv_id，
    于是：会话在列表里分裂成两条，而 msg_id 又是拿 conv_id 算的，
    整段历史被当成新消息重新插一遍（重复 + 条数虚高）。
    username（wxid / xxx@chatroom）在微信侧是不变的，拿它做键才对。
    显示名照旧存 conv.title，改了就改 title，不碰键。
    """
    return "%s::%s" % (account_name, username)

def _recompute_mid(cid: str, local_id, ctime) -> str:
    """按 msg_id 的既定公式重算（迁移改名时用）。"""
    return MSG_PREFIX + hashlib.sha1(
        ("%s|%s|%s" % (cid, local_id, ctime)).encode()).hexdigest()[:12]

def migrate_conv_keys(conn: sqlite3.Connection, account_name: str,
                      title_map: dict[str, set[str]]) -> dict:
    """把老库里 `account::显示名` 的会话键迁到 `account::username`。

    只在**同步时能拿到 ground truth** 的地方做：title_map 是本次同步逐个
    username 算出来的「显示名 → username 集合」，比反过来猜可靠得多。

    迁移要动三样东西，少一样就会留下暗坑：
      1. conv.conv_id      —— 键本身
      2. msg.conv_id       —— 消息跟着走
      3. msg.msg_id        —— 它是拿 conv_id 算的，不改的话下一轮所有消息
                              都"认不出自己"，全部走兜底去重（每轮多 137 万次查询）

    只重写 `db-` 前缀的 msg_id（同步链路自己算的）。`json-` 那条是导入链路
    按文件算的，反推不出原始字段，保持不动——它们的 conv_id 照样跟着迁，
    位置不会错。

    返回统计；跳过（显示名重名且无法判定）的会话原样留着，不猜。
    """
    stats = {"conv": 0, "msg": 0, "skip": 0, "renamed": 0, "stuck": {},
             "legacy": 0}
    legacy = conn.execute(
        "SELECT conv_id, title FROM conv WHERE account=?"
        " AND COALESCE(username,'')=''", (account_name,)).fetchall()
    if not legacy:
        return stats

    for conv_id, title in legacy:
        # 键里那半截才是建库时的显示名（title 可能后来被改过）
        key = conv_id.split("::", 1)[-1] if "::" in conv_id else conv_id
        cands = set(title_map.get(key, ())) | set(title_map.get(title or "", ()))
        username = ""
        if len(cands) == 1:
            username = next(iter(cands))
        elif len(cands) > 1:
            # 显示名撞车（实测 4933 个联系人里 199 组同名）：拿这个会话里
            # 出现过的 sender_username 去认人。群聊的 sender 是成员而非群号，
            # 认不出来就宁可不迁——留着老键只是"改了名会分裂"，猜错就是真错。
            who = {r[0] for r in conn.execute(
                "SELECT DISTINCT sender_username FROM msg WHERE conv_id=?"
                " AND COALESCE(sender_username,'')<>''", (conv_id,))}
            hit = [u for u in cands if u in who]
            if len(hit) == 1:
                username = hit[0]
        if not username:
            stats["skip"] += 1
            if len(cands) > 1:
                log.warning("显示名 %r 对应多个会话且无法判定，跳过迁移：%s",
                            key, conv_id)
                # 记下来交给同步主循环：这类会话继续沿用老键，
                # 不去新建 account::username —— 否则同名两人的历史会各自
                # 再插一遍（msg_id 跟着 cid 变），凭空多出上万条重复。
                stats["stuck"][key] = conv_id
                stats["stuck"].setdefault(title or "", conv_id)
            else:
                # 零候选是另一种情况：这个显示名在**当前微信号里根本不存在**。
                # 实测 12 个全是 JSON 导入遗留的历史会话（微信那边早就没了，
                # 或本来就不是这个号的），其中 10 个零候选、2 个歧义。
                # 这类会话的 msg_id 清一色是 json- 前缀，本轮同步不会碰它们，
                # 所以"保持老键"没有分裂风险——它不是失败，是无法归属。
                stats["legacy"] += 1
            continue

        new_cid = _stable_cid(account_name, username)
        if new_cid == conv_id:
            conn.execute("UPDATE conv SET username=? WHERE conv_id=?",
                         (username, conv_id))
            stats["conv"] += 1
            continue

        rows = conn.execute(
            "SELECT msg_id, seq, ts FROM msg WHERE conv_id=?", (conv_id,)).fetchall()
        rebind = []
        for mid, seq, ts in rows:
            new_mid = (_recompute_mid(new_cid, seq, ts)
                       if (mid or "").startswith(MSG_PREFIX) else mid)
            rebind.append((new_cid, new_mid, mid))
        # OR IGNORE：新 msg_id 撞上已存在的行（同一条消息之前已经被写进
        # 新键了）时跳过，那些重复的由下面的 DELETE 收掉
        conn.executemany(
            "UPDATE OR IGNORE msg SET conv_id=?, msg_id=? WHERE msg_id=?", rebind)
        stats["msg"] += len(rebind)
        # 全文索引**不在这里跟随**：FTS5 的列是 UNINDEXED，逐行改 189 万条
        # 等于每行全表扫一次。改完由调用方整体重建一次（见 rebuild_fts）。
        conn.execute("DELETE FROM msg WHERE conv_id=?", (conv_id,))
        stats["renamed"] += 1

        exists = conn.execute("SELECT 1 FROM conv WHERE conv_id=?", (new_cid,)).fetchone()
        if exists:
            # 新老键同时存在（比如别的路径已经按新键建过）：合并，删掉老行
            conn.execute("DELETE FROM conv WHERE conv_id=?", (conv_id,))
        else:
            conn.execute("UPDATE conv SET conv_id=?, username=? WHERE conv_id=?",
                         (new_cid, username, conv_id))
        # 条数以实际消息数为准，别沿用累加出来的旧值
        conn.execute(
            "UPDATE conv SET n_msg=(SELECT COUNT(*) FROM msg WHERE conv_id=?)"
            " WHERE conv_id=?", (new_cid, new_cid))
        stats["conv"] += 1

    if stats["conv"]:
        log.info("会话键迁移（显示名 → username）：会话 %d 个、消息 %d 条、跳过 %d 个",
                 stats["conv"], stats["msg"], stats["skip"])
    return stats

def sync_messages(plain: Path, vault: Path, account_name: str = DEFAULT_ACCOUNT,
                  since_ts: int = 0, limit_tables: int = 0,
                  self_wxid: str = "") -> dict:
    """读 plain 库里的全部 Msg_* 表，写入 vault.db。返回统计。"""
    contacts = read_contacts(plain)
    sessions = read_sessions(plain)
    me = self_wxid or (account_name.split("_")[0] if "_" in account_name else account_name)
    # username → contact 行（备注/昵称）。注意：必须按 username 查，
    # 不能按 contact.db 的自增 id 查 —— 那是另一个 id 空间。
    by_user = {v["username"]: v for v in contacts.values() if v.get("username")}

    zb = zstd.ZstdDecompressor() if zstd else None
    conn = sqlite3.connect(str(vault))
    ensure_schema(conn)
    conn.execute("PRAGMA journal_mode=WAL")

    stats = {"conv": 0, "msg": 0, "skip_dup": 0, "tables": 0, "backfill": 0}
    # 先把「显示名 → username」这张表建出来（迁移要用，且只有这里能算准），
    # 再在真正写数据之前把老键迁过去。
    # 这里必须 O(n)：_display_title 每调一次都要遍历全部联系人，
    # 逐个 session 调就是 会话数 × 联系人数（几千 × 几千）次比较。
    title_map: dict[str, set[str]] = {}
    for u in sessions:
        v = by_user.get(u)
        t = ((v.get("remark") or v.get("nick")) if v else "") or sessions.get(u, u)
        title_map.setdefault(t, set()).add(u)
    stats["migrate"] = migrate_conv_keys(conn, account_name, title_map)
    # 迁移改写了 msg_id → 旧索引整批失效，必须重建（否则每条搜索结果
    # 都指向已经不存在的 msg_id）。这是**一次性**的：以后没有迁移就不跑。
    if stats["migrate"].get("renamed"):
        try:
            stats["fts_rebuilt"] = rebuild_fts(conn)
        except sqlite3.Error as e:
            log.warning("全文索引重建失败（不影响消息入库，搜索暂时回落 LIKE）：%s", e)

    msg_db = plain / "message"
    files = sorted(msg_db.glob("message_[0-9]*.db")) if msg_db.is_dir() else []
    if limit_tables:
        files = files[:limit_tables]

    # 会话表名是 md5(username)，必须拿 session.db 的 username 列表反查归属。
    # 这里为空时后面会 tables=0 —— 若不当回事，autosync 会当成"没有新消息"
    # 照常推进指纹，从此静默停止同步。所以必须显式报错。
    if not sessions:
        log.error("session.db 读不到会话列表（解密失败或表为空）：无法确定消息归属，"
                  "本轮不会同步任何消息。通常是账号级密钥失效，请重新定标")
    elif not files:
        log.error("明文库下没有 message_*.db：要么解密失败，要么微信改了目录结构")

    now = int(time.time())
    for f in files:
        # c 先置 None：_ro() 抛异常时 finally 里才知道不用关
        c = None
        try:
            c = _ro(f)
        except sqlite3.Error as e:
            log.warning("打开失败 %s：%s", f.name, e)
            continue
        # 每个 message 库都有自己的 Name2Id，必须逐库读
        n2i = read_name2id(f)
        known_users = set(n2i.values())
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
            # 同名撞车且判定不了的会话：继续用老键（保持合并现状），
            # 不去新建 account::username —— 见 migrate_conv_keys 里的说明
            stuck = stats["migrate"].get("stuck", {})
            cid = stuck.get(ctitle) or _stable_cid(account_name, username)
            conv_username = "" if ctitle in stuck else username
            stats["tables"] += 1

            # 表名要拼进 SQL：按已知形态校验 + 双引号包裹，双保险
            if not re.fullmatch(r"Msg_[0-9a-f]{32}", t):
                log.warning("跳过形态异常的消息表名：%r", t)
                continue
            rows = c.execute(
                "SELECT local_id, local_type, create_time, real_sender_id, "
                "message_content, WCDB_CT_message_content "
                'FROM "%s"' % t)
            batch = []
            for local_id, ltype, ctime, sender_id, content, ct in rows:
                # 单行解析失败只跳过这一条。原来这里没有任何保护：
                # 一条脏数据（ltype 非 int、content 非 bytes/str、ctime 超出
                # strftime 范围）就会抛穿整个循环 → 本轮全部 136 万条白跑。
                try:
                    if not ctime or ctime < since_ts:
                        continue
                    base_id = int(ltype) & 0xFFFFFFFF
                    base = BASE_TYPE.get(base_id, "other")
                    text = _decompress(content, zb)
                    prefix, body = _strip_sender(text, known_users)
                    # sender_id 是本库 Name2Id 的 rowid，先翻译回真实 username
                    sender_user = n2i.get(sender_id, "")
                    is_self = 1 if sender_user == me else 0
                    who = by_user.get(sender_user, {})
                    sender = (who.get("remark") or who.get("nick")
                              or sender_user or prefix or str(sender_id))
                    content_out = _summarize(base, body)
                    mid = MSG_PREFIX + hashlib.sha1(
                        ("%s|%s|%s" % (cid, local_id, ctime)).encode()).hexdigest()[:12]
                except Exception as e:
                    stats["bad_rows"] = stats.get("bad_rows", 0) + 1
                    # 只详细报前几条：真出事时往往是成片的，全打会刷屏
                    if stats["bad_rows"] <= 5:
                        log.warning("跳过无法解析的消息 %s/%s#%s：%s: %s",
                                    f.name, t, local_id, type(e).__name__, e)
                    continue
                batch.append((mid, cid, account_name, local_id, ctime,
                              time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ctime)),
                              sender, is_self, base, content_out,
                              "/wxdb/%s" % ctitle, now, sender_user))
            if not batch:
                continue
            fresh = []
            update = []
            for b in batch:
                # 优先按 msg_id 认亲：mid 由 cid+local_id+ctime 算出，稳定不变
                row = conn.execute(
                    "SELECT msg_id, sender, sender_username, is_self"
                    " FROM msg WHERE msg_id=?", (b[0],)).fetchone()
                if not row:
                    # 兜底：只服务「另一条导入路径」（JSON 导入的 mid 前缀是
                    # json-，与这里的 db- 不同，所以 msg_id 必然不命中）。
                    #
                    # 为什么**不能**加 seq：JSON 记录里的 seq 是文件内序号，
                    # 与微信的 local_id 是两套编号，永远对不上。加了 seq 的
                    # 后果是这 81 万条跨源重复全部失配、被当成新消息插一遍
                    # （实测过一次：一轮多出 805,071 条）。
                    #
                    # 为什么**不能**用 content：它会被重算（剥离发送者前缀），
                    # 拿新值比对旧记录必然落空，同样会把同一条消息插两份
                    # （更早实测过一次，多出 390,288 条）。
                    #
                    # 至于"同一秒连发两条会被吞"——那需要先走到兜底才会发生，
                    # 而 wxdb 自己的消息 msg_id 必然命中上面那条主路径，
                    # 根本到不了这里。所以这个键在实际同步场景是安全的。
                    row = conn.execute(
                        "SELECT msg_id, sender, sender_username, is_self"
                        " FROM msg WHERE conv_id=? AND ts=? AND sender=? AND is_self=?"
                        " LIMIT 1",
                        (b[1], b[4], b[6], b[7])).fetchone()
                if row:
                    stats["skip_dup"] += 1
                    # 老数据的 sender / is_self 用错了 id 空间，按正确映射重算；
                    # content 不动——历史正文里的发送者前缀属于显示问题，
                    # 为此重写 content 会破坏上面那个兜底键，得不偿失
                    if ((row[1] or "") != b[6] or (row[2] or "") != b[12]
                            or (row[3] or 0) != b[7]):
                        update.append((b[6], b[12], b[7], row[0]))
                else:
                    fresh.append(b[:10] + (None, None, b[10], b[11], b[12]))
            if update:
                conn.executemany(
                    "UPDATE msg SET sender=?, sender_username=?, is_self=?"
                    " WHERE msg_id=?", update)
                stats["backfill"] = stats.get("backfill", 0) + len(update)
            if fresh:
                conn.executemany(
                    "INSERT OR IGNORE INTO msg (msg_id, conv_id, account, seq, ts, time_str,"
                    " sender, is_self, type, content, media, media_full, source_file, created_at,"
                    " sender_username) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", fresh)
                stats["msg"] += len(fresh)

            ts_list = [b[4] for b in batch]
            is_group = 1 if username.endswith("@chatroom") else 0
            conn.execute(
                "INSERT INTO conv (conv_id, account, title, username, is_group, n_msg,"
                " start_ts, end_ts, sources, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(conv_id) DO UPDATE SET"
                "   n_msg = n_msg + excluded.n_msg,"
                "   title = excluded.title,"          # 改备注/改群名 → 只改显示名
                "   username = excluded.username,"    # 老库认领：补上稳定身份
                "   start_ts = MIN(COALESCE(conv.start_ts, excluded.start_ts), excluded.start_ts),"
                "   end_ts = MAX(COALESCE(conv.end_ts, excluded.end_ts), excluded.end_ts),"
                "   updated_at = excluded.updated_at",
                (cid, account_name, ctitle, conv_username, is_group, len(fresh),
                 min(ts_list), max(ts_list), json.dumps([str(f)]), now, now))
            stats["conv"] = conn.execute("SELECT COUNT(*) FROM conv").fetchone()[0]
        c.close()

    # 补齐历史遗留的索引缺口（新消息/老消息都在这里入索引，一处搞定）。
    # 放在迁移之后：迁移会重写 msg_id，索引必须跟着改完再补，否则补的是旧 id。
    try:
        stats["fts"] = ensure_fts(conn)
    except sqlite3.Error as e:
        log.warning("全文索引补齐失败（不影响消息入库）：%s", e)

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
    # G3：passphrase.json 可用时，keys 文件缺失/为空照常启动（无感续用）；
    # 两种密钥材料都没有才报错退出。
    keys, passphrase_hex, key_err = load_key_material(keys_path)
    if key_err:
        log.error("%s", key_err)
        return 2
    log.info("密钥：%d 个库", len(keys))
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
                          self_wxid=_self_wxid(account))
    log.info("同步完成：%d 个会话表 / 新增 %d 条消息 / 跳过重复 %d 条 / 修正发送者与己方标记 %d 条",
             stats["tables"], stats["msg"], stats["skip_dup"], stats["backfill"])
    # 一个会话表都没匹配上 = 同步实际失败，必须返回非 0。
    # 否则 autosync 只会看到 exit 0、照常推进指纹，从此静默停止同步，
    # 日志里连一句"出问题了"都没有。
    if stats["tables"] == 0:
        return 4
    return 0

if __name__ == "__main__":
    sys.exit(main())
