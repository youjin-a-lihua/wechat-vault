#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Vault - 通用聊天记录解析器
=====================================

设计目标：把微信各种来源、各种格式的聊天记录导出，统一解析为同一种内部结构。

支持格式（自动嗅探）：
  1. CSV   —— WeChatMsg / PyWxDump 风格（含表头字段识别）
  2. JSON  —— WeChatMsg 风格 / 通用 [{...}] 数组
  3. HTML  —— 微信官方导出 / WeChatMsg HtmlExporter / PyWxDump exportHtml
  4. TXT   —— 微信官方导出段落式 / 简易时间戳行

内部统一结构（Normalized Message）：
  {
    "msg_id":     str,          # 稳定唯一 ID（由来源+序号+时间派生）
    "seq":        int,          # 会话内序号（从 0 递增）
    "ts":         int | None,   # Unix 时间戳（秒）
    "time_str":   str | None,   # 原始时间字符串（保留）
    "sender":     str,          # 发送者显示名
    "is_self":    bool,         # 是否本人发出（决定左右气泡）
    "type":       str,          # text / image / voice / video / file / link / system / other
    "content":    str,          # 文本正文（非文本消息为描述或空）
    "media":      str | None,   # 媒体相对路径
    "raw":        dict,         # 原始字段留存（便于回溯）
  }

会话结构（Normalized Conversation）：
  {
    "conv_id":    str,
    "title":      str,          # 联系人 / 群名
    "source":     str,          # csv / json / html / txt
    "source_file":str,
    "is_group":   bool,
    "members":    [str],        # 群成员（若能解析出）
    "messages":   [NormalizedMessage],
    "stats":      {...},
  }
"""

from __future__ import annotations

import csv
import json
import os
import re
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Any, Iterable
import logging

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

CST = timezone(timedelta(hours=8))  # 中国标准时间

# 消息类型归一化映射（覆盖 WeChatMsg / PyWxDump / 官方导出的各种叫法）
TYPE_MAP = {
    # 文本
    "1": "text", "text": "text", "文字": "text", "文本": "text", "txt": "text",
    # 图片
    "3": "image", "image": "image", "img": "image", "图片": "image", "图片消息": "image",
    # 语音
    "34": "voice", "voice": "voice", "audio": "voice", "语音": "voice", "语音消息": "voice",
    # 视频
    "43": "video", "video": "video", "视频": "video", "视频消息": "video",
    # 文件
    "49": "file", "file": "file", "文件": "file", "文件消息": "file",
    # 链接 / 公众号
    "49_link": "link", "link": "link", "链接": "link", "分享": "link",
    # 位置
    "48": "location", "location": "location", "位置": "location",
    # 名片
    "42": "card", "card": "card", "名片": "card",
    # 系统消息
    "10000": "system", "system": "system", "系统消息": "system", "sys": "system",
    # 转账 / 红包
    "transfer": "transfer", "转账": "transfer",
    "redpacket": "redpacket", "红包": "redpacket",
    # 表情
    "47": "emoji", "emoji": "emoji", "表情": "emoji", "动画表情": "emoji",
    # 其他
    "other": "other", "其他": "other",
}

# 各类时间字符串格式（按优先级尝试）
TIME_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y年%m月%d日 %H:%M:%S",
    "%Y年%m月%d日 %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m-%d %H:%M",
    "%H:%M:%S",
    "%H:%M",
]

# 表头别名映射：把各来源的列名统一到内部字段名
HEADER_ALIASES = {
    # 时间
    "ts": "ts", "time": "ts", "createtime": "ts", "create_time": "ts",
    "unixtime": "ts", "timestamp": "ts", "时间": "ts", "时间戳": "ts",
    # 时间字符串
    "strtime": "time_str", "time_str": "time_str", "datestring": "time_str",
    "日期": "time_str", "时间字符串": "time_str", "formattime": "time_str",
    # 发送者
    "sender": "sender", "nickname": "sender", "nick_name": "sender",
    "nick": "sender", "from": "sender", "fromuser": "sender",
    "sender_name": "sender", "发送者": "sender", "昵称": "sender", "发言人": "sender",
    "talkerid": "sender", "talker": "sender",
    # 是否本人
    "issender": "is_self", "is_sender": "is_self", "isself": "is_self", "isme": "is_self", "self": "is_self",
    "isfromme": "is_self", "发送方": "is_self", "是否本人": "is_self",
    # 内容
    "strcontent": "content", "content": "content", "msg": "content",
    "text": "content", "message": "content", "内容": "content", "消息": "content",
    # 类型
    "type": "type", "type_name": "type", "msgtype": "type", "msg_type": "type",
    "kind": "type", "类型": "type", "消息类型": "type",
    # 媒体
    "src": "media", "path": "media", "media": "media", "filepath": "media",
    "file": "media", "图片路径": "media", "媒体": "media",
    "mediapath": "media", "media_path": "media", "mediafile": "media",
    "mediafilepath": "media", "media_file": "media", "attachment": "media",
    "attach": "media", "file_path": "media", "附件": "media", "附件路径": "media",
    # 备注
    "remark": "remark", "备注": "remark", "备注名": "remark",
    # 群
    "roomname": "room", "room_name": "room", "room": "room", "chatroom": "room",
    "群": "room", "群名": "room",
    # ID
    "localid": "msg_id", "id": "msg_id", "msgid": "msg_id",
    "msgsvrid": "server_id", "msg_svr_id": "server_id",
}

# 文本中识别媒体/系统提示的正则
RE_IMG_TAG = re.compile(r"\[图片\]|\[Photo\]|\[photo\]|<img[^>]*>", re.I)
RE_VOICE_TAG = re.compile(r"\[语音\]|\[Voice\]|\[voice\]|\[音频\]", re.I)
RE_VIDEO_TAG = re.compile(r"\[视频\]|\[Video\]|\[video\]", re.I)
RE_FILE_TAG = re.compile(r"\[文件\]|\[File\]|\[file\]|\[文档\]", re.I)
RE_EMOJI_TAG = re.compile(r"\[动画表情\]|\[表情\]|\[Emoji\]|\[Sticker\]", re.I)
RE_LINK_TAG = re.compile(r"\[链接\]|\[Link\]|\[分享\]", re.I)
RE_SYS_TAG = re.compile(
    r"^\[系统\]|你已添加了|以上是打招呼|撤回了一条消息|"
    r"邀请.*加入了群聊|^\w+ 撤回|加入了群聊|退出了群聊"
)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def norm_key(k: str) -> str:
    """表头归一化：小写、去空格下划线连字符。"""
    return re.sub(r"[\s_\-]+", "", str(k or "").strip().lower())


def map_header(name: str) -> str | None:
    """把原始表头映射到内部字段名。"""
    return HEADER_ALIASES.get(norm_key(name))


def parse_time(value: Any) -> tuple[int | None, str | None]:
    """尽力把任意时间表示解析为 (unix_ts, 原始字符串)。"""
    if value is None or value == "":
        return None, None
    raw = str(value).strip()

    # 纯数字 → 可能是 Unix 时间戳（秒或毫秒）
    if re.fullmatch(r"\d{9,13}", raw):
        n = int(raw)
        if n > 10_000_000_000:  # 毫秒
            n //= 1000
        return n, raw

    # 尝试各种日期格式
    for fmt in TIME_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.year < 1970:  # 补全年份（如 "10-01 15:30"）
                dt = dt.replace(year=datetime.now().year)
            return int(dt.replace(tzinfo=CST).timestamp()), raw
        except ValueError as e:
            log.debug("时间解析失败，沿用原始值：%s", e)
            continue

    # 尝试 ISO
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return int(dt.timestamp()), raw
    except Exception as e:
        log.debug("ISO 时间解析失败，沿用原始值：%s", e)
        pass

    return None, raw


def fmt_time(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d %H:%M:%S")


def to_bool(v: Any) -> bool:
    """把各种"是否本人"的表示统一成 bool。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    s = str(v or "").strip().lower()
    return s in {"1", "true", "yes", "y", "self", "me", "是", "本人", "我", "发送"}


def infer_type(content: str, given_type: Any = None) -> str:
    """推断消息类型：优先用给定类型，其次从内容里的 [标签] 推断。"""
    if given_type:
        mapped = TYPE_MAP.get(str(given_type).strip().lower())
        if mapped:
            return mapped
    c = str(content or "")
    if RE_IMG_TAG.search(c):
        return "image"
    if RE_VOICE_TAG.search(c):
        return "voice"
    if RE_VIDEO_TAG.search(c):
        return "video"
    if RE_FILE_TAG.search(c):
        return "file"
    if RE_EMOJI_TAG.search(c):
        return "emoji"
    if RE_LINK_TAG.search(c):
        return "link"
    if RE_SYS_TAG.search(c):
        return "system"
    return "text"


def make_msg_id(source: str, conv: str, seq: int, ts: int | None, content: str) -> str:
    """生成稳定唯一 ID（同样输入 → 同样 ID，便于增量去重）。"""
    h = hashlib.sha1(
        f"{source}|{conv}|{seq}|{ts}|{content[:200]}".encode("utf-8", "ignore")
    ).hexdigest()[:12]
    return f"{source}-{h}"


def clean_text(s: Any) -> str:
    """清理文本：去首尾空白，统一换行。"""
    if s is None:
        return ""
    t = str(s).replace("\r\n", "\n").replace("\r", "\n")
    return t.strip()


# ---------------------------------------------------------------------------
# 各格式解析器
# ---------------------------------------------------------------------------

def parse_csv(path: str) -> list[dict]:
    """解析 CSV（WeChatMsg / PyWxDump 风格，支持表头别名）。"""
    rows: list[dict] = []
    # 尝试多种编码
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1"):
        try:
            with open(path, "r", encoding=enc, newline="") as f:
                text = f.read()
            break
        except (UnicodeDecodeError, LookupError) as e:
            log.debug("该编码不可用，尝试下一种：%s", e)
            continue
    if text is None:
        raise ValueError(f"无法解码 CSV 文件: {path}")

    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        return rows

    # 建立 原始表头 -> 内部字段 的映射，并按优先级排序
    # 注意：多个表头可能映射到同一内部字段（如 NickName 与 TalkerId 都想当 sender），
    # 需要按优先级取舍：优先级数字越小越优先（显示名 > 微信号）。
    HEADER_PRIORITY = {
        "sender": {  # 显示名 > 备注名 > 微信号
            "nickname": 0, "nick_name": 0, "nick": 0, "sender": 0,
            "sender_name": 0, "from": 0, "fromuser": 0,
            "昵称": 0, "发言人": 0, "发送者": 0,
            "remark": 1, "备注": 1, "备注名": 1,
            "talkerid": 2, "talker": 2,
        },
        "ts": {"createtime": 0, "create_time": 0, "ts": 0, "time": 0,
               "unixtime": 0, "timestamp": 0, "时间戳": 0, "时间": 1},
        "content": {"strcontent": 0, "content": 0, "msg": 0, "message": 0,
                    "text": 0, "内容": 0, "消息": 0},
        "media": {"src": 0, "path": 0, "filepath": 0, "media": 0,
                  "图片路径": 0, "媒体": 0},
    }

    # (原始表头, 内部字段, 优先级) 按优先级升序排列
    plan: list[tuple[str, str, int]] = []
    for h in reader.fieldnames:
        m = map_header(h)
        if m:
            prio = HEADER_PRIORITY.get(m, {}).get(norm_key(h), 5)
            plan.append((h, m, prio))
    plan.sort(key=lambda x: x[2])

    for raw in reader:
        rec: dict = {"raw": dict(raw)}
        for orig, internal, _prio in plan:
            if internal in rec:      # 已被更高优先级填过 → 跳过
                continue
            v = raw.get(orig)
            if v is None or v == "":
                continue
            rec[internal] = v
        rows.append(rec)
    return rows


def parse_json(path: str) -> list[dict]:
    """解析 JSON（WeChatMsg 风格 / 通用数组 / 对象包裹）。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    # 定位消息数组
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("messages", "msgs", "data", "list", "records", "chat"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        else:
            items = [data]
    else:
        return []

    rows: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        rec = {"raw": it}
        for k, v in it.items():
            m = map_header(k)
            if m and v not in (None, ""):
                rec.setdefault(m, v)
        # 未映射到的字段也保留在 raw 里
        rows.append(rec)
    return rows


def parse_html(path: str) -> list[dict]:
    """
    解析 HTML（微信官方导出 / WeChatMsg / PyWxDump）。

    策略：不依赖固定 class 名，而是用多重启发式：
      1. 优先找带 message/chat/msg 语义的容器
      2. 从容器里抽取 时间/发送者/内容
      3. 兜底：按行文本 + 时间戳正则切分
    """
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        html = f.read()

    rows: list[dict] = []

    # --- 先尝试结构化提取 ---
    # 常见容器 class 关键词。
    # 不用 lookahead（会吃掉最后一条），改为在全文里逐个定位消息块起始位置，
    # 再用"下一个块起点"来切分。
    starts = [
        m.start() for m in re.finditer(
            r'<div[^>]+class="[^"]*(?:message|msg|chat-item|chat_item|bubble)[^"]*"',
            html, re.I,
        )
    ]
    if starts:
        blocks = []
        for i, s in enumerate(starts):
            e = starts[i + 1] if i + 1 < len(starts) else len(html)
            blocks.append(html[s:e])
    else:
        # 宽松：任何带 message 语义的 div
        blocks = re.findall(
            r'<div[^>]+class="[^"]*messag[^"]*"[^>]*>(.*?)</div>', html, re.I | re.S
        )

    if blocks:
        for b in blocks:
            rows.append(_extract_from_block(b))
        if any(r.get("content") for r in rows):
            return rows

    # --- 兜底：按文本行解析 ---
    return parse_html_as_text(html)


def _extract_from_block(block: str) -> dict:
    """从一个消息块里抽取字段。"""
    rec: dict = {"raw": {}}

    # 时间
    m = re.search(r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?\s*\d{1,2}:\d{2}(?::\d{2})?)", block)
    if m:
        rec["time_str"] = m.group(1)

    # 发送者
    m = re.search(
        r'<(?:span|div|p|a)[^>]+class="[^"]*(?:sender|nickname|name|user|from)[^"]*"[^>]*>(.*?)</',
        block, re.I | re.S,
    )
    if m:
        rec["sender"] = clean_text(re.sub(r"<[^>]+>", "", m.group(1)))

    # 本人判断
    if re.search(r'class="[^"]*(?:self|me|right|out)[^"]*"', block, re.I):
        rec["is_self"] = "1"

    # 内容
    m = re.search(
        r'<(?:div|span|p)[^>]+class="[^"]*(?:content|text|msg|bubble|body)[^"]*"[^>]*>(.*?)</',
        block, re.I | re.S,
    )
    body = m.group(1) if m else block
    # 提取图片
    im = re.search(r'<img[^>]+src="([^"]+)"', body, re.I)
    if im:
        rec["media"] = im.group(1)
    txt = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    txt = re.sub(r"<[^>]+>", "", txt)
    rec["content"] = clean_text(txt)
    rec["raw"]["block"] = True
    return rec


def parse_html_as_text(html: str) -> list[dict]:
    """兜底：把 HTML 当纯文本，用时间戳切分消息。"""
    # 去 script/style
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.I | re.S)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"</(?:div|p|li|tr|h\d)>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", html)
    return parse_txt_text(text)


def parse_txt(path: str) -> list[dict]:
    """解析 TXT。"""
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16"):
        try:
            with open(path, "r", encoding=enc) as f:
                text = f.read()
            break
        except (UnicodeDecodeError, LookupError) as e:
            log.debug("该编码不可用，尝试下一种：%s", e)
            continue
    if text is None:
        raise ValueError(f"无法解码 TXT 文件: {path}")
    return parse_txt_text(text)


# 时间戳片段（供多种布局复用）
_T_FULL = r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?\s+\d{1,2}:\d{2}(?::\d{2})?"
_T_MDHM = r"\d{1,2}[-/月]\d{1,2}[日]?\s+\d{1,2}:\d{2}(?::\d{2})?"
_T_HM = r"\d{1,2}:\d{2}(?::\d{2})?"

# 行首时间戳（多种写法）
TIME_LINE_PATTERNS = [
    re.compile(r"^\s*[\[\(]?(" + _T_FULL + r")[\]\)]?\s*(.*)$"),
    re.compile(r"^\s*[\[\(]?(" + _T_MDHM + r")[\]\)]?\s*(.*)$"),
    re.compile(r"^\s*[\[\(]?(" + _T_HM + r")[\]\)]?\s*(.*)$"),
]

# 「发送者 + 分隔符 + 时间戳」在**行首**的格式（微信导出常见）
#   张三\t2026-10-01 20:15:00
#   张三  2026/10/1 20:15
#   张三, 2026-10-01 20:15:00
# 要求发送者较短且不含冒号/标点，避免误吃普通正文。
SENDER_TIME_PATTERNS = [
    re.compile(r"^\s*([^\t:：,，|]{1,24})[\t]+(" + _T_FULL + r"|" + _T_MDHM + r"|" + _T_HM + r")\s*$"),
    re.compile(r"^\s*([^\t:：,，|]{1,24})[,，|]\s*(" + _T_FULL + r"|" + _T_MDHM + r"|" + _T_HM + r")\s*$"),
    re.compile(r"^\s*([^\t:：,，|]{1,24})\s{2,}(" + _T_FULL + r"|" + _T_MDHM + r"|" + _T_HM + r")\s*$"),
]

# 「时间戳 + 发送者」在行尾（如: 内容  20:15 张三）——少见，仅在前两种都不匹配时兜底
TAIL_TIME_SENDER = re.compile(
    r"^(.*?)\s*[\[\(]?(" + _T_FULL + r"|" + _T_MDHM + r")[\]\)]?\s*$"
)


# 最近一次 TXT 解析识别出的会话标题（由 parse_txt_text 写入，parse_file 读取）
LAST_TITLE_HINT: str | None = None


def parse_txt_text(text: str) -> list[dict]:
    """
    解析文本型聊天记录。

    兼容多种常见布局：
      A) 时间 发送者
         内容
      B) 发送者 时间          ← 微信导出常见（昵称 + Tab/逗号/多空格 + 时间戳）
         内容
      C) [时间] 内容
      D) 发送者: 内容         （整段式，无独立时间行）
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    rows: list[dict] = []
    cur: dict | None = None

    # 每次解析先清空上一次的标题残留（直接调用本函数时也保证语义干净）
    global LAST_TITLE_HINT
    LAST_TITLE_HINT = None

    # 微信官方导出的 TXT 首行是「会话标题」（如「家庭群」），不是消息。
    # 但纯文本转储（整份无时间戳）的首行就是内容，不能剥。
    # 判据：首行无时间戳，且后续存在至少一行「时间戳行 / 发送者+时间戳行」。
    title_hint = None
    _nl = [ln.rstrip() for ln in lines]
    _first_i = next((i for i, ln in enumerate(_nl) if ln.strip()), None)
    if _first_i is not None:
        _first = _nl[_first_i]
        _has_ts_anywhere = any(
            any(p.match(ln) for p in TIME_LINE_PATTERNS) or
            any(p.match(ln) for p in SENDER_TIME_PATTERNS)
            for ln in _nl[_first_i + 1:] if ln.strip()
        )
        _first_is_ts = any(
            p.match(_first) for p in TIME_LINE_PATTERNS
        ) or any(p.match(_first) for p in SENDER_TIME_PATTERNS)
        if _has_ts_anywhere and not _first_is_ts and len(_first.strip()) <= 64:
            title_hint = _first.strip()

    def flush():
        nonlocal cur
        if cur:
            cur["content"] = clean_text("\n".join(cur.pop("_buf", [])))
            rows.append(cur)
            cur = None

    for line in lines:
        s = line.rstrip()
        if not s.strip():
            continue
        if title_hint is not None and s.strip() == title_hint and not rows and cur is None:
            continue  # 剥掉首行会话标题

        matched = False

        # ---- ① 行首「发送者 + 分隔符 + 时间戳」（微信导出常见）----
        #      注意：必须先于纯时间模式尝试，否则 "张三\t20:15" 会被误判
        for pat in SENDER_TIME_PATTERNS:
            m = pat.match(s)
            if m:
                who = m.group(1).strip()
                # 发送者不能像正文（排除含句读的长文本）
                if who and len(who) <= 24 and not re.search(r"[。！？；]", who):
                    flush()
                    cur = {"time_str": m.group(2).strip(), "sender": who, "_buf": []}
                    matched = True
                    break
        if matched:
            continue

        # ---- ② 行首纯时间戳 ----
        for pat in TIME_LINE_PATTERNS:
            m = pat.match(s)
            if m:
                flush()
                tstr, rest = m.group(1), m.group(2).strip()
                rest = re.sub(r"^[\]\)】]\s*", "", rest)  # 去掉残留括号
                cur = {"time_str": tstr, "_buf": []}
                # rest 可能是 "发送者" 或 "发送者: 内容" 或 "内容"
                mm = re.match(r"^([^:：]{1,24})[:：]\s*(.*)$", rest)
                if mm:
                    cur["sender"] = mm.group(1).strip()
                    if mm.group(2):
                        cur["_buf"].append(mm.group(2))
                elif rest:
                    # 判断是不是纯发送者名（短且无标点）
                    if len(rest) <= 24 and not re.search(r"[。！？，,.!?]", rest):
                        cur["sender"] = rest
                    else:
                        cur["_buf"].append(rest)
                matched = True
                break

        if matched:
            continue

        # ---- ③ 「内容 + 行尾时间戳」兜底（少见的 内容  20:15 布局）----
        if cur is None:
            m = TAIL_TIME_SENDER.match(s)
            if m and m.group(1).strip():
                flush()
                cur = {"time_str": m.group(2).strip(), "_buf": [m.group(1).strip()]}
                continue

        # 非时间行 → 归入当前消息
        if cur is not None:
            cur["_buf"].append(s)
        else:
            # 文件开头没有时间戳 → 当作独立内容
            cur = {"time_str": None, "_buf": [s]}

    flush()
    LAST_TITLE_HINT = title_hint
    return rows


# ---------------------------------------------------------------------------
# 统一装配
# ---------------------------------------------------------------------------

def sniff_format(path: str) -> str:
    """嗅探文件格式。"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return "csv"
    if ext in (".json",):
        return "json"
    if ext in (".html", ".htm"):
        return "html"
    if ext in (".txt", ".log", ".md"):
        return "txt"

    # 无扩展名 → 看内容
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
            head = f.read(4096)
        hs = head.lstrip()
        if hs.startswith("{") or hs.startswith("["):
            return "json"
        if re.search(r"<html|<div|<body|<p\b", head, re.I):
            return "html"
        if "," in head.split("\n")[0] and head.count("\n") > 1:
            return "csv"
    except Exception as e:
        log.debug("格式嗅探失败，按 html 处理：%s", e)
        pass
    return "txt"


def normalize_rows(rows: Iterable[dict], source: str, conv_id: str) -> list[dict]:
    """把各格式的原始行统一为标准消息结构。"""
    out: list[dict] = []
    for i, r in enumerate(rows):
        content = clean_text(r.get("content", ""))
        ts, tstr = parse_time(r.get("ts") or r.get("time_str"))
        sender = clean_text(r.get("sender", "")) or ("我" if to_bool(r.get("is_self")) else "对方")
        mtype = infer_type(content, r.get("type"))

        # time_str 归一化：
        #   - 若来源只给了 Unix 时间戳（如 CSV 的 CreateTime），tstr 会是纯数字串，
        #     此时用 ts 重新格式化成人类可读时间。
        #   - 若 ts 为空但 tstr 可解析，parse_time 已回填 ts。
        if not tstr or re.fullmatch(r"\d{9,13}", tstr.strip()):
            tstr = fmt_time(ts) if ts else (tstr or "")

        # 关键：来源提供的 ID（如 CSV 的 localId）只在单个会话内唯一，
        # 因此必须拼接 conv_id 前缀 + 格式，保证全局唯一，避免跨会话主键冲突。
        raw_id = str(r.get("msg_id") or "").strip()
        if raw_id:
            msg_id = f"{source}:{conv_id}:{raw_id}"
        else:
            msg_id = make_msg_id(source, conv_id, i, ts, content)

        msg = {
            "msg_id": msg_id,
            "seq": i,
            "ts": ts,
            "time_str": tstr or fmt_time(ts),
            "sender": sender,
            "is_self": to_bool(r.get("is_self")),
            "type": mtype,
            "content": content,
            "media": r.get("media") or None,
            "raw": r.get("raw", {}),
        }
        out.append(msg)
    return out


def build_stats(messages: list[dict]) -> dict:
    """计算会话统计。"""
    n_self = sum(1 for m in messages if m["is_self"])
    n_other = len(messages) - n_self
    tss = [m["ts"] for m in messages if m["ts"]]
    types: dict[str, int] = {}
    senders: dict[str, int] = {}
    for m in messages:
        types[m["type"]] = types.get(m["type"], 0) + 1
        senders[m["sender"]] = senders.get(m["sender"], 0) + 1
    return {
        "total": len(messages),
        "self": n_self,
        "other": n_other,
        "start_ts": min(tss) if tss else None,
        "end_ts": max(tss) if tss else None,
        "start": fmt_time(min(tss)) if tss else "",
        "end": fmt_time(max(tss)) if tss else "",
        "types": types,
        "top_senders": sorted(senders.items(), key=lambda x: -x[1])[:20],
    }


def parse_file(path: str, conv_id: str | None = None) -> dict:
    """解析单个文件 → 标准会话结构。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    fmt = sniff_format(path)
    conv_id = conv_id or os.path.splitext(os.path.basename(path))[0]

    parser = {"csv": parse_csv, "json": parse_json, "html": parse_html, "txt": parse_txt}[fmt]
    # 每次解析前清空上一个文件的标题残留（避免串台）
    global LAST_TITLE_HINT
    LAST_TITLE_HINT = None
    rows = parser(path)
    title_hint = LAST_TITLE_HINT
    messages = normalize_rows(rows, fmt, conv_id)

    # 群聊判断：不同发送者 > 2 且非"我/对方"
    senders = {m["sender"] for m in messages if m["sender"] not in ("我", "对方", "")}
    is_group = len(senders) > 2

    return {
        "conv_id": conv_id,
        "title": title_hint or conv_id,
        "source": fmt,
        "source_file": os.path.abspath(path),
        "is_group": is_group,
        "members": sorted(senders)[:200],
        "messages": messages,
        "stats": build_stats(messages),
    }


def parse_dir(dirpath: str) -> list[dict]:
    """批量解析目录下所有可识别文件。"""
    exts = {".csv", ".json", ".html", ".htm", ".txt"}
    out: list[dict] = []
    for root, _dirs, files in os.walk(dirpath):
        for fn in sorted(files):
            if os.path.splitext(fn)[1].lower() not in exts:
                continue
            p = os.path.join(root, fn)
            try:
                out.append(parse_file(p))
            except Exception as e:  # 单文件失败不影响整体
                out.append({
                    "conv_id": os.path.splitext(fn)[0],
                    "title": fn,
                    "source": "error",
                    "source_file": p,
                    "is_group": False,
                    "members": [],
                    "messages": [],
                    "stats": {},
                    "error": str(e),
                })
    return out


# ---------------------------------------------------------------------------
# 命令行自测
# ---------------------------------------------------------------------------

def _selftest():
    """内置自测：用合成样本验证四种格式解析器。"""
    import tempfile

    samples = {
        "csv": (
            "chat.csv",
            "localId,TalkerId,Type,IsSender,CreateTime,StrTime,StrContent,NickName\n"
            "1,wxid_abc,1,0,1735689600,2025-01-01 10:00:00,你好呀,小明\n"
            "2,wxid_abc,1,1,1735689660,2025-01-01 10:01:00,你好！我是AKI,我\n"
            "3,wxid_abc,3,0,1735689720,2025-01-01 10:02:00,[图片],小明\n",
        ),
        "json": (
            "chat.json",
            json.dumps([
                {"id": 1, "type_name": "text", "is_sender": 0, "CreateTime": 1735689600,
                 "msg": "你好呀", "talker": "小明"},
                {"id": 2, "type_name": "text", "is_sender": 1, "CreateTime": 1735689660,
                 "msg": "你好！我是AKI", "talker": "我"},
            ], ensure_ascii=False),
        ),
        "html": (
            "chat.html",
            '<html><body>'
            '<div class="message"><div class="time">2025-01-01 10:00:00</div>'
            '<div class="sender">小明</div><div class="content">你好呀</div></div>'
            '<div class="message self"><div class="time">2025-01-01 10:01:00</div>'
            '<div class="sender">我</div><div class="content">你好！我是AKI</div></div>'
            '</body></html>',
        ),
        "txt": (
            "chat.txt",
            "2025-01-01 10:00:00 小明: 你好呀\n"
            "2025-01-01 10:01:00 我: 你好！我是AKI\n"
            "2025-01-01 10:02:00 小明: 在忙吗？\n",
        ),
    }

    tmp = tempfile.mkdtemp(prefix="wv_selftest_")
    ok = 0
    for fmt, (fn, content) in samples.items():
        p = os.path.join(tmp, fn)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        conv = parse_file(p, f"测试-{fmt}")
        msgs = conv["messages"]
        print(f"[{fmt:5}] 解析 {len(msgs)} 条 | 类型={conv['source']} | "
              f"首条={msgs[0]['sender']}:{msgs[0]['content'][:20] if msgs else 'N/A'}")
        if msgs:
            ok += 1
    print(f"\n自测结果：{ok}/{len(samples)} 种格式解析成功")
    return ok == len(samples)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if os.path.isdir(target):
            for c in parse_dir(target):
                print(json.dumps(c["stats"], ensure_ascii=False, indent=2))
        else:
            print(json.dumps(parse_file(target)["stats"], ensure_ascii=False, indent=2))
    else:
        sys.exit(0 if _selftest() else 1)
