#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Vault - 导出
===================

把归档库里的会话导出成**自包含**的文件，让数据能离开这个系统：

  · HTML —— 单文件、内嵌样式、图片走 `/media/` 引用；浏览器可直接
            「打印 → 另存为 PDF」，等价于 PDF 导出
  · CSV  —— Excel 可直接打开（带 UTF-8 BOM，中文不乱码）
  · TXT  —— 纯文本流水，最通用

设计取舍：
  - HTML 不自嵌图片 base64：那样会让文件膨胀几十倍且无法增量。
    改为引用 `/media/...`，导出文件在同源下打开即完整；
    如需彻底离线，用 `inline_media=True` 走 data URI（代价是文件大）。
  - CSV 用 utf-8-sig（BOM）—— 这是 Excel 认中文的关键。
"""

from __future__ import annotations

import csv
import html
import io
import json
import time
from pathlib import Path
import logging

log = logging.getLogger(__name__)

TYPE_LABEL = {
    "text": "文本", "image": "图片", "voice": "语音", "video": "视频",
    "file": "文件", "emoji": "表情", "link": "链接", "location": "位置",
    "card": "名片", "transfer": "转账", "redpacket": "红包",
    "system": "系统", "other": "其他",
}


def _fmt_time(ts) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except Exception:
        return ""


def _fmt_day(ts) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%Y年%m月%d日", time.localtime(int(ts)))
    except Exception:
        return ""


def _display(m: dict) -> str:
    """把一条消息渲染成可读文本（媒体类给中文占位）。"""
    t = m.get("type") or "text"
    content = (m.get("content") or "").strip()
    if t == "text":
        return content
    label = TYPE_LABEL.get(t, t)
    return f"[{label}]" + (f" {content}" if content else "")


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def conv_to_csv(conv: dict) -> bytes:
    """单会话 → CSV（Excel 友好，带 BOM）。"""
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(["时间", "发送者", "是否我发出", "类型", "内容", "媒体路径"])
    for m in conv.get("messages", []):
        w.writerow([
            _fmt_time(m.get("ts")),
            m.get("sender") or "",
            "是" if m.get("is_self") else "否",
            TYPE_LABEL.get(m.get("type") or "text", m.get("type") or ""),
            (m.get("content") or ""),
            (m.get("media") or ""),
        ])
    return buf.getvalue().encode("utf-8-sig")


def convs_to_csv(convs: list[dict]) -> bytes:
    """多会话 → 单个 CSV（多出「账号」「会话」两列）。"""
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(["账号", "会话", "时间", "发送者", "是否我发出", "类型", "内容", "媒体路径"])
    for c in convs:
        acct = c.get("account") or ""
        title = c.get("title") or c.get("conv_id") or ""
        for m in c.get("messages", []):
            w.writerow([
                acct, title,
                _fmt_time(m.get("ts")),
                m.get("sender") or "",
                "是" if m.get("is_self") else "否",
                TYPE_LABEL.get(m.get("type") or "text", m.get("type") or ""),
                (m.get("content") or ""),
                (m.get("media") or ""),
            ])
    return buf.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------------------
# TXT
# ---------------------------------------------------------------------------

def conv_to_txt(conv: dict) -> bytes:
    title = conv.get("title") or conv.get("conv_id") or "会话"
    acct = conv.get("account") or "默认账号"
    lines = [
        f"会话：{title}",
        f"账号：{acct}",
        f"消息数：{conv.get('n_msg', len(conv.get('messages', [])))}",
        f"导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 46,
        "",
    ]
    last_day = None
    for m in conv.get("messages", []):
        d = _fmt_day(m.get("ts"))
        if d and d != last_day:
            lines.append(f"——— {d} ———")
            last_day = d
        sender = "我" if m.get("is_self") else (m.get("sender") or "对方")
        hm = ""
        if m.get("ts"):
            try:
                hm = time.strftime("%H:%M", time.localtime(int(m["ts"])))
            except Exception:
                hm = ""
        lines.append(f"[{hm}] {sender}: {_display(m)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# HTML（单文件，可直接打印成 PDF）
# ---------------------------------------------------------------------------

_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
body{
  background:#f2f2f2;color:#191919;
  font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
       "Hiragino Sans GB","Microsoft YaHei",sans-serif;
  padding:26px 16px 60px;
}
.doc{max-width:760px;margin:0 auto;background:#fff;border-radius:12px;
     box-shadow:0 2px 18px rgba(0,0,0,.07);overflow:hidden}
.hd{padding:22px 26px;border-bottom:1px solid #ececec}
.hd h1{font-size:18px;font-weight:600}
.hd .meta{font-size:12.5px;color:#8c8c8c;margin-top:7px;line-height:1.9}
.hd .meta b{color:#5a5a5a;font-weight:500}
.body{padding:20px 26px 34px}
.day{
  text-align:center;margin:22px 0 16px;
}
.day span{
  display:inline-block;font-size:11.5px;color:#8c8c8c;
  background:#f2f2f2;border-radius:9px;padding:2.5px 11px;
}
.msg{display:flex;gap:10px;margin-bottom:15px;align-items:flex-start}
.msg.mine{flex-direction:row-reverse}
.av{
  width:34px;height:34px;border-radius:5px;flex-shrink:0;color:#fff;
  font-size:13px;font-weight:600;display:flex;align-items:center;
  justify-content:center;
}
.bd{max-width:74%}
.nm{font-size:11.5px;color:#8c8c8c;margin-bottom:4px}
.msg.mine .nm{text-align:right}
.bub{
  background:#fff;border:1px solid #e6e6e6;border-radius:7px;
  padding:8px 12px;word-break:break-word;white-space:pre-wrap;
}
.msg.mine .bub{background:#95ec69;border-color:#88e05e}
.bub .tp{color:#8c8c8c;font-size:13px}
.bub img{max-width:100%;border-radius:5px;display:block;margin-top:5px;cursor:zoom-in}
.sys{text-align:center;font-size:11.5px;color:#8c8c8c;margin:13px 0}
.ft{padding:15px 26px;border-top:1px solid #ececec;font-size:11.5px;color:#a0a0a0;text-align:center}
@media print{
  body{background:#fff;padding:0}
  .doc{box-shadow:none;border-radius:0;max-width:none}
  .msg{break-inside:avoid}
}
"""

_JS = """
document.addEventListener('click', e => {
  if (e.target.tagName !== 'IMG') return;
  const o = document.createElement('div');
  o.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:99;'
    +'display:flex;align-items:center;justify-content:center;cursor:zoom-out';
  const i = document.createElement('img');
  i.src = e.target.src;
  i.style.cssText='max-width:94vw;max-height:94vh';
  o.appendChild(i);
  o.onclick = () => o.remove();
  document.body.appendChild(o);
});
"""

_AV_COLORS = ['#5b8ff9', '#61ddaa', '#65789b', '#f6bd16', '#7262fd',
              '#78d3f8', '#9661bc', '#f6903d', '#008685', '#f08bb4']


def _av_color(name: str) -> str:
    h = 0
    for ch in str(name):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return _AV_COLORS[h % len(_AV_COLORS)]


def _initial(name: str) -> str:
    s = str(name or "?").strip()
    return s[0].upper() if s else "?"


def _media_html(m: dict, inline_media: bool, media_root: Path | None) -> str:
    rel = (m.get("media") or "").strip()
    if not rel:
        return ""
    t = m.get("type")
    src = rel
    if inline_media and media_root:
        try:
            from urllib.parse import quote
            p = (media_root / rel.lstrip("/")).resolve()
            p.relative_to(media_root.resolve())
            if p.is_file():
                import base64
                mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".png": "image/png", ".gif": "image/gif",
                        ".webp": "image/webp"}.get(p.suffix.lower())
                if mime:
                    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                    src = f"data:{mime};base64,{b64}"
        except Exception as e:
            log.debug("内联图片失败，导出里该图留空：%s", e)
            pass
    if t == "image":
        return f'<img src="{html.escape(src)}" loading="lazy" alt="图片">'
    if t == "voice":
        return f'<audio controls preload="none" src="{html.escape(src)}"></audio>'
    if t == "video":
        return f'<video controls preload="none" src="{html.escape(src)}"></video>'
    return f'<a href="{html.escape(src)}" target="_blank">下载附件</a>'


def conv_to_html(conv: dict, inline_media: bool = False,
                 media_root: Path | None = None,
                 standalone_title: str | None = None) -> str:
    """单会话 → 自包含 HTML。"""
    title = standalone_title or conv.get("title") or conv.get("conv_id") or "会话"
    acct = conv.get("account") or "默认账号"
    msgs = conv.get("messages", [])
    n = conv.get("n_msg", len(msgs))
    first = _fmt_time(conv.get("start_ts") or (msgs[0].get("ts") if msgs else None))
    last = _fmt_time(conv.get("end_ts") or (msgs[-1].get("ts") if msgs else None))

    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{html.escape(title)} · 微信聊天记录</title>",
        f"<style>{_CSS}</style></head><body>",
        '<div class="doc">',
        '<div class="hd">',
        f"<h1>{html.escape(title)}</h1>",
        '<div class="meta">'
        f"<b>账号</b> {html.escape(acct)} &nbsp;·&nbsp; "
        f"<b>消息</b> {n} 条<br>"
        f"<b>时间跨度</b> {html.escape(first)} → {html.escape(last)}"
        "</div>",
        "</div>",
        '<div class="body">',
    ]

    last_day = None
    for m in msgs:
        d = _fmt_day(m.get("ts"))
        if d and d != last_day:
            parts.append(f'<div class="day"><span>{html.escape(d)}</span></div>')
            last_day = d

        t = m.get("type")
        if t == "system":
            parts.append(
                f'<div class="sys">{html.escape(_display(m))}</div>')
            continue

        mine = bool(m.get("is_self"))
        sender = "我" if mine else (m.get("sender") or "对方")
        media = _media_html(m, inline_media, media_root)
        content = (m.get("content") or "").strip()

        if t == "text" or not media:
            body = html.escape(content)
        else:
            label = TYPE_LABEL.get(t, t)
            body = (f'<span class="tp">[{label}]'
                    + (f" {html.escape(content)}" if content else "")
                    + "</span>")
        bubble = f'<div class="bub">{body}{media}</div>'

        parts.append(
            f'<div class="msg{" mine" if mine else ""}">'
            f'<div class="av" style="background:{_av_color(sender)}">'
            f'{html.escape(_initial(sender))}</div>'
            f'<div class="bd"><div class="nm">{html.escape(sender)}</div>'
            f'{bubble}</div></div>'
        )

    parts += [
        "</div>",
        '<div class="ft">由 WeChat Vault · 微信数据方舟 导出 · '
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}</div>",
        "</div>",
        f"<script>{_JS}</script>",
        "</body></html>",
    ]
    return "".join(parts)


def index_html(convs: list[dict], title: str = "聊天记录导出") -> str:
    """多会话 → 一个总览页，链接到各会话（用于打包导出）。"""
    rows = []
    for c in convs:
        acct = c.get("account") or "默认账号"
        t = c.get("title") or c.get("conv_id") or ""
        n = c.get("n_msg", len(c.get("messages", [])))
        rows.append(
            "<tr>"
            f"<td>{html.escape(acct)}</td>"
            f"<td>{html.escape(t)}</td>"
            f"<td class='n'>{n}</td>"
            f"<td>{html.escape(_fmt_time(c.get('end_ts')))}</td>"
            "</tr>")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{background:#f2f2f2;color:#191919;padding:26px 16px;
 font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
.doc{{max-width:820px;margin:0 auto;background:#fff;border-radius:12px;
 box-shadow:0 2px 18px rgba(0,0,0,.07);overflow:hidden}}
h1{{font-size:18px;padding:22px 26px;border-bottom:1px solid #ececec;font-weight:600}}
table{{width:100%;border-collapse:collapse}}
th,td{{text-align:left;padding:10px 26px;font-size:13.5px;border-bottom:1px solid #f2f2f2}}
th{{color:#8c8c8c;font-weight:500;font-size:12.5px;background:#fafafa}}
td.n{{color:#8c8c8c}}
.ft{{padding:15px 26px;font-size:11.5px;color:#a0a0a0}}
</style></head><body>
<div class="doc">
<h1>{html.escape(title)}</h1>
<table><thead><tr><th>账号</th><th>会话</th><th>消息数</th><th>最后消息</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<div class="ft">共 {len(convs)} 个会话 · 导出于 {time.strftime('%Y-%m-%d %H:%M:%S')}</div>
</div></body></html>"""
