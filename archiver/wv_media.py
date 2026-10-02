#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信数据方舟 · 媒体流水线（图片解密 + 消息关联 + 头像抽取）
==========================================================

背景与结论（2026-10-02 实测定稿，与公开资料交叉验证一致）
-------------------------------------------------------
微信 4.x 的 .dat 图片是 V2 格式：

    [0:6)   签名 07 08 56 32 08 07
    [6:10)  aes_size  (u32 LE，本机实测恒为 1024)
    [10:14) xor_size  (u32 LE，= 尾部 XOR 段长度)
    [14]    占位
    [15:15+aes_size)                  AES-128-ECB 密文 → 明文「头部」
    [15+aes_size:15+aes_size+16)      16 字节固定块（本机恒为 4b342788…afad，跳过）
    [15+aes_size+16: +xor_size)       尾部，单字节 XOR

    明文 = AES_ECB_dec(头部密文, aes_key) + (尾部 XOR xor_key)

两把密钥都是**账号级**、可离线派生，不需要扫进程内存：

    xor_key = code & 0xFF
    aes_key = md5(f"{code}{wxid}").hexdigest()[:16].encode()   # 16 字节 ASCII

    · xor_key 可先用缩略图（*_t.dat）尾部反推：JPEG 必以 FF D9 结尾 ⇒
      xor_key = tail[-2] ^ 0xFF（本机得 0x42）
    · code 满足 code & 0xFF == xor_key，用「解密后必须是合法 JPEG 头」
      做严格校验暴力搜索即可（本机得 1*********），一次求出永久可用

消息 ↔ 图片文件的关联依据（实测 99.1% 命中）
------------------------------------------
    attach/<md5(chat_username)>/<YYYY-MM>/Img/*.dat
    · 会话用户名来自 vault.msg.source_file 的 basename
    · 同一 (chat, 时间戳) 的消息在明文库里记录了三档明文尺寸
      （length / hdlength / cdnthumblength），与解密后明文**字节数精确相等**
    → 用 (md5(username), 月份, 明文长度) 三元组定位文件

用法
----
    python wv_media.py keys    --attach <attach目录>          # 定标并缓存密钥
    python wv_media.py decode  --attach <dir> --out <媒体目录> # 批量解密
    python wv_media.py link    --vault <vault.db> --plain-dbs <dir> --out <dir>
    python wv_media.py avatars --plain-dbs <dir> --out <dir>
    python wv_media.py all     --attach <dir> --vault <vault.db> --plain-dbs <dir> --out <dir>
"""

from __future__ import annotations

import shutil

import argparse
import collections
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
import time
import wave
from multiprocessing import Pool

from Crypto.Cipher import AES
import zstandard as zstd
import logging

log = logging.getLogger(__name__)

V2_MAGIC = b"\x07\x08V2\x08\x07"
FIXED_BLOCK = bytes.fromhex("4b34278883a017cadb25201c644dafad")
IMAGE_MAGIC = {
    b"\xff\xd8\xff": "jpg", b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif", b"GIF89a": "gif",
    b"RIFF": "webp", b"BM": "bmp", b"wxgf": "wxgf",
}
# JPEG 段标记：SOI 之后必须紧跟合法标记，用于严格排除 AES 随机输出撞上前缀
JPEG_STRICT = {b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"\xff\xd8\xff\xdb",
               b"\xff\xd8\xff\xee", b"\xff\xd8\xff\xed", b"\xff\xd8\xff\xfe"}

# ---------------------------------------------------------------------------
# 密钥
# ---------------------------------------------------------------------------

def derive_xor_key(thumbs: list[str], sample: int = 120) -> int | None:
    """由缩略图尾部反推账号级单字节 XOR 密钥（JPEG 必以 FF D9 结尾）。"""
    votes = collections.Counter()
    for p in thumbs[:sample]:
        try:
            with open(p, "rb") as f:
                f.seek(-2, os.SEEK_END)
                t = f.read(2)
        except OSError as e:
            log.debug("缩略图不可读，跳过该投票：%s", e)
            continue
        if len(t) == 2:
            votes[t[0] ^ 0xFF] += 1
    if not votes:
        return None
    k, n = votes.most_common(1)[0]
    return k if n >= max(3, len(thumbs[:sample]) // 4) else None

def aes_key_of(code: int, wxid: str) -> bytes:
    return hashlib.md5(("%d%s" % (code, wxid)).encode()).hexdigest()[:16].encode()

def _strict_ok(block: bytes, key: bytes) -> bool:
    try:
        pt = AES.new(key, AES.MODE_ECB).decrypt(block)
    except Exception:
        return False
    return pt[:4] in JPEG_STRICT

_SCAN: dict = {}

def _scan_init(blocks: list[bytes], wxid: str):
    _SCAN["blocks"], _SCAN["wxid"] = blocks, wxid

def _scan_worker(rng: tuple[int, int]) -> int | None:
    """在 [lo, hi) 内按 256 步长扫描 code（模块级，供 multiprocessing pickle）。"""
    lo, hi = rng
    blocks, wxid = _SCAN["blocks"], _SCAN["wxid"]
    for code in range(lo, hi, 256):
        key = aes_key_of(code, wxid)
        # 粗筛：首个密文块必须解出合法 JPEG 段标记
        if not _strict_ok(blocks[0], key):
            continue
        # 复核：另外两块也要通过，才排除 AES 随机输出撞前缀的假阳性
        if all(_strict_ok(b, key) for b in blocks[1:3]):
            return code
    return None

def find_code(blocks: list[bytes], wxid: str, xor_key: int,
              workers: int = 4) -> int | None:
    """暴力求 code：约束 code & 0xFF == xor_key，用严格 JPEG 头自校验。

    实测（本机）：1677 万候选中唯一真解；假阳性被「三块同时通过」压到可忽略。
    """
    # ── F6 修复：分段必须对齐 256 步长 ──
    # _scan_worker 用 range(lo, hi, 256)，要求每段起点满足 (lo & 0xFF) == xor_key。
    # 若直接 span = 2**32 // workers，当 workers 非 2 的幂时 span % 256 != 0，
    # 第 2、3 段起点会错位 → 漏扫大量候选 → 可能永远找不到正确 code。
    # 正确做法：把全范围 [0, 2**32) 先按 256 对齐切成 n=workers 段，再各自定位到
    # 段内第一个满足 (c & 0xFF)==xor_key 的起点（+256 之内必有）。
    n_units = 2 ** 32 // 256            # 共 2**24 个 256 步长单元
    per = (n_units + workers - 1) // workers   # 每进程单元数（向上取整）
    jobs = []
    for i in range(workers):
        u0 = i * per
        if u0 >= n_units:
            break
        u1 = min(u0 + per, n_units)
        # 单元 u 对应 code 起点 u*256；在此段内找第一个 &0xFF==xor_key 的起点
        lo = u0 * 256
        while lo < u1 * 256 and (lo & 0xFF) != xor_key:
            lo += 1
        hi = u1 * 256
        jobs.append((lo, hi))
    # ── F4 修复：扫描进度（1677 万候选，无反馈会像卡死）──
    print("  暴力定标：%d 进程 × %d 段，约 %d 万候选…"
          % (workers, len(jobs), 2 ** 24 / 10000), flush=True)
    with Pool(workers, _scan_init, (blocks, wxid)) as pool:
        for r in pool.imap_unordered(_scan_worker, jobs):
            if r is not None:
                return r
    return None

def wxid_candidates(name: str) -> list[str]:
    """账号目录名 → 参与密钥派生的 wxid 候选（去重、按可能性排序）。

    ── 实测（2026-10-02）：目录 `wxid_示例账号_2c37`（`_2c37` 为设备后缀）
    对应的正确取值是 **`wxid_示例账号`**（保留 wxid_ 下划线，仅去设备后缀）。
    不同机型/版本的后缀形式不一，故这里给出多个候选，由「能否解出合法 JPEG」
    自动判定，不写死。
    """
    cands: list[str] = []
    m = re.match(r"^(wxid_[0-9A-Za-z]+)_[0-9a-fA-F]{3,10}$", name)
    if m:
        cands.append(m.group(1))                       # wxid_xxx  （本机命中）
        cands.append("".join(name.split("_")[:2]))      # wxidxxx
    cands.append(name)
    cands.append(name.split("_", 1)[0] if "_" in name else name)
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out

def calibrate_keys(attach: str, wxid_names: list[str] | str, cache: str) -> dict:
    """求出并缓存 xor_key / code / aes_key。keys 由 hd 块自校验自动挑定。"""
    if os.path.exists(cache):
        try:
            d = json.load(open(cache))
            if d.get("aes_key") and d.get("code"):
                return d
        except Exception as e:
            log.warning("密钥缓存损坏，改为重新定标：%s", e)
            pass

    thumbs = sorted(glob.glob(attach + "/*/*/Img/*_t.dat"))
    xk = derive_xor_key(thumbs)
    if xk is None:
        raise SystemExit("无法从缩略图反推 xor_key（attach 目录为空？）")
    print("  xor_key = 0x%02x（由 %d 个缩略图尾部位反推）" % (xk, len(thumbs)))

    blocks = []
    for p in thumbs:
        d = open(p, "rb").read()
        if d[:6] == V2_MAGIC and len(d) > 1200:
            blocks.append(d[15:31])
        if len(blocks) >= 3:
            break
    if not blocks:
        raise SystemExit("找不到 V2 缩略图，无法定标 aes_key")

    names = ([wxid_names] if isinstance(wxid_names, str) else wxid_names)
    for nm in names:
        t0 = time.time()
        code = find_code(blocks, nm, xk)
        if code is None:
            print("  候选 wxid=%-28s 未命中（%.1fs）" % (nm, time.time() - t0))
            continue
        out = {"code": code, "xor_key": xk, "wxid": nm,
               "aes_key": aes_key_of(code, nm).decode(),
               "calibrated_at": int(time.time())}
        print("  ✓ wxid=%s  code=%d  aes_key=%s  (%.1fs)"
              % (nm, code, out["aes_key"], time.time() - t0))
        os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
        json.dump(out, open(cache, "w"), ensure_ascii=False, indent=2)
        return out
    raise SystemExit("所有 wxid 候选均未找到匹配的 code（微信可能已变更算法）")

# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------

def decode_dat(blob: bytes, aes_key: bytes, xor_key: int) -> tuple[bytes, str] | None:
    """解一个 V2 .dat；返回 (明文, 扩展名) 或 None。"""
    if len(blob) < 1200 or blob[:6] != V2_MAGIC:
        return None
    aes_size, xor_size = struct.unpack("<II", blob[6:14])
    if aes_size % 16 or 15 + aes_size + 16 + xor_size > len(blob):
        return None
    # 固定块自检：这 16 字节对**所有账号/所有文件**恒定。若解出的不是它，
    # 说明 aes_key 或布局解析错了——宁可返回 None 也别把垃圾当图片。
    if blob[15 + aes_size:15 + aes_size + 16] != FIXED_BLOCK:
        return None
    head = AES.new(aes_key, AES.MODE_ECB).decrypt(blob[15:15 + aes_size])
    tbl = bytes.maketrans(bytes(range(256)), bytes(b ^ xor_key for b in range(256)))
    tail = blob[15 + aes_size + 16:15 + aes_size + 16 + xor_size].translate(tbl)
    plain = head + tail
    for magic, ext in IMAGE_MAGIC.items():
        if plain.startswith(magic):
            return plain, ext
    return None

_W = {}

def _init_worker(aes_key: bytes, xor_key: int, out: str):
    _W["aes"], _W["xor"], _W["out"] = aes_key, xor_key, out

def _worker(path: str):
    try:
        blob = open(path, "rb").read()
    except OSError:
        return None
    r = decode_dat(blob, _W["aes"], _W["xor"])
    if not r:
        return (path, None, None, 0)
    plain, ext = r
    md5 = hashlib.md5(plain).hexdigest()
    rel = ""
    if ext and ext != "wxgf":
        sub = md5[:2]
        rel = "%s/%s.%s" % (sub, md5, ext)
        dst = os.path.join(_W["out"], rel)
        if not os.path.exists(dst):
            # ⚠️ 多进程会并发命中同一 md5（同一张图在多个会话出现）：
            #    必须用「带 pid 的唯一临时名」再原子改名，否则第二个进程
            #    会去 rename 已被第一个进程搬走的 tmp，报 FileNotFoundError。
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                tmp = "%s.%d.tmp" % (dst, os.getpid())
                with open(tmp, "wb") as f:
                    f.write(plain)
                os.replace(tmp, dst)
            except OSError:
                try:
                    os.unlink(tmp)
                except (OSError, UnboundLocalError):  # 可安全忽略：临时文件清理失败，主流程不受影响
                    pass
    return (path, len(plain), rel, md5)

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

def run_decode(attach: str, out: str, aes_key: bytes, xor_key: int,
               workers: int = 4) -> dict:
    """批量解密全部 .dat，返回 (哈希, 月份, 明文长度) → 相对路径 的索引。"""
    files = glob.glob(attach + "/*/*/Img/*.dat")
    os.makedirs(out, exist_ok=True)
    print("  待解密 .dat：%d" % len(files))
    stats = collections.Counter()
    sizemap: dict[tuple, str] = {}
    md5map: dict[str, str] = {}
    t0 = time.time()
    with Pool(workers, _init_worker, (aes_key, xor_key, out)) as pool:
        for i, res in enumerate(pool.imap_unordered(_worker, files, chunksize=200), 1):
            if not res:
                stats["读取失败"] += 1
                continue
            path, size, rel, md5 = res
            if size is None:
                stats["解码失败"] += 1
                continue
            stats["解码成功"] += 1
            if not rel:
                stats["wxgf需二层解压"] += 1
                continue
            parts = os.path.relpath(path, attach).split(os.sep)
            if len(parts) >= 2:
                sizemap[(parts[0], parts[1], size)] = rel
            md5map[md5] = rel
            stats["已落盘"] += 1
            if i % 20000 == 0:
                print("    ... %d/%d" % (i, len(files)), flush=True)
    print("  完成 %.1fs：%s" % (time.time() - t0, dict(stats)))
    idx = {"sizemap": {"%s|%s|%d" % k: v for k, v in sizemap.items()},
           "md5map": md5map}
    json.dump(idx, open(os.path.join(out, "_index.json"), "w"))
    print("  索引：%d 条尺寸索引 / %d 条 md5 索引" % (len(sizemap), len(md5map)))
    return idx

# ---------------------------------------------------------------------------
# 消息关联
# ---------------------------------------------------------------------------

def _plain_sizes(plain_dbs: str) -> dict[tuple, list[int]]:
    """从解密明文库取每个图片消息的候选明文尺寸：{(chat_hash, 时间戳): [尺寸…]}"""
    out: dict[tuple, list[int]] = {}
    zbuf = zstd.ZstdDecompressor()

    def dec(b):
        try:
            return zbuf.decompress(b)
        except Exception:
            return b""

    for db in sorted(glob.glob(plain_dbs + "/message/message_*.db")):
        try:
            c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
            tabs = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'Msg\\_%' ESCAPE '\\'")]
            for t in tabs:
                h = t[4:]
                try:
                    rows = c.execute(
                        "SELECT create_time, message_content FROM %s "
                        "WHERE local_type=3" % t).fetchall()
                except sqlite3.Error as e:
                    log.warning("读取明文库失败，该库尺寸信息整体缺失（会明显拉低关联命中率）：%s", e)
                    continue
                for ct, mc in rows:
                    m = re.search(rb"<img ([^>]*)>", dec(mc))
                    if not m:
                        continue
                    a = dict(re.findall(rb'([a-zA-Z0-9_]+)="([^"]*)"', m.group(1)))
                    sizes = []
                    for k in (b"length", b"hdlength", b"cdnthumblength"):
                        v = a.get(k)
                        if v and v.isdigit() and int(v) > 0:
                            sizes.append(int(v))
                    if sizes:
                        out[(h, ct)] = sizes
            c.close()
        except sqlite3.Error as e:
            log.warning("消息表查询失败，该会话尺寸信息缺失，会拉低后续关联命中率：%s", e)
            continue
    return out

def _username_of(source_file: str | None) -> str:
    if not source_file:
        return ""
    return os.path.basename(str(source_file)).rsplit(".", 1)[0]

def _ct_hash_map(plain_dbs: str) -> dict:
    """时间戳 → {该时刻真实 username 的 md5 集合}。

    用途（run_link 的二次校正）：vault 里 `source_file` 是显示名（备注/昵称），
    但磁盘目录名是 md5(真实 username)。若显示名恰好等于某个真实 username，
    直接 md5(显示名) 就能命中目录——这个集合就是用来判断「该显示名是否也是
    合法 username」的。

    数据源：contact 表的 username 全集（含 chat_room 群号）的 md5。
    """
    import hashlib as _h
    db = os.path.join(plain_dbs, "contact", "contact.db")
    if not os.path.exists(db):
        return {}
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        names = set()
        for (u,) in c.execute("SELECT username FROM contact"):
            if u:
                names.add(u)
        try:
            for (u,) in c.execute("SELECT username FROM chat_room"):
                if u:
                    names.add(u)
        except sqlite3.Error as e:
            # chat_room 表在部分旧版微信里不存在；缺失只影响「群号直连」这一条校正路径
            log.debug("chat_room 表不可用（旧版微信无此表？），群号直连校正跳过：%s", e)
        c.close()
    except sqlite3.Error as e:
        log.warning("联系人表读取失败，二次校正不可用：%s", e)
        return {}
    return {_h.md5(u.encode()).hexdigest() for u in names}


def run_link(vault: str, plain_dbs: str, idx: dict) -> dict:
    """给 vault.db 的图片消息回填 media 相对路径。"""
    sizemap = idx["sizemap"]
    print("  解析明文库尺寸信息…", flush=True)
    psizes = _plain_sizes(plain_dbs)
    print("  明文库图片消息：%d 条" % len(psizes))

    def month_of(ts: int) -> str:
        return dt.datetime.fromtimestamp(
            ts, dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m")

    conn = sqlite3.connect(vault)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT msg_id, source_file, ts FROM msg WHERE type='image'").fetchall()
    print("  vault 图片消息：%d 条" % len(rows))

    chash = _conv_hash_map(conn, rows, plain_dbs)
    # 「时间戳 → 该时刻真实 username 的 md5 集合」，供下方做二次校正：
    # 显示名可能命中备注/昵称，但磁盘目录用的是 md5(真实 username)；
    # 若某条消息的显示名本身就是一个合法 username 的 md5，则直接命中目录。
    c2h = _ct_hash_map(plain_dbs)
    print("  会话↔哈希 映射：%d 个会话" % len(chash))

    hit = 0
    upd = []
    for r in rows:
        k = _username_of(r["source_file"])
        h = chash.get(k)
        if not h or not r["ts"]:
            continue
        # 优先用真实 username 的 md5 校正（若恰好也在候选集合里）
        h_pool = {h}
        uk = hashlib.md5(k.encode()).hexdigest()
        if uk in c2h:
            h_pool.add(uk)
        mo = month_of(r["ts"])
        rel = None
        for hh in h_pool:
            for size in psizes.get((hh, r["ts"]), []):
                rel = sizemap.get("%s|%s|%d" % (hh, mo, size))
                if rel:
                    break
            if rel:
                break
        if rel:
            hit += 1
            upd.append(("/media/" + rel, r["msg_id"]))
    print("  命中并回填：%d / %d (%.1f%%)"
          % (hit, len(rows), 100 * hit / max(len(rows), 1)))
    conn.executemany("UPDATE msg SET media=? WHERE msg_id=?", upd)
    conn.commit()
    conn.close()
    return {"images": len(rows), "linked": hit, "convs_matched": len(chash)}

# ---------------------------------------------------------------------------
# 头像
# ---------------------------------------------------------------------------

def run_avatars(plain_dbs: str, out: str) -> dict:
    """把 head_image.db 里的头像落到 <out>/avatars/<md5(username)>.<ext>，
    并生成 显示名 → 文件名 的索引（前端按发送者显示名取头像）。"""
    src = plain_dbs + "/head_image/head_image.db"
    if not os.path.exists(src):
        print("  未找到 head_image.db，跳过")
        return {"avatars": 0}
    c = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
    try:
        rows = c.execute(
            "SELECT username, image_buffer FROM head_image "
            "WHERE image_buffer IS NOT NULL").fetchall()
    finally:
        c.close()

    # username → 显示名（备注 > 昵称 > 微信号）
    names: dict[str, str] = {}
    cd = plain_dbs + "/contact/contact.db"
    if os.path.exists(cd):
        cc = sqlite3.connect("file:%s?mode=ro" % cd, uri=True)
        try:
            for u, rm, nk, al in cc.execute(
                    "SELECT username, remark, nick_name, alias FROM contact"):
                names[u] = (rm or nk or al or u)
        finally:
            cc.close()

    adir = os.path.join(out, "avatars")
    os.makedirs(adir, exist_ok=True)
    index: dict[str, str] = {}
    n = 0
    for u, blob in rows:
        if not blob or len(blob) < 100:
            continue
        ext = "jpg"
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png"
        elif blob[:3] == b"GIF":
            ext = "gif"
        fn = "%s.%s" % (hashlib.md5(u.encode()).hexdigest(), ext)
        with open(os.path.join(adir, fn), "wb") as f:
            f.write(blob)
        for key in {u, names.get(u, "")}:
            if key:
                index[key] = fn
        n += 1
    json.dump(index, open(os.path.join(out, "avatar_index.json"), "w"),
              ensure_ascii=False)
    print("  头像：%d 张，索引 %d 条" % (n, len(index)))
    return {"avatars": n, "index": len(index)}

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    _setup_logging()
    ap = argparse.ArgumentParser(description="微信数据方舟 · 媒体流水线")
    ap.add_argument("cmd", choices=["keys", "decode", "link", "avatars", "all", "videos", "files", "voice", "emoji", "wxam", "avatars-fetch", "extras"])
    ap.add_argument("--attach", required=True, help="attach 目录")
    ap.add_argument("--wxid", default="", help="账号 wxid（不含设备后缀）")
    ap.add_argument("--vault", help="vault.db 路径")
    ap.add_argument("--plain-dbs", dest="plain_dbs", help="解密明文库目录")
    ap.add_argument("--out", required=True, help="媒体输出目录")
    ap.add_argument("--account-root", dest="account_root", default="",
                    help="账号数据根（…/<wxid>_<设备后缀>，含 msg/）")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    cache = os.path.join(a.out, "_keys.json")
    os.makedirs(a.out, exist_ok=True)
    info = {"out": a.out, "attach": a.attach}

    if a.cmd in ("keys", "decode", "all"):
        # 账号目录 = attach 的上两级（…/<wxid>_<设备后缀>/msg/attach）
        acct = os.path.basename(os.path.dirname(os.path.dirname(a.attach.rstrip("/"))))
        cands = [a.wxid] if a.wxid else wxid_candidates(acct)
        print("定标密钥（账号目录=%s，候选=%s）…" % (acct, cands))
        k = calibrate_keys(a.attach, cands, cache)
        info["keys"] = k

    if a.cmd in ("decode", "all"):
        k = json.load(open(cache))
        idx = run_decode(a.attach, a.out, k["aes_key"].encode(), int(k["xor_key"]),
                         a.workers)
        info["decode"] = {"sizemap": len(idx["sizemap"]), "md5map": len(idx["md5map"])}

    if a.cmd in ("link", "all"):
        idx = json.load(open(os.path.join(a.out, "_index.json")))
        info["link"] = run_link(a.vault, a.plain_dbs, idx)

    if a.cmd in ("avatars", "all"):
        info["avatars"] = run_avatars(a.plain_dbs, a.out)

    # ── 扩展：视频 / 文件 / 语音 / 头像联网（需 --account-root 指向账号数据根）──
    root = a.account_root
    if a.cmd in ("videos", "extras") and root:
        info["videos"] = run_videos(root, a.vault, a.out, a.plain_dbs)
    if a.cmd in ("files", "extras") and root:
        info["files"] = run_files(root, a.vault, a.out)
    if a.cmd in ("voice", "extras") and a.plain_dbs:
        info["voice"] = run_voice(a.plain_dbs, a.vault, a.out)
    if a.cmd in ("wxam", "extras") and root:
        info["wxam"] = run_wxam(root, a.vault, a.out)
    if a.cmd in ("emoji", "extras") and a.plain_dbs:
        info["emoji"] = run_emoji(a.plain_dbs, a.vault, a.out)
    if a.cmd in ("avatars-fetch", "extras") and a.plain_dbs:
        info["avatars_fetch"] = run_avatars_fetch(a.plain_dbs, a.vault, a.out)

    print("\n完成：", json.dumps(info, ensure_ascii=False, indent=2))

# ===========================================================================
# 以下为「视频 / 文件 / 语音 / 头像联网」扩展（2026-10-02）
# ===========================================================================
#  各类型的关联判据（均为实测结论，详见 SKILL 与验收报告）：
#    · 视频：msg/video/<YYYY-MM>/<hash>.mp4 + <hash>_thumb.jpg（均明文）
#            判据 = (月份, XML 的 cdnthumblength) == 缩略图 jpg 的字节数
#            实测 98.3% 命中；其中 45.9% 本机也留有可播放的 mp4
#    · 文件：msg/file/<YYYY-MM>/<原始文件名>
#            判据 = vault.content 的「[文件] xxx」与磁盘文件名相等
#            覆盖率受限于本机实际留存的文件（809 个文件名 / 2077 条消息）
#    · 语音：media_0.db 的 VoiceInfo.voice_data 是 SILK_V3（首字节 0x02 前缀）
#            用 pilk 解成 PCM 再包成 WAV（浏览器可直接播）
#            判据 = (chat_name_id → Name2Id.user_name 的 hash, create_time)
# ===========================================================================

def _month8(ts: int) -> str:
    return dt.datetime.fromtimestamp(
        ts, dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m")

def run_videos(account_root: str, vault: str, out: str, plain_dbs: str = "") -> dict:
    """视频：按 (月份, 缩略图字节数) 关联；优先给可播放的 mp4，否则给封面图。"""
    vroot = os.path.join(account_root, "msg", "video")
    thumbs: dict[tuple, str] = {}
    for p in glob.glob(vroot + "/*/*_thumb.jpg"):
        thumbs[(os.path.basename(os.path.dirname(p)), os.path.getsize(p))] = p
    print("  视频缩略图索引：%d" % len(thumbs))

    conn = sqlite3.connect(vault)
    rows = conn.execute("SELECT msg_id, source_file, ts FROM msg "
                        "WHERE type='video'").fetchall()
    chash = _conv_hash_map(conn, rows, plain_dbs)

    mdir = os.path.join(out, "video")
    os.makedirs(mdir, exist_ok=True)
    upd, playable, cover_only = [], 0, 0
    zb = zstd.ZstdDecompressor()
    # 从明文库补 cdnthumblength：(hash, ct) → 缩略图长度
    tlen = _video_thumb_len(_plain_root(out))
    for mid, sf, ts in rows:
        if not ts:
            continue
        k = _username_of(sf)
        h = chash.get(k)
        if not h:
            continue
        mo = _month8(ts)
        tl = tlen.get((h, ts))
        if not tl:
            continue
        src = thumbs.get((mo, tl))
        if not src:
            continue
        stem = os.path.basename(src)[:-10]
        mp4 = os.path.join(os.path.dirname(src), stem + ".mp4")
        use, name = (mp4, stem + ".mp4") if os.path.exists(mp4) else (src, stem + "_thumb.jpg")
        rel = "video/" + name
        dst = os.path.join(out, rel)
        if not os.path.exists(dst):
            shutil.copyfile(use, dst)
        upd.append(("/media/" + rel, mid))
        playable += 1 if os.path.exists(mp4) else 0
        cover_only += 0 if os.path.exists(mp4) else 1
    conn.executemany("UPDATE msg SET media=? WHERE msg_id=?", upd)
    conn.commit()
    conn.close()
    print("  视频关联：%d / %d（可播放 %d，仅封面 %d）"
          % (len(upd), len(rows), playable, cover_only))
    return {"videos": len(rows), "linked": len(upd),
            "playable": playable, "cover_only": cover_only}

def run_files(account_root: str, vault: str, out: str) -> dict:
    """文件：按磁盘上的原始文件名匹配。文件本身是明文，直接引用无需拷贝。"""
    froot = os.path.join(account_root, "msg", "file")
    index: dict[str, str] = {}
    for p in glob.glob(froot + "/*/*"):
        if os.path.isfile(p):
            index.setdefault(os.path.basename(p), p)
    print("  本地文件索引：%d" % len(index))

    conn = sqlite3.connect(vault)
    rows = conn.execute("SELECT msg_id, content FROM msg WHERE type='file'").fetchall()
    upd = []
    for mid, content in rows:
        m = re.match(r"\[文件\]\s*(.+)$", (content or "").strip())
        if not m:
            continue
        nm = m.group(1).strip()
        src = index.get(nm)
        if not src:
            # 容错：去掉结尾的 (n) 再试
            alt = re.sub(r"\(\d+\)$", "", nm)
            src = index.get(nm + "(1)") or index.get(alt)
        if src:
            # 文件名可能含 / 等字符 → 用 urlencode 交给浏览器处理
            rel = "file/" + os.path.basename(src)
            upd.append((nm, src, mid))
    # 复制到媒体目录（用 md5(名字) 作文件名，避免中文/特殊字符与重名问题）
    fdir = os.path.join(out, "file")
    os.makedirs(fdir, exist_ok=True)
    final = []
    for nm, src, mid in upd:
        safe = hashlib.md5(nm.encode()).hexdigest()[:16] + os.path.splitext(nm)[1]
        dst = os.path.join(fdir, safe)
        if not os.path.exists(dst):
            shutil.copyfile(src, dst)
        final.append(("/media/file/" + safe, mid, nm))
    conn.executemany("UPDATE msg SET media=? WHERE msg_id=?",
                     [(a, b) for a, b, _ in final])
    conn.commit()
    conn.close()
    print("  文件关联：%d / %d" % (len(final), len(rows)))
    return {"files": len(rows), "linked": len(final)}

def run_voice(plain_dbs: str, vault: str, out: str) -> dict:
    """语音：VoiceInfo 的 SILK_V3 → PCM → WAV（浏览器可直接播放）。"""
    try:
        os.chdir("/")                      # 避免源码目录遮蔽 site-packages
        import pilk
    except Exception as ex:
        print("  ✗ 未找到 silk 解码器（pilk）：%r" % (ex,))
        return {"voice": 0, "linked": 0, "error": "no-silk-decoder"}

    src = os.path.join(plain_dbs, "message", "media_0.db")
    if not os.path.exists(src):
        print("  未找到 media_0.db")
        return {"voice": 0, "linked": 0}
    c = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
    names = {i + 1: r[0] for i, r in enumerate(
        c.execute("SELECT user_name FROM Name2Id").fetchall())}
    rows = c.execute("SELECT chat_name_id, create_time, voice_data "
                     "FROM VoiceInfo").fetchall()
    c.close()
    print("  VoiceInfo：%d 条，Name2Id %d 条" % (len(rows), len(names)))

    vdir = os.path.join(out, "voice")
    os.makedirs(vdir, exist_ok=True)
    tmp = os.path.join(out, "_tmp")
    os.makedirs(tmp, exist_ok=True)

    # (hash, ct) → wav 相对路径
    idx: dict[tuple, str] = {}
    ok = 0
    for cn, ct, blob in rows:
        uname = names.get(cn)
        if not uname or not blob or len(blob) < 64:
            continue
        silk = blob[1:] if blob[:1] == b"\x02" else blob
        h = hashlib.md5(uname.encode()).hexdigest()
        sp = os.path.join(tmp, "a.silk")
        dp = os.path.join(tmp, "a.pcm")
        try:
            with open(sp, "wb") as f:
                f.write(silk)
            pilk.decode(sp, dp)
            pcm = open(dp, "rb").read()
            name = "%s_%d.wav" % (h[:16], ct)
            dst = os.path.join(vdir, name)
            with wave.open(dst, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
                w.writeframes(pcm)
            idx[(h, ct)] = "voice/" + name
            ok += 1
        except Exception as e:
            log.debug("语音解码失败，跳过该条：%s", e)
            continue
    print("  解码成功：%d / %d" % (ok, len(rows)))

    conn = sqlite3.connect(vault)
    vrows = conn.execute("SELECT msg_id, source_file, ts FROM msg "
                         "WHERE type='voice'").fetchall()
    chash = _conv_hash_map(conn, vrows, plain_dbs)
    upd = []
    for mid, sf, ts in vrows:
        h = chash.get(_username_of(sf))
        rel = idx.get((h, ts)) if h and ts else None
        if rel:
            upd.append(("/media/" + rel, mid))
    conn.executemany("UPDATE msg SET media=? WHERE msg_id=?", upd)
    conn.commit()
    conn.close()
    print("  语音关联：%d / %d" % (len(upd), len(vrows)))
    return {"voice": len(vrows), "decoded": ok, "linked": len(upd)}

def run_emoji(plain_dbs: str, vault: str, out: str,
              limit: int = 6000, workers: int = 8) -> dict:
    """表情包（动画表情）：本地不缓存 → 从消息 XML 自带的 cdnurl 下载补齐。

    ── 实测（2026-10-02）：`<emoji ... md5="..." cdnurl="http://.../stodownload?m=<md5>&filekey=...">`
    里的 cdnurl **仍然可用**（抽样 6/6 下载成功，返回合法 GIF/PNG）。
    本地 `resource/emoticon_webp` 只有 109 个内置表情，用户收藏的表情在
    `emoticon.db` 里只有 505 条且多为远程 URL —— 所以「下载」是唯一可行路径。

    为控制体积与请求量，只下载**出现频次最高的前 limit 个**表情
    （表情包复用率极高，Top 数千即可覆盖绝大多数消息）。
    """
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    zb = zstd.ZstdDecompressor()

    def dec(b):
        try:
            return zb.decompress(b)
        except Exception:
            return b""

    # ① 扫明文库： (hash, ct) → md5 ；md5 频次
    ct2md5: dict[tuple, str] = {}
    freq: dict[str, int] = {}
    url: dict[str, str] = {}
    for db in sorted(glob.glob(plain_dbs + "/message/message_*.db")):
        try:
            c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
            tabs = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'Msg\\_%' ESCAPE '\\'")]
            for t in tabs:
                h = t[4:]
                try:
                    rows = c.execute("SELECT create_time, message_content FROM %s "
                                     "WHERE local_type=47" % t).fetchall()
                except sqlite3.Error as e:
                    log.warning("读取表情库失败，该库表情无法关联：%s", e)
                    continue
                for ct, mc in rows:
                    m = re.search(rb"<emoji ([^>]*)>", dec(mc))
                    if not m:
                        continue
                    a = dict(re.findall(rb'([a-zA-Z0-9_]+)\s*=\s*"([^"]*)"', m.group(1)))
                    md5 = a.get(b"md5", b"").decode()
                    if not md5:
                        continue
                    ct2md5[(h, ct)] = md5
                    freq[md5] = freq.get(md5, 0) + 1
                    u = a.get(b"cdnurl", b"").decode().replace("&amp;", "&")
                    if u:
                        url.setdefault(md5, u)
            c.close()
        except sqlite3.Error as e:
            log.warning("表情表查询失败，该会话表情无法关联：%s", e)
            continue
    print("  表情：消息 %d 条，唯一 md5 %d，其中带 cdnurl %d"
          % (len(ct2md5), len(freq), len(url)))

    top = [m for m, _ in sorted(freq.items(), key=lambda x: -x[1]) if m in url][:limit]
    edir = os.path.join(out, "emoji")
    os.makedirs(edir, exist_ok=True)
    print("  待下载 Top%d 表情" % len(top))

    def fetch(md5):
        for ext in (".gif", ".png", ".jpg", ".webp"):
            if os.path.exists(os.path.join(edir, md5 + ext)):
                return (md5, md5 + ext)
        try:
            req = urllib.request.Request(url[md5], headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://wx.qq.com/"})
            with urllib.request.urlopen(req, timeout=12) as r:
                blob = r.read()
            if len(blob) < 64:
                return (md5, None)
            ext = (".gif" if blob[:3] == b"GIF" else
                   ".png" if blob[:8] == b"\x89PNG\r\n\x1a\n" else
                   ".jpg" if blob[:3] == b"\xff\xd8\xff" else
                   ".webp" if blob[:4] == b"RIFF" else ".bin")
            with open(os.path.join(edir, md5 + ext), "wb") as f:
                f.write(blob)
            return (md5, md5 + ext)
        except Exception:
            return (md5, None)

    idx: dict[str, str] = {}
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (md5, fn) in enumerate(pool.map(fetch, top), 1):
            if fn:
                idx[md5] = "emoji/" + fn
                ok += 1
            if i % 1000 == 0:
                print("    ... %d/%d" % (i, len(top)))
    print("  下载成功：%d / %d" % (ok, len(top)))

    # ② 回填 vault（用 (hash, ct) 关联，与图片/语音同一套路）
    conn = sqlite3.connect(vault)
    vrows = conn.execute("SELECT msg_id, source_file, ts FROM msg "
                         "WHERE type='emoji'").fetchall()
    chash = _conv_hash_map(conn, vrows, plain_dbs)
    upd = []
    for mid, sf, ts in vrows:
        h = chash.get(_username_of(sf))
        if not h or not ts:
            continue
        md5 = ct2md5.get((h, ts))
        rel = idx.get(md5) if md5 else None
        if rel:
            upd.append(("/media/" + rel, mid))
    conn.executemany("UPDATE msg SET media=? WHERE msg_id=?", upd)
    conn.commit()
    conn.close()
    print("  表情关联：%d / %d" % (len(upd), len(vrows)))
    return {"emoji": len(vrows), "downloaded": ok, "linked": len(upd)}

def run_wxam(account_root: str, vault: str, out: str, workers: int = 3) -> dict:
    """把 wxgf（WxAM）原图解码出来，回填 `media_full`。

    ── 为什么需要它：约 13% 的「原图/高清」在 V2 解密后是 `wxgf` 私有容器
    （内层其实是标准 HEVC 码流）。不解码时这些消息只能显示缩略图。
    ── 解码依赖：容器内 ffmpeg 的原生 hevc 解码器（无腾讯二进制、无 Windows 依赖）。

    ★ 关联链（实测得出；直接用尺寸匹配 XML 的 length 是**行不通**的）：
        消息 --(会话哈希, 月份, cdnthumblength)--> `_t.dat` 的 **stem**
             --(同一 stem)--> 原图（`.dat` / `_h.dat`，可能就是 wxgf）
             --(已解码)--> media_full
      依据：① `cdnthumblength` == `_t.dat` 明文字节数（实测命中 97.9%）
            ② 缩略图与原图**共享同一文件名 stem**（实测 36,145 对，如 `xx.dat` / `xx_t.dat`）
            ③ 而 wxgf 原图的长度**对不上** XML 的任何字段 —— 故不能用尺寸直接匹配原图

    落盘：`<out>/orig/<xx>/<md5>.<jpg>`（md5 取 wxgf 明文哈希，**已存在则复用**，幂等）
    """
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import wv_wxam as W

    keys_p = os.path.join(out, "_keys.json")
    if not os.path.exists(keys_p):
        print("  缺 %s，请先跑 keys" % keys_p)
        return {"wxam": 0, "decoded": 0}
    keys = json.load(open(keys_p))
    aes_key = keys["aes_key"].encode()
    xor_key = int(keys["xor_key"])

    attach = os.path.join(account_root, "msg", "attach")
    files = glob.glob(attach + "/*/*/Img/*.dat")
    print("  扫描 .dat：%d" % len(files))

    _WX.update(aes_key=aes_key, xor_key=xor_key, out=out)
    thumb: dict[tuple, str] = {}     # (hash, 月份, 缩略图明文尺寸) → stem
    wxrel: dict[str, str] = {}       # stem → 已解码原图相对路径
    stat = collections.Counter()
    t0 = time.time()
    with Pool(workers, _wx_init, (aes_key, xor_key, out)) as pool:
        for i, r in enumerate(pool.imap_unordered(_wx_worker, files, chunksize=100), 1):
            if not r:
                continue
            kind, path, size, stem, rel = r
            parts = os.path.relpath(path, attach).split(os.sep)
            if len(parts) >= 2 and kind == "t":
                thumb[(parts[0], parts[1], size)] = stem
            elif kind == "orig":
                if rel:
                    wxrel[stem] = rel
                    stat["原图可用"] += 1
                else:
                    stat["原图解码失败"] += 1
            else:
                stat["非wxgf-%s" % kind] += 1
            if i % 20000 == 0:
                print("    ... %d/%d" % (i, len(files)), flush=True)
    print("  扫描完成 %.1fs：%s" % (time.time() - t0, dict(stat)))
    print("  缩略图尺寸索引 %d 条；可关联的 wxgf 原图 %d 个" % (len(thumb), len(wxrel)))

    # 回填
    conn = sqlite3.connect(vault)
    if "media_full" not in {r[1] for r in conn.execute("PRAGMA table_info(msg)")}:
        conn.execute("ALTER TABLE msg ADD COLUMN media_full TEXT")
        conn.commit()
    rows = conn.execute("SELECT msg_id, source_file, ts, media FROM msg "
                        "WHERE type='image'").fetchall()
    root_pdb = _plain_root(out)
    chash = _conv_hash_map(conn, rows, root_pdb)
    tl = _plain_thumb_len(root_pdb)
    print("  明文库缩略图尺寸：%d 条" % len(tl))

    upd, only_full, steps = [], 0, collections.Counter()
    for mid, sf, ts, cur in rows:
        if not ts:
            continue
        h = chash.get(_username_of(sf))
        if not h:
            continue
        tlen = tl.get((h, ts))
        if not tlen:
            steps["无缩略图尺寸"] += 1
            continue
        stem = thumb.get((h, _month8(ts), tlen))
        if not stem:
            steps["缩略图未匹配"] += 1
            continue
        rel = wxrel.get(stem)
        if not rel:
            steps["该图原图非wxgf(无需原图)"] += 1
            continue
        upd.append(("/media/" + rel, mid))
        steps["命中原图"] += 1
        if not cur:
            only_full += 1
    conn.executemany("UPDATE msg SET media_full=? WHERE msg_id=?", upd)
    if only_full:
        conn.execute("UPDATE msg SET media=CASE WHEN media IS NULL OR media='' "
                     "THEN media_full ELSE media END WHERE media_full IS NOT NULL")
    conn.commit()
    conn.close()
    print("  链路统计：%s" % dict(steps))
    print("  原图回填 media_full：%d 条（其中 %d 条原先无图，已直接用原图显示）"
          % (len(upd), only_full))
    return {"wxam": len(files), "decoded": stat["原图可用"],
            "orig_index": len(wxrel), "thumb_index": len(thumb),
            "linked_full": len(upd), "steps": dict(steps)}

_WX: dict = {}

def _wx_init(aes_key, xor_key, out):
    _WX.update(aes_key=aes_key, xor_key=xor_key, out=out)

def _wx_worker(path):
    """解密一个 .dat，分类返回：
       ('t',   path, 明文尺寸, stem, None)        —— 缩略图
       ('orig',path, 明文尺寸, stem, rel|None)    —— 原图（rel 为 wxgf 解码产物）
       ('other',...)、None                        —— 非图像
    """
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import wv_wxam as W

    try:
        blob = open(path, "rb").read()
    except OSError:
        return None
    inner = W.v2_strip(blob, _WX["aes_key"], _WX["xor_key"])
    if not inner:
        return None

    b = os.path.basename(path)[:-4]
    stem, suf = b, "orig"
    for sname in ("_h", "_t_W", "_t"):
        if b.endswith(sname):
            stem, suf = b[:-len(sname)], ("t" if sname in ("_t", "_t_W") else "h")
            break

    if suf == "t":
        return ("t", path, len(inner), stem, None)
    if inner[:4] != b"wxgf":
        return ("other", path, len(inner), stem, None)

    md5 = hashlib.md5(inner).hexdigest()
    sub = md5[:2]
    # 已解码过（任一扩展名）→ 复用，不再跑 ffmpeg
    for ext in ("jpg", "png", "gif"):
        rel_try = "orig/%s/%s.%s" % (sub, md5, ext)
        if os.path.exists(os.path.join(_WX["out"], rel_try)):
            return ("orig", path, len(inner), stem, rel_try)

    r = W.decode_wxgf(inner)
    if not r["ok"]:
        return ("orig", path, len(inner), stem, None)
    rel = "orig/%s/%s.%s" % (sub, md5, r["fmt"])
    dst = os.path.join(_WX["out"], rel)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = "%s.%d.tmp" % (dst, os.getpid())
        with open(tmp, "wb") as f:
            f.write(r["data"])
        os.replace(tmp, dst)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:  # 可安全忽略：临时文件清理失败，主流程不受影响
            pass
    return ("orig", path, len(inner), stem, rel)

def _plain_thumb_len(plain_dbs: str) -> dict:
    """(会话哈希, create_time) → cdnthumblength（缩略图明文字节数）。"""
    out: dict[tuple, int] = {}
    zb = zstd.ZstdDecompressor()

    def dec(b):
        try:
            return zb.decompress(b)
        except Exception:
            return b""

    for db in sorted(glob.glob(plain_dbs + "/message/message_*.db")):
        try:
            c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
            tabs = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'Msg\\_%' ESCAPE '\\'")]
            for t in tabs:
                h = t[4:]
                try:
                    rows = c.execute("SELECT create_time, message_content FROM %s "
                                     "WHERE local_type=3" % t).fetchall()
                except sqlite3.Error as e:
                    log.warning("读取明文库失败，该库缩略图尺寸整体缺失：%s", e)
                    continue
                for ct, mc in rows:
                    m = re.search(rb"<img ([^>]*)>", dec(mc))
                    if not m:
                        continue
                    a = dict(re.findall(rb'([a-zA-Z0-9_]+)\s*=\s*"([^"]*)"', m.group(1)))
                    v = a.get(b"cdnthumblength")
                    if v and v.isdigit() and int(v) > 0:
                        out[(h, ct)] = int(v)
            c.close()
        except sqlite3.Error as e:
            log.warning("消息表查询失败，该会话缩略图尺寸缺失：%s", e)
            continue
    return out

def _plain_root(out: str = "") -> str:
    """解密明文库目录（wv_media 多处复用）。"""
    return os.environ.get("WV_PLAIN_DBS", "/data/wx_backup/plain_dbs")

_PSIZES_CACHE: dict = {}

def _name_hash_map(plain_dbs: str) -> dict:
    """显示名/微信号 → md5(username)。

    ── 实测（2026-10-02）：vault 里 `source_file` 的 basename 是**会话显示名**
    （备注/昵称/群名），不是微信号。与 contact 的 remark/nick_name/alias/username
    比对命中 **465/469 = 99.1%**，比时间戳指纹（359/469）准得多。
    所以优先用名称直连，指纹只做兜底。
    """
    db = os.path.join(plain_dbs, "contact", "contact.db")
    out: dict[str, str] = {}
    if not os.path.exists(db):
        return out
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        for u, rm, nk, al in c.execute(
                "SELECT username, remark, nick_name, alias FROM contact"):
            if not u:
                continue
            h = hashlib.md5(u.encode()).hexdigest()
            for k in (u, rm, nk, al):
                if k:
                    out.setdefault(k, h)
        # 群：chat_room 的 username 也是 md5 源
        for (u,) in c.execute("SELECT username FROM chat_room"):
            if u:
                out.setdefault(u, hashlib.md5(u.encode()).hexdigest())
        c.close()
    except sqlite3.Error as e:
        log.warning("联系人表查询失败，显示名→username 映射不完整，关联结果会静默变少：%s", e)
        pass
    return out

def _conv_hash_map(conn, rows, plain_dbs: str = "") -> dict:
    """会话 source_file 别名 → Msg_<hash>。

    两级策略：
      ① 名称直连（显示名/昵称/备注/微信号 → md5(username)）—— 实测 99.1%
      ② 时间戳指纹兜底（同名冲突或非联系人会话）
    """
    root = plain_dbs or _plain_root("")
    cache = _NAME_CACHE
    if root not in cache:
        cache[root] = _name_hash_map(root)
    name2hash = cache[root]

    best: dict[str, str] = {}
    unresolved = []
    for r in rows:
        k = _username_of(r[1])
        h = name2hash.get(k)
        if h:
            best[k] = h
        elif k and k not in unresolved:
            unresolved.append(k)

    # 兜底：时间戳指纹
    if unresolved:
        if root not in _PSIZES_CACHE:
            try:
                _PSIZES_CACHE[root] = _plain_sizes(root)
            except Exception:
                _PSIZES_CACHE[root] = {}
        c2h: dict[int, set] = collections.defaultdict(set)
        for (h, ct) in _PSIZES_CACHE[root]:
            c2h[ct].add(h)
        uset = set(unresolved)
        votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        for r in rows:
            k = _username_of(r[1])
            if k in uset and r[2]:
                for h in c2h.get(r[2], ()):
                    votes[k][h] += 1
        for k, cnt in votes.items():
            if cnt:
                best[k] = cnt.most_common(1)[0][0]
    return best

_NAME_CACHE: dict = {}

if __name__ == "__main__":
    main()
