#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wv-keymate 密钥管家 —— 微信 4.x 个人数据导出/归档的密钥流水线（Windows 侧）。

定位：把「用户以任意方式拿到账号密钥」之后的 10 分钟变成确定性体验——
校验 → 规范化 → 投递 NAS → 触发同步，一条龙跑完。

命令流（`run` 子命令一次走完，各步也都有独立子命令便于排查）：

  1. 导入   密钥来源两种：① 粘贴 64 hex 账号级 passphrase；② 拖入/指定 JSON
           文件（自动识别 passphrase.json / all_keys.json，兼容 96hex 拼接
           与 dict 两种旧格式）
  2. 校验   以**实证解密器**为唯一裁判：SQLCipher 4 页模型解密 → sqlite3
           试开，能开即密钥有效。逐库报告「解开了几个 / 哪些失败」
  3. 规范化 按共享契约写 passphrase.json（含元数据）+ all_keys.json
  4. 投递   robocopy 到 \\<NAS>\\wechat-vault\\wx_backup\\（NAS 共享映射容器
           /data，即 wv_sync --keys 默认目录），投递后提示 NAS 端 chmod 600
  5. 同步   POST http://<NAS>:8790/api/ingest（内网 HTTP，见 viewer/wv_server.py）

合规红线（本插件与全部配套脚本共同遵守）：
  * 只做非侵入、文档化的数据处理；不改动、不触碰第三方进程内部
  * 绝不开公网：投递只走 SMB、通知只走内网 HTTP，目标主机必须落在
    私网白名单（10/8、172.16/12、192.168/16、127/8）之内
  * 不携带任何第三方取钥工具的实现、下载指向或源码
  * 纯 Python 标准库；仅解密校验一步可选依赖 pycryptodome

日志纪律：**绝不打印密钥本体**，只打前 6 位 + 长度（见 mask_secret）。

密钥契约（与消费端 archiver/wv_sync.py 共享，不得偏离）：

  all_keys.json
    { "db_dir": "...",
      "keys": { "message/message_0.db": {"enc_key": "<64 hex>", "salt": "<32 hex>"}, ... } }
    enc_key = 已派生的 AES-256 密钥（直接用于 AES，不再走 KDF）
    salt    = 该库文件前 16 字节

  passphrase.json
    {"passphrase": "<64 hex = 32 字节账号级 password>", "wxid": "wxid_xxx",
     "captured_at": "ISO8601", "source": "scan|dump|manual", "wechat_version": "4.x.x"}
    消费端 load_passphrase() 只读 passphrase 且只校验 len==64，
    元数据字段天然被忽略、向后兼容。
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from Crypto.Cipher import AES as _AES
except ImportError:  # 可选依赖：只有解密裁判需要
    _AES = None

log = logging.getLogger("wv_keymate")

# ------------------------------------------------------------------ 常量
# SQLCipher 4 页模型（与 archiver/wv_sync.decrypt_one 同一套参数）：
#   页大小 4096，reserve 80（[4032:4096) 是页 HMAC，不参与解密）
#   页 0   [0:16) 盐 | [16:4016) 密文 | [4016:4032) IV | [4032:4096) HMAC
#   页 n>0 [0:4016) 密文 | [4016:4032) IV | [4032:4096) HMAC
PAGE_SZ = 4096
RESERVE = 80
IV_OFF = 4016
HMAC_OFF = 4032
SALT_SZ = 16
PBKDF2_ROUNDS = 256000
SQLITE_HEADER = b"SQLite format 3\x00"

PASSPHRASE_FILE = "passphrase.json"
ALL_KEYS_FILE = "all_keys.json"
DEFAULT_SHARE = "wechat-vault"
DEFAULT_REMOTE_DIR = "wx_backup"
DEFAULT_INGEST_PORT = 8790

_RE_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_RE_HEX96 = re.compile(r"^[0-9a-fA-F]{96}$")
_RE_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")

# 内存里密钥材料的两种形态：x'<96hex>' 包裹 与 裸 96hex
KEY_WRAPPED_PATTERN = re.compile(rb"x'([0-9a-fA-F]{96})'")
KEY_BARE_PATTERN = re.compile(rb"(?<![0-9a-fA-F])([0-9a-fA-F]{96})(?![0-9a-fA-F])")

# 私网白名单：投递/通知目标只允许这些网段（拒绝一切公网）
_ALLOWED_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
)


# ------------------------------------------------------------------ 日志
def mask_secret(secret: str) -> str:
    """密钥打码：只露前 6 位 + 长度。任何日志/报错都必须走它。"""
    s = secret or ""
    return "%s…(len=%d)" % (s[:6], len(s))


def _redact(text: str) -> str:
    """兜底：把混进日志的长十六进制串打码（防御式，正常路径不该触发）。"""
    return re.sub(r"[0-9a-fA-F]{32,}", lambda m: mask_secret(m.group(0)), text or "")


# ------------------------------------------------------------------ 网络
def is_private_host(host: str) -> bool:
    """目标主机是否落在私网白名单内。

    规则：字面 IP 必须属于 10/8、172.16/12、192.168/16、127/8；
    `localhost` 直接放行；其他主机名解析后的**全部**地址都必须在白名单内，
    解析失败或混入任一公网地址都拒绝。
    """
    h = (host or "").strip().strip("[]").lower()
    if not h or h == "localhost":
        return bool(h)

    def _in(ip_str: str) -> bool:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        return any(ip in net for net in _ALLOWED_NETS)

    try:
        if _in(h):
            return True
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(h, None)
    except OSError:
        return False
    addrs = {i[4][0] for i in infos}
    if not addrs:
        return False
    return all(_in(a) for a in addrs)


def ensure_private_host(host: str) -> str:
    """非私网目标直接抛错——绝不开公网。"""
    if not is_private_host(host):
        raise ValueError(
            "目标 %r 不在私网白名单内（只允许 10/8、172.16/12、192.168/16、127/8）。"
            "本工具绝不开公网" % (host,))
    return host


# ------------------------------------------------------------- 解密裁判
def derive_aes_key(passphrase_hex: str, salt: bytes) -> bytes:
    """账号级 passphrase → 该库的 AES-256 密钥（PBKDF2-HMAC-SHA512, 256000 轮）。"""
    pw = (passphrase_hex or "").strip().lower()
    if not _RE_HEX64.match(pw):
        raise ValueError("passphrase 必须是 64 hex，实际长度 %d" % len(pw))
    return hashlib.pbkdf2_hmac("sha512", bytes.fromhex(pw), salt, PBKDF2_ROUNDS, dklen=32)


def decrypt_sqlcipher4(blob: bytes, aes_key: bytes) -> bytes | None:
    """按 SQLCipher 4 页模型解密整库，返回明文字节（失败返回 None）。"""
    if _AES is None:
        return None
    if len(blob) < PAGE_SZ:
        return None
    out = bytearray()
    n_pages = len(blob) // PAGE_SZ
    for pi in range(n_pages):
        page = blob[pi * PAGE_SZ:(pi + 1) * PAGE_SZ]
        iv = page[IV_OFF:HMAC_OFF]
        try:
            if pi == 0:
                ct = page[SALT_SZ:IV_OFF]
                pt = _AES.new(aes_key, _AES.MODE_CBC, iv).decrypt(ct)
                # 明文页 0 还原 SQLite 头（加密状态下前 16 字节被盐占据）
                out += SQLITE_HEADER + pt + b"\x00" * RESERVE
            else:
                ct = page[:IV_OFF]
                pt = _AES.new(aes_key, _AES.MODE_CBC, iv).decrypt(ct)
                out += pt + b"\x00" * RESERVE
        except ValueError:
            return None
    return bytes(out)


def try_open_sqlite(plain: bytes) -> bool:
    """裁判：明文能不能被 sqlite3 当作正常库打开并读到 schema。

    注意必须真的执行一条读 schema 的查询——只 connect 不查询时 sqlite
    是惰性读头的，错密钥解出来的垃圾也可能"看起来连上了"。
    """
    if not plain or len(plain) < PAGE_SZ:
        return False
    fd, tmp = tempfile.mkstemp(suffix=".db")
    path = Path(tmp)
    try:
        os.write(fd, plain)
        os.close(fd)
        fd = -1
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return True
        except sqlite3.Error:
            return False
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return False
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            path.unlink()
        except OSError:
            pass


def judge_db(db_path: Path, passphrase_hex: str = "", enc_key_hex: str = "") -> bool:
    """解密裁判：给库配一把候选密钥，解开且能当 sqlite 打开才算有效。"""
    if _AES is None:
        raise RuntimeError("缺少 pycryptodome，无法做解密校验（pip install pycryptodome）")
    try:
        blob = Path(db_path).read_bytes()
    except OSError as e:
        log.warning("读取失败 %s：%s", Path(db_path).name, e)
        return False
    if len(blob) < PAGE_SZ:
        return False

    salt = blob[:SALT_SZ]
    if passphrase_hex:
        try:
            aes_key = derive_aes_key(passphrase_hex, salt)
        except ValueError:
            return False
    elif enc_key_hex:
        key_hex = enc_key_hex.strip().lower()
        if not _RE_HEX64.match(key_hex):
            return False
        aes_key = bytes.fromhex(key_hex)
    else:
        return False

    plain = decrypt_sqlcipher4(blob, aes_key)
    if plain is None:
        return False
    return try_open_sqlite(plain)


def read_salt(db_path: Path) -> bytes:
    """该库前 16 字节即 salt。"""
    with open(db_path, "rb") as f:
        return f.read(SALT_SZ)


def extract_candidates(data: bytes) -> set[str]:
    """从内存/快照字节流里捞 96 hex 密钥候选（统一小写）。"""
    out: set[str] = set()
    for m in KEY_WRAPPED_PATTERN.finditer(data):
        out.add(m.group(1).decode().lower())
    for m in KEY_BARE_PATTERN.finditer(data):
        out.add(m.group(1).decode().lower())
    return out


# --------------------------------------------------------------- 密钥解析
def split_legacy_96hex(hex96: str) -> dict:
    """旧格式 96hex 拼接 → {enc_key, salt}（前 64 是 AES key，后 32 是盐）。"""
    v = (hex96 or "").strip().lower()
    if not _RE_HEX96.match(v):
        raise ValueError("96hex 形态不合法（实际长度 %d）" % len(v))
    return {"enc_key": v[:64], "salt": v[64:96]}


def normalize_entry(val) -> dict | None:
    """把一条密钥记录规整成 {"enc_key": 64hex, "salt": 32hex}；无法识别返回 None。

    兼容三种旧写法：
      * 96hex 拼接字符串（老 all_keys.json 的 values）
      * 64hex 纯 enc_key 字符串（salt 留空，校验时改从库文件头取）
      * dict：{"enc_key": ..., "salt": ...}（新契约；enc_key 若是 96hex 也照切）
    """
    if isinstance(val, str):
        v = val.strip().lower()
        if _RE_HEX96.match(v):
            return {"enc_key": v[:64], "salt": v[64:96]}
        if _RE_HEX64.match(v):
            return {"enc_key": v, "salt": ""}
        return None
    if isinstance(val, dict):
        ek = str(val.get("enc_key") or "").strip().lower()
        salt = str(val.get("salt") or "").strip().lower()
        if _RE_HEX96.match(ek):
            return {"enc_key": ek[:64], "salt": ek[64:96]}
        if _RE_HEX64.match(ek):
            return {"enc_key": ek, "salt": salt if _RE_HEX32.match(salt) else ""}
        return None
    return None


def parse_key_payload(obj) -> dict:
    """把任意导入对象规整为统一结构。

    返回：
      {"passphrase": str, "keys": {rel: {"enc_key","salt"}},
       "universal": {"enc_key","salt"}|None, "meta": {...}}

    * passphrase.json 形态：顶层有 passphrase
    * all_keys.json 形态：顶层有 keys（value 为 96hex 或 dict）
    * 老式扁平形态：{rel: 96hex|dict}
    * 裸 64hex → passphrase；裸 96hex → universal enc_key（按库盐逐个试）
    """
    out = {"passphrase": "", "keys": {}, "universal": None, "meta": {}}

    if isinstance(obj, str):
        v = obj.strip().lower()
        if _RE_HEX64.match(v):
            out["passphrase"] = v
            return out
        if _RE_HEX96.match(v):
            out["universal"] = split_legacy_96hex(v)
            return out
        raise ValueError("无法识别的密钥输入（既不是 64/96 hex，也不是 JSON）")

    if not isinstance(obj, dict):
        raise ValueError("密钥输入必须是 JSON 对象或十六进制串")

    for meta_key in ("wxid", "captured_at", "source", "wechat_version", "db_dir"):
        if meta_key in obj and isinstance(obj[meta_key], str):
            out["meta"][meta_key] = obj[meta_key]

    raw_pass = obj.get("passphrase")
    if isinstance(raw_pass, str) and raw_pass.strip():
        pw = raw_pass.strip().lower()
        if not _RE_HEX64.match(pw):
            raise ValueError("passphrase 必须是 64 hex，实际长度 %d" % len(pw))
        out["passphrase"] = pw

    keys_src = obj.get("keys") if isinstance(obj.get("keys"), dict) else obj
    for rel, val in (keys_src or {}).items():
        if rel == "passphrase" or rel in ("db_dir", "wxid", "captured_at",
                                          "source", "wechat_version"):
            continue
        if not isinstance(val, (str, dict)):
            continue
        ent = normalize_entry(val)
        if ent:
            out["keys"][str(rel)] = ent

    return out


def load_input(source: str) -> dict:
    """导入入口：裸 hex / JSON 文本 / 文件路径，三种输入自动识别。"""
    s = (source or "").strip()
    if not s:
        raise ValueError("密钥输入为空")

    stripped = s.lower()
    if _RE_HEX64.match(stripped) or _RE_HEX96.match(stripped):
        return parse_key_payload(s)

    p = Path(s)
    if p.is_file():
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError("JSON 解析失败 %s：%s" % (p.name, e)) from e
        return parse_key_payload(obj)

    if s.lstrip().startswith(("{", "[")):
        try:
            return parse_key_payload(json.loads(s))
        except ValueError as e:
            raise ValueError("JSON 解析失败：%s" % e) from e

    raise ValueError("找不到输入文件：%s" % s)


# --------------------------------------------------------------- 契约读写
def guess_wxid(db_dir: Path) -> str:
    """从 db_storage 路径反推账号名（形如 wxid_xxx_2c37 → wxid_xxx）。"""
    name = Path(db_dir).resolve().parent.name
    m = re.match(r"^(wxid_[0-9A-Za-z]+)_[0-9a-fA-F]{3,10}$", name)
    if m:
        return m.group(1)
    return name if name.startswith("wxid_") else ""


def build_passphrase_doc(passphrase_hex: str, wxid: str = "", source: str = "manual",
                         wechat_version: str = "", captured_at: str = "") -> dict:
    """passphrase.json 文档（含元数据；消费端只读 passphrase，天然向后兼容）。"""
    return {
        "passphrase": (passphrase_hex or "").strip().lower(),
        "wxid": wxid or "",
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source if source in ("scan", "dump", "manual") else "manual",
        "wechat_version": wechat_version or "",
    }


def build_all_keys_doc(db_dir, keys: dict) -> dict:
    """all_keys.json 文档（契约：keys 的 value 一律是 {enc_key, salt} 字典）。"""
    norm = {}
    for rel, val in (keys or {}).items():
        ent = normalize_entry(val) if not (isinstance(val, dict) and "enc_key" in val) else {
            "enc_key": str(val["enc_key"]).strip().lower(),
            "salt": str(val.get("salt") or "").strip().lower(),
        }
        if ent and _RE_HEX64.match(ent["enc_key"]):
            norm[str(rel)] = {"enc_key": ent["enc_key"],
                              "salt": ent.get("salt", "")}
    return {"db_dir": str(db_dir or ""), "keys": norm}


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 下无 POSIX 权限位，靠投递后 NAS 端 chmod 兜底


def write_contract(out_dir, *, db_dir, keys: dict, passphrase: str = "",
                   wxid: str = "", source: str = "manual",
                   wechat_version: str = "", captured_at: str = "") -> dict:
    """按契约落盘 passphrase.json + all_keys.json，返回写好的路径。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = {}

    if passphrase:
        doc = build_passphrase_doc(passphrase, wxid=wxid, source=source,
                                   wechat_version=wechat_version,
                                   captured_at=captured_at)
        p = out / PASSPHRASE_FILE
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        _chmod_private(p)
        written[PASSPHRASE_FILE] = p

    doc2 = build_all_keys_doc(db_dir, keys)
    p2 = out / ALL_KEYS_FILE
    p2.write_text(json.dumps(doc2, ensure_ascii=False, indent=2), encoding="utf-8")
    _chmod_private(p2)
    written[ALL_KEYS_FILE] = p2
    return written


def load_contract(path) -> dict:
    """读回契约文件（单文件或所在目录都行）→ 统一结构。"""
    p = Path(path)
    if p.is_dir():
        bundle = {"passphrase": "", "keys": {}, "universal": None, "meta": {}}
        for name in (PASSPHRASE_FILE, ALL_KEYS_FILE):
            f = p / name
            if f.is_file():
                got = load_input(str(f))
                bundle["passphrase"] = bundle["passphrase"] or got["passphrase"]
                bundle["keys"].update(got["keys"])
                bundle["meta"].update(got["meta"])
        return bundle
    return load_input(str(p))


# ------------------------------------------------------------------ 探测
def probe_db_dir(db_dir, passphrase: str = "", keys: dict | None = None,
                 extras: list | None = None) -> dict:
    """对 db_storage 下的库逐个跑解密裁判。

    优先账号级 passphrase（库被微信重建后依然能解）；否则用逐库 enc_key；
    extras 是通用 enc_key 候选（裸 96hex 拆出来的），逐库试、命中即登记。

    返回 {"ok": [...], "failed": [...], "skipped": [...], "keys": {...}}，
    rel 一律用 POSIX 相对路径（与 wv_sync.load_keys 的键一致）。
    """
    root = Path(db_dir)
    if not root.is_dir():
        raise ValueError("db_storage 目录不存在：%s" % root)
    if _AES is None:
        raise RuntimeError("缺少 pycryptodome，无法做解密校验（pip install pycryptodome）")

    keys = {k: dict(v) for k, v in (keys or {}).items()}
    extras = list(extras or [])
    ok: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    dbs = sorted(p for p in root.rglob("*.db") if p.is_file())
    for db in dbs:
        rel = db.relative_to(root).as_posix()
        hit_mode = ""

        if passphrase and judge_db(db, passphrase_hex=passphrase):
            salt = read_salt(db).hex()
            keys[rel] = {"enc_key": derive_aes_key(passphrase, bytes.fromhex(salt)).hex(),
                         "salt": salt}
            hit_mode = "passphrase"
        elif rel in keys and judge_db(db, enc_key_hex=keys[rel].get("enc_key", "")):
            if not keys[rel].get("salt"):
                keys[rel]["salt"] = read_salt(db).hex()
            hit_mode = "enc_key"
        else:
            # 候选池：显式通用候选 + 已知 enc_key 兜底（微信同账号各库常共用
            # 同一 AES key，新建的库没有登记项时靠它接住）
            cands = list(extras)
            seen = {c.get("enc_key") for c in cands if isinstance(c, dict)}
            for ent in keys.values():
                ek = (ent or {}).get("enc_key", "")
                if ek and ek not in seen:
                    seen.add(ek)
                    cands.append({"enc_key": ek})
            tried = False
            for cand in cands:
                ek = (cand or {}).get("enc_key", "")
                if ek and judge_db(db, enc_key_hex=ek):
                    keys[rel] = {"enc_key": ek, "salt": read_salt(db).hex()}
                    hit_mode = "enc_key"
                    break
                tried = True
            if not hit_mode:
                # 有 passphrase/密钥材料但打不开 → 算失败；什么都没有 → 跳过
                if (rel in keys) or passphrase or tried or extras:
                    failed.append(rel)
                else:
                    skipped.append(rel)

        if hit_mode:
            ok.append(rel)
            log.info("校验通过 %-40s 方式=%s", rel, hit_mode)
        elif rel in failed:
            log.warning("校验失败 %s（密钥材料无法解开该库）", rel)

    log.info("校验完成：解开 %d / 失败 %d / 无材料跳过 %d（共 %d 个库）",
             len(ok), len(failed), len(skipped), len(dbs))
    return {"ok": ok, "failed": failed, "skipped": skipped, "keys": keys}


# ------------------------------------------------------------------ 投递
def deliver_keys(keys_dir, nas_host: str, share: str = DEFAULT_SHARE,
                 remote_dir: str = DEFAULT_REMOTE_DIR,
                 files: tuple = (PASSPHRASE_FILE, ALL_KEYS_FILE)) -> dict:
    """robocopy 密钥包到 NAS 共享（只走 SMB，目标必须是私网）。"""
    ensure_private_host(nas_host)
    src = Path(keys_dir)
    present = [f for f in files if (src / f).is_file()]
    if not present:
        raise ValueError("密钥包目录里没有可投递的文件：%s" % src)

    dest = "\\\\%s\\%s\\%s" % (nas_host, share, remote_dir)
    log.info("投递 %d 个文件 → %s", len(present), dest)

    if os.name == "nt":
        cmd = ["robocopy", str(src), dest, *present,
               "/R:1", "/W:2", "/NP", "/NDL", "/NJH", "/NJS", "/NC", "/NS"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        # robocopy：0-7 视为成功（1=有拷贝、2=有多余项……），≥8 才是失败
        if r.returncode >= 8:
            raise RuntimeError("robocopy 失败（退出码 %d）：%s"
                               % (r.returncode, _redact((r.stderr or r.stdout or "")[-400:])))
        rc = r.returncode
    else:
        # 非 Windows（测试/排障）：只做本地镜像，便于验证文件选择逻辑
        mirror = src.parent / (src.name + "_nas_staging")
        mirror.mkdir(parents=True, exist_ok=True)
        for f in present:
            shutil.copy2(src / f, mirror / f)
        rc = 0

    log.info("投递完成（robocopy 退出码 %s）", rc)
    log.info("请在 NAS 端执行：chmod 600 %s/%s %s/%s",
             remote_dir, PASSPHRASE_FILE, remote_dir, ALL_KEYS_FILE)
    return {"dest": dest, "files": present, "returncode": rc}


def trigger_ingest(nas_host: str, port: int = DEFAULT_INGEST_PORT,
                   timeout: int = 180) -> str:
    """POST http://<NAS>:8790/api/ingest 触发 NAS 端立即归档同步（内网 HTTP）。"""
    ensure_private_host(nas_host)
    url = "http://%s:%d/api/ingest" % (nas_host, port)
    log.info("触发同步：%s", url)
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 409 = 已有归档在跑，不算失败，但要说清楚
        body = e.read().decode("utf-8", "replace") if e.fp else ""
        return "HTTP %s %s" % (e.code, body)
    except urllib.error.URLError as e:
        raise RuntimeError("触发同步失败：%s" % _redact(str(e.reason))) from e


# ------------------------------------------------------------------ 流水线
def run_pipeline(input_source: str, db_dir, out_dir, nas_host: str = "",
                 wxid: str = "", source: str = "manual", wechat_version: str = "",
                 share: str = DEFAULT_SHARE, remote_dir: str = DEFAULT_REMOTE_DIR,
                 port: int = DEFAULT_INGEST_PORT,
                 skip_deliver: bool = False, skip_trigger: bool = False) -> dict:
    """导入 → 校验 → 规范化 → 投递 → 触发同步。返回全程报告。"""
    report: dict = {"steps": []}

    # 1) 导入
    bundle = load_input(input_source)
    report["import"] = {
        "passphrase": mask_secret(bundle["passphrase"]),
        "n_keys": len(bundle["keys"]),
        "universal": bool(bundle["universal"]),
    }
    report["steps"].append("import")
    log.info("导入：passphrase=%s，逐库密钥 %d 条，通用密钥候选=%s",
             mask_secret(bundle["passphrase"]), len(bundle["keys"]),
             "有" if bundle["universal"] else "无")

    # 2) 校验（解密裁判）
    extras = [bundle["universal"]] if bundle["universal"] else []
    probe = probe_db_dir(db_dir, passphrase=bundle["passphrase"],
                         keys=bundle["keys"], extras=extras)
    report["verify"] = {"ok": probe["ok"], "failed": probe["failed"],
                        "skipped": probe["skipped"]}
    report["steps"].append("verify")
    if not probe["ok"]:
        raise RuntimeError("没有任何库通过解密裁判，请确认密钥来源与 db_storage 目录")

    # 3) 规范化
    out = Path(out_dir)
    meta = bundle.get("meta") or {}
    written = write_contract(
        out,
        db_dir=db_dir,
        keys=probe["keys"],
        passphrase=bundle["passphrase"],
        wxid=wxid or meta.get("wxid") or guess_wxid(Path(db_dir)),
        source=source,
        wechat_version=wechat_version or meta.get("wechat_version", ""),
    )
    report["contract"] = {k: str(v) for k, v in written.items()}
    report["steps"].append("normalize")

    # 4) 投递
    if not skip_deliver and nas_host:
        report["deliver"] = deliver_keys(out, nas_host, share, remote_dir)
        report["steps"].append("deliver")

        # 5) 触发同步
        if not skip_trigger:
            report["ingest"] = trigger_ingest(nas_host, port)
            report["steps"].append("trigger")
    elif nas_host:
        report["deliver"] = {"skipped": True}
    else:
        log.info("未指定 NAS 主机，跳过投递与触发（可稍后用 deliver/trigger 子命令补）")

    return report


# --------------------------------------------------------------------- CLI
def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-v", "--verbose", action="store_true")


def _add_input_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("input", help="64hex passphrase / 96hex / JSON 文件路径 / JSON 文本")
    p.add_argument("--db-dir", default=os.environ.get("WV_DB_DIR", ""),
                   help="微信 db_storage 目录（或设 WV_DB_DIR）")
    p.add_argument("--out", default=os.environ.get("WV_KEYMATE_OUT", "keymate_out"),
                   help="契约输出目录（默认 keymate_out）")
    p.add_argument("--wxid", default="", help="账号 wxid（默认按目录名猜）")
    p.add_argument("--source", choices=["scan", "dump", "manual"], default="manual",
                   help="密钥来源标记，写入 passphrase.json")
    p.add_argument("--wechat-version", default="", help="微信版本号，写入元数据")


def _add_target_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--nas", default=os.environ.get("WV_NAS_HOST", ""),
                   help="NAS 主机名/IP（或设 WV_NAS_HOST），必须是私网")
    p.add_argument("--share", default=DEFAULT_SHARE, help="SMB 共享名")
    p.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR,
                   help="共享下的目标目录（对应容器 /data/wx_backup）")
    p.add_argument("--port", type=int, default=DEFAULT_INGEST_PORT, help="ingest 端口")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="keymate",
        description="微信 4.x 个人数据导出/归档的密钥管家：校验、规范化、投递一条龙")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="全流程：导入→校验→规范化→投递→触发同步")
    _add_input_args(p_run)
    _add_target_args(p_run)
    p_run.add_argument("--skip-deliver", action="store_true", help="只做前三步")
    p_run.add_argument("--skip-trigger", action="store_true", help="投递后不 POST ingest")
    _add_common(p_run)

    p_imp = sub.add_parser("import", help="导入+校验+规范化（不投递）")
    _add_input_args(p_imp)
    _add_common(p_imp)

    p_ver = sub.add_parser("verify", help="只跑解密裁判并报告")
    p_ver.add_argument("--keys", default="", help="契约文件或目录（passphrase/all_keys）")
    p_ver.add_argument("--input", default="", help="或直接给 hex / JSON")
    p_ver.add_argument("--db-dir", default=os.environ.get("WV_DB_DIR", ""))
    _add_common(p_ver)

    p_del = sub.add_parser("deliver", help="robocopy 密钥包到 NAS")
    p_del.add_argument("--keys-dir", default=os.environ.get("WV_KEYMATE_OUT", "keymate_out"))
    _add_target_args(p_del)
    _add_common(p_del)

    p_trg = sub.add_parser("trigger", help="POST /api/ingest 触发 NAS 同步")
    _add_target_args(p_trg)
    _add_common(p_trg)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))

    try:
        if args.cmd == "run":
            if not args.db_dir:
                log.error("必须指定 --db-dir（db_storage 目录）")
                return 2
            rep = run_pipeline(
                args.input, args.db_dir, args.out, nas_host=args.nas,
                wxid=args.wxid, source=args.source,
                wechat_version=args.wechat_version,
                share=args.share, remote_dir=args.remote_dir, port=args.port,
                skip_deliver=args.skip_deliver, skip_trigger=args.skip_trigger)
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "import":
            if not args.db_dir:
                log.error("必须指定 --db-dir（db_storage 目录）")
                return 2
            rep = run_pipeline(
                args.input, args.db_dir, args.out, nas_host="",
                wxid=args.wxid, source=args.source,
                wechat_version=args.wechat_version)
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "verify":
            if not args.db_dir:
                log.error("必须指定 --db-dir（db_storage 目录）")
                return 2
            if args.keys:
                bundle = load_contract(args.keys)
            elif args.input:
                bundle = load_input(args.input)
            else:
                log.error("必须给 --keys 或 --input 之一")
                return 2
            extras = [bundle["universal"]] if bundle["universal"] else []
            probe = probe_db_dir(args.db_dir, passphrase=bundle["passphrase"],
                                 keys=bundle["keys"], extras=extras)
            print(json.dumps({"ok": probe["ok"], "failed": probe["failed"],
                              "skipped": probe["skipped"]},
                             ensure_ascii=False, indent=2))
            return 0 if probe["ok"] else 1

        if args.cmd == "deliver":
            rep = deliver_keys(args.keys_dir, args.nas, args.share, args.remote_dir)
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "trigger":
            text = trigger_ingest(args.nas, args.port)
            print(text)
            return 0
    except (ValueError, RuntimeError, OSError) as e:
        log.error("%s", _redact(str(e)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
