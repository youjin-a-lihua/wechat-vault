#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeChat Vault - 最终全链路验收（一次性、端到端）
==============================================

覆盖：
  A. 解析器 4 格式自测
  B. .dat 解码器自测
  C. 归档器：全新库全量扫描 + 增量幂等 + 完整性校验
  D. store 模式后端：启动耗时 + 全部 API 契约（TestClient 真实 HTTP）
  E. 前端静态资源：index.html / app.js / app.css 可访问 + 关键 UI 元素存在
  F. 数据一致性自审：缓存 vs 实际、孤儿检测、FTS 对应、消息数守恒

所有路径使用 pathlib + 显式 Windows/Temp 目录，避免 /tmp 语义歧义。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent
PY = sys.executable

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail else ""))
    return ok


def run(mod_path: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, str(mod_path), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(PROJ),
    )


def section(t: str) -> None:
    print(f"\n{'=' * 72}\n{t}\n{'=' * 72}")


# ---------------------------------------------------------------------------
# A. 解析器
# ---------------------------------------------------------------------------
section("A. 解析器（parser/wv_parser.py）")
# 自测：无参数调用即触发 _selftest()
r = run(PROJ / "parser" / "wv_parser.py", [])
out_a = r.stdout + r.stderr
check("解析器自测通过", r.returncode == 0 and "4/4" in out_a,
      (out_a.strip().splitlines() or [""])[-1][:90])

# 实盘解析 samples 目录（路径直接作为位置参数，输出为每个会话的 stats JSON）
r = run(PROJ / "parser" / "wv_parser.py", [str(PROJ / "samples")])
out_a2 = r.stdout + r.stderr
n_stats = out_a2.count('"top_senders"')   # 每个会话 stats 块各含一个
check("samples 目录解析成功", r.returncode == 0 and n_stats >= 5,
      f"{n_stats} 个会话 stats 块")

# ---------------------------------------------------------------------------
# B. .dat 解码器
# ---------------------------------------------------------------------------
section("B. .dat 解码器（archiver/wv_dat.py）")
r = run(PROJ / "archiver" / "wv_dat.py", ["selftest"])
check(".dat 解码器自测通过", r.returncode == 0 and "5/5" in (r.stdout + r.stderr),
      (r.stdout.strip().splitlines() or [""])[-1][:90])

# ---------------------------------------------------------------------------
# C. 归档器（全新库 + 增量 + 校验）
# ---------------------------------------------------------------------------
section("C. 归档器（archiver/wv_archiver.py）")
# 用独立临时目录，避免污染 samples/store
tmp_root = Path(tempfile.gettempdir()) / "wv_acceptance"
if tmp_root.exists():
    shutil.rmtree(tmp_root, ignore_errors=True)
fresh_store = tmp_root / "store"
fresh_store.mkdir(parents=True, exist_ok=True)

t0 = time.time()
r = run(PROJ / "archiver" / "wv_archiver.py",
        ["scan", "--source", str(PROJ / "samples"), "--store", str(fresh_store)])
scan_out = r.stdout + r.stderr
import re as _re
first_added = 0
for line in scan_out.splitlines():
    if "新增" in line or "added" in line.lower():
        m = _re.search(r"(\d+)", line)
        if m:
            first_added = max(first_added, int(m.group(1)))
check("首次全量扫描成功", r.returncode == 0,
      f"耗时 {time.time()-t0:.2f}s；{(scan_out.strip().splitlines() or [''])[-1][:70]}")

db = fresh_store / "vault.db"
check("vault.db 已生成", db.exists(), f"{db.stat().st_size if db.exists() else 0} bytes")

# 直接查库核对消息条数
n_msg = 0
if db.exists():
    conn = sqlite3.connect(str(db))
    try:
        n_msg = conn.execute("SELECT COUNT(*) FROM msg").fetchone()[0]
    except Exception as e:
        check("msg 表可查询", False, str(e)[:80])
    conn.close()
check("首次入库消息数 >= 2000", n_msg >= 2000, f"{n_msg} 条")

# 增量幂等：再跑一次应当 0 新增
r = run(PROJ / "archiver" / "wv_archiver.py",
        ["scan", "--source", str(PROJ / "samples"), "--store", str(fresh_store)])
out2 = r.stdout + r.stderr
n_msg2 = n_msg
if db.exists():
    try:
        conn = sqlite3.connect(str(db))
        n_msg2 = conn.execute("SELECT COUNT(*) FROM msg").fetchone()[0]
        conn.close()
    except sqlite3.OperationalError as e:
        check("msg 表存在", False, str(e)[:80])
check("增量幂等（重复扫描不重复入库）", n_msg2 == n_msg and n_msg > 0,
      f"{n_msg} -> {n_msg2}")

# 完整性校验
r = run(PROJ / "archiver" / "wv_archiver.py", ["verify", "--store", str(fresh_store)])
vout = r.stdout + r.stderr
check("归档库完整性校验通过", r.returncode == 0 and "失败" not in vout,
      [l for l in vout.splitlines() if "通过" in l or "OK" in l or "校验" in l][:1] and
      [l for l in vout.splitlines() if "通过" in l or "OK" in l or "校验" in l][0][:80] or "")

# ---------------------------------------------------------------------------
# D. 后端 store 模式 + 全部 API（真实 HTTP）
# ---------------------------------------------------------------------------
section("D. 后端 API（viewer/wv_server.py，store 模式，真实 HTTP）")
sys.path.insert(0, str(PROJ / "viewer"))
sys.path.insert(0, str(PROJ / "parser"))

os.environ["WV_STORE"] = str(fresh_store)
try:
    import wv_server as S
except Exception as e:
    check("后端模块可导入", False, str(e)[:120])
    S = None

if S:
    check("后端模块可导入", True)
    t0 = time.time()
    try:
        # 直接调用 load_store 并计时（生产模式）—— 注意必须传 Path
        S.load_store(fresh_store)
        load_t = time.time() - t0
        check("store 模式加载 < 1s", load_t < 1.0, f"{load_t*1000:.0f} ms")
    except Exception as e:
        check("store 模式加载 < 1s", False, f"{type(e).__name__}: {e}"[:100])

    try:
        from fastapi.testclient import TestClient
        c = TestClient(S.app)

        r = c.get("/api/status")
        ok = r.status_code == 200 and isinstance(r.json(), dict)
        js = r.json() if ok else {}
        check("GET /api/status", ok,
              f"mode={js.get('mode')} conv={js.get('conv_count')} msg={js.get('msg_count')}")

        r = c.get("/api/conversations")
        body = r.json()
        convs = body.get("conversations", body.get("items", body)) if isinstance(body, dict) else body
        check("GET /api/conversations", r.status_code == 200 and len(convs) >= 5,
              f"{len(convs)} 个会话")
        first_conv = convs[0].get("conv_id") if convs else None

        if first_conv:
            r = c.get(f"/api/messages?conv_id={first_conv}&limit=20")
            body = r.json()
            msgs = body.get("items", body.get("messages", body)) if isinstance(body, dict) else body
            check("GET /api/messages", r.status_code == 200 and len(msgs) > 0,
                  f"{len(msgs)} 条（total={body.get('total') if isinstance(body,dict) else '?'}）")

        # 搜索：中文全文检索
        q = "今天"
        r = c.get(f"/api/search?q={q}")
        body = r.json()
        hits = body.get("hits", body.get("results", body.get("items", []))) if isinstance(body, dict) else body
        check("GET /api/search（中文全文检索）", r.status_code == 200,
              f"q={q} -> {len(hits)} 命中")

        r = c.get("/api/stats")
        check("GET /api/stats", r.status_code == 200 and isinstance(r.json(), dict))

        r = c.get("/")
        html = r.text
        check("GET / （页面）", r.status_code == 200 and "<html" in html.lower(),
              f"{len(html)} chars")

        r = c.get("/app.js")
        check("GET /app.js", r.status_code == 200 and len(r.text) > 5000,
              f"{len(r.text)} chars")

        r = c.get("/app.css")
        check("GET /app.css", r.status_code == 200 and len(r.text) > 3000,
              f"{len(r.text)} chars")

        # E. 前端关键 UI 元素
        section("E. 前端关键 UI 元素（微备份对标：会话列表 + 聊天气泡）")
        for elem, desc in [
            ("convList", "左侧会话列表"),
            ("msgList", "右侧消息气泡区"),
            ("searchPanel", "全局搜索面板"),
            ("statsPanel", "统计面板"),
        ]:
            hit = elem in html or (elem in (c.get("/app.js").text))
            check(f"UI 元素 #{elem}（{desc}）", hit)

        js_text = c.get("/app.js").text
        css_text = c.get("/app.css").text
        check("微信气泡样式（CSS bubble）", "bubble" in css_text or "bubble" in js_text)
        check("微信官方绿 #95ec69", "95ec69" in css_text.lower())
        check("主题切换逻辑", "theme" in js_text.lower())

    except Exception as e:
        check("TestClient 全链路", False, f"{type(e).__name__}: {e}"[:150])

# ---------------------------------------------------------------------------
# E2. 媒体解码链路（.dat → 可读图片 → HTTP 服务）
# ---------------------------------------------------------------------------
section("E2. 媒体解码链路（.dat → 可读图片 → HTTP）")
media_work = tmp_root / "media"
media_inbox = media_work / "inbox"; media_inbox.mkdir(parents=True, exist_ok=True)
media_store = media_work / "store"; media_store.mkdir(parents=True, exist_ok=True)

# 造一个 XOR 加密 PNG（明文 1x1 红点）
_png = bytes.fromhex(
    "89504E470D0A1A0A0000000D4948445200000001000000010806000000"
    "1F15C4890000000D4944415478DA63F8CFC0F01F00050001FF9E7C3A5F"
    "0000000049454E44AE426082")
_dat = bytes(b ^ 0x5A for b in _png)

sys.path.insert(0, str(PROJ / "archiver"))
try:
    import wv_dat as D
    r = D.decode_dat(_dat)
    check(".dat 解码还原一致", r["ok"] and r["data"] == _png,
          f"fmt={r['fmt']} ext={r['ext']}")
except Exception as e:
    check(".dat 解码还原一致", False, f"{type(e).__name__}: {e}"[:80])

(media_inbox / "pic1.dat").write_bytes(_dat)
(media_inbox / "媒体会话.csv").write_text(
    "CreateTime,Sender,Type,Content,MediaPath\n"
    "2026-06-01 10:00:00,张三,text,看看这张图,\n"
    "2026-06-01 10:00:05,张三,image,[图片],pic1.dat\n"
    "2026-06-01 10:00:10,我,text,收到了,\n", encoding="utf-8")

r = run(PROJ / "archiver" / "wv_archiver.py",
        ["scan", "--source", str(media_inbox), "--store", str(media_store)])
check("含媒体消息归档成功", r.returncode == 0)

mdb = media_store / "vault.db"
media_rel = None
if mdb.exists():
    conn = sqlite3.connect(str(mdb)); conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT media FROM msg WHERE type='image'").fetchone()
    media_rel = row["media"] if row else None
    conn.close()
check("image 消息 media 字段已填 /media/ 路径",
      bool(media_rel) and media_rel.startswith("/media/"), f"{media_rel!r}")

dec_files = list((media_store / "media").glob("*")) if (media_store / "media").exists() else []
check("解码文件已落盘且与原图一致",
      bool(dec_files) and dec_files[0].read_bytes() == _png,
      f"{[f.name for f in dec_files]}")

# 通过 HTTP 拉取解码后的媒体
try:
    S.load_store(media_store)
    from fastapi.testclient import TestClient
    c2 = TestClient(S.app)
    r = c2.get(media_rel)
    check("GET /media/... 返回正确图片",
          r.status_code == 200 and r.content == _png,
          f"{r.status_code} {len(r.content)}B")
    # 路径穿越防护
    r = c2.get("/media/../../vault.db")
    check("媒体路径穿越被拦截", r.status_code in (403, 404), f"HTTP {r.status_code}")
except Exception as e:
    check("GET /media/... 返回正确图片", False, f"{type(e).__name__}: {e}"[:80])


# ---------------------------------------------------------------------------
# F. 数据一致性自审
# ---------------------------------------------------------------------------section("F. 数据一致性自审")
if db.exists():
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    total_msg = conn.execute("SELECT COUNT(*) FROM msg").fetchone()[0]
    total_conv = conn.execute("SELECT COUNT(*) FROM conv").fetchone()[0]
    fts_n = conn.execute("SELECT COUNT(*) FROM msg_fts").fetchone()[0]

    # 孤儿：msg.conv_id 不在 conv 中
    orphan = conn.execute(
        "SELECT COUNT(*) FROM msg WHERE conv_id NOT IN (SELECT conv_id FROM conv)"
    ).fetchone()[0]
    check("无孤儿消息（conv_id 全部有效）", orphan == 0, f"{orphan} 条孤儿")

    # FTS 与「可索引消息」一一对应（设计：仅 text/link/system/other 且内容非空）
    indexable = conn.execute(
        "SELECT COUNT(*) FROM msg WHERE type IN ('text','link','system','other') "
        "AND content IS NOT NULL AND content<>''"
    ).fetchone()[0]
    check("FTS 索引 == 可索引消息数", fts_n == indexable,
          f"fts={fts_n} indexable={indexable}（另 {total_msg-fts_n} 条为图片/语音等无文本消息，设计不索引）")
    # 无孤立 FTS 记录
    orphan_fts = conn.execute(
        "SELECT COUNT(*) FROM msg_fts f WHERE f.msg_id NOT IN (SELECT msg_id FROM msg)"
    ).fetchone()[0]
    check("无孤立 FTS 记录", orphan_fts == 0, f"{orphan_fts} 条")
    # 无遗漏：可索引消息全部已建索引
    missed = conn.execute(
        "SELECT COUNT(*) FROM msg m WHERE m.type IN ('text','link','system','other') "
        "AND m.content IS NOT NULL AND m.content<>'' "
        "AND m.msg_id NOT IN (SELECT msg_id FROM msg_fts)"
    ).fetchone()[0]
    check("可索引消息无遗漏", missed == 0, f"{missed} 条漏建")

    # 每个会话的消息数之和 == 总数
    s = conn.execute("SELECT COALESCE(SUM(n_msg),0) FROM conv").fetchone()[0]
    check("会话 n_msg 之和 == 消息总数", s == total_msg, f"sum={s} total={total_msg}")

    # 消息时间单调性（同会话内）
    bad_order = 0
    for row in conn.execute("SELECT DISTINCT conv_id FROM msg").fetchall():
        cid = row[0]
        ts_list = [x[0] for x in conn.execute(
            "SELECT ts FROM msg WHERE conv_id=? ORDER BY seq", (cid,)).fetchall()]
        for a, b in zip(ts_list, ts_list[1:]):
            if a and b and b < a - 86400:  # 容忍跨天乱序 1 天
                bad_order += 1
    check("会话内时间基本有序", bad_order == 0, f"{bad_order} 处异常")

    # MANIFEST 一致性
    mf = fresh_store / "MANIFEST.json"
    if mf.exists():
        man = json.loads(mf.read_text(encoding="utf-8"))
        check("MANIFEST.json 已生成", True,
              f"keys={list(man.keys())[:5]}")
    else:
        check("MANIFEST.json 已生成", False)

    print(f"\n  摘要：{total_conv} 会话 / {total_msg} 消息 / {fts_n} FTS 条目")
    conn.close()

# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------
section("验收结果")
print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("\n  失败项：")
    for f in FAIL:
        print(f"    - {f}")
    sys.exit(1)
else:
    print("\n  全部通过 -- 百分百可交付")
    sys.exit(0)
