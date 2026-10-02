"""本地端到端验证：多账号隔离 + 双轨 + 全部 API。
造 2 个账号 + 存档轨包，跑归档器，起服务，逐个接口核对后关闭。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

work = Path(tempfile.mkdtemp(prefix="wv_e2e_"))
data = work / "data"
inbox = data / "inbox"

# ── 造样本：两个账号，各含同名会话「张三」（验证隔离） ──
for acct, extra in [("AKI的微信", "甲号消息内容AAA"), ("小号2024", "乙号消息内容BBB")]:
    d = inbox / acct
    d.mkdir(parents=True, exist_ok=True)
    lines = ["张三"]  # 标题行
    for i in range(1, 6):
        lines.append(f"2026-09-0{i} 10:0{i}:00 张三")
        lines.append(f"{extra} 第{i}条")
    (d / "张三.txt").write_text("\n".join(lines), encoding="utf-8")
    # 另一个会话，只有甲号有
    if acct == "AKI的微信":
        (d / "工作群.txt").write_text(
            "工作群\n2026-09-01 09:00:00 老板\n今天开会\n", encoding="utf-8")

# ── 存档轨投放口：伪造微信备份包（含无扩展名 BAK_*） ──
raw = inbox / "raw"
raw.mkdir(parents=True, exist_ok=True)
(raw / "Backup.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
(raw / "BAK_0_TEXT").write_bytes(b"FAKE-TEXT-BAK" * 50)
(raw / "BAK_0_MEDIA").write_bytes(b"FAKE-MEDIA-BAK" * 80)

print(f"工作目录: {work}")

# ── 1. 跑可读轨归档器（排除 raw） ──
env = dict(os.environ)
r = subprocess.run(
    [PY, str(ROOT / "archiver" / "wv_archiver.py"), "scan",
     "--source", str(inbox), "--store", str(data),
     "--exclude-dir", "raw"],
    capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
print("=== 可读轨归档 ===")
print((r.stdout or "")[-1200:])
if r.returncode != 0:
    print("[stderr]", (r.stderr or "")[-1500:])
    raise SystemExit(1)

# ── 2. 跑存档轨归档器 ──
r2 = subprocess.run(
    [PY, str(ROOT / "archiver" / "wv_raw.py"), "ingest",
     "--source", str(raw), "--dest", str(data / "raw-snapshots"),
     "--manifest", str(data / "MANIFEST.json")],
    capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
print("=== 存档轨归档 ===")
print((r2.stdout or "")[-1200:])
if r2.returncode != 0:
    print("[stderr]", (r2.stderr or "")[-1500:])
    raise SystemExit(1)

# ── 3. 起服务（走 CLI --store，与容器 entrypoint 一致） ──
port = 8899
proc = subprocess.Popen(
    [PY, str(ROOT / "viewer" / "wv_server.py"),
     "--store", str(data), "--host", "127.0.0.1", "--port", str(port)],
    cwd=str(ROOT / "viewer"), env=dict(os.environ),
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    encoding="utf-8", errors="replace")

base = f"http://127.0.0.1:{port}"

def get(p, **params):
    if params:
        from urllib.parse import urlencode
        p = p + ("&" if "?" in p else "?") + urlencode(params)
    with urllib.request.urlopen(base + p, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))

ok = True
try:
    for _ in range(40):
        try:
            get("/api/status"); break
        except Exception:
            time.sleep(0.4)
    else:
        print("!! 服务未起来")
        raise SystemExit(1)

    st = get("/api/status")
    print("=== /api/status ===")
    print(json.dumps(st, ensure_ascii=False, indent=1))

    def check(label, cond, detail=""):
        global ok
        mark = "PASS" if cond else "FAIL"
        if not cond: ok = False
        print(f"[{mark}] {label} {detail}")

    check("会话数=3（张三x2 + 工作群）", st["conv_count"] == 3, f"实际 {st['conv_count']}")
    check("账号数=2", st.get("account_count") == 2, f"实际 {st.get('account_count')}")

    accts = get("/api/accounts")
    names = sorted(a["account"] for a in accts["items"])
    print("=== /api/accounts ===", json.dumps(accts, ensure_ascii=False)[:400])
    check("账号名正确", names == ["AKI的微信", "小号2024"], f"实际 {names}")

    arch = get("/api/archive")
    print("=== /api/archive ===", json.dumps(arch, ensure_ascii=False)[:500])
    check("存档轨已启用", arch["enabled"] is True)
    check("存档轨文件数=3", arch["file_count"] == 3, f"实际 {arch['file_count']}")
    check("存档轨有快照代次", len(arch["snapshots"]) >= 1)
    check("识别出 Backup.db", any("Backup.db" in s["kind"] for s in arch["snapshots"]))

    # 账号过滤
    c_all = get("/api/conversations", limit=100)
    c_a   = get("/api/conversations", account="AKI的微信", limit=100)
    c_b   = get("/api/conversations", account="小号2024", limit=100)
    check("全部会话=3", c_all["total"] == 3, f"实际 {c_all['total']}")
    check("甲号会话=2", c_a["total"] == 2, f"实际 {c_a['total']}")
    check("乙号会话=1", c_b["total"] == 1, f"实际 {c_b['total']}")
    check("甲号 conv_id 带账号前缀",
          all(x["conv_id"].startswith("AKI的微信::") for x in c_a["items"]),
          c_a["items"][0]["conv_id"] if c_a["items"] else "")

    # 同名会话隔离：两个「张三」conv_id 不同
    ids = [x["conv_id"] for x in c_all["items"] if x["title"] == "张三"]
    check("同名张三被隔离成 2 个 conv", len(set(ids)) == 2, f"{ids}")

    # 搜索按账号过滤
    s_all = get("/api/search", q="消息内容", limit=50)
    s_a   = get("/api/search", q="消息内容", **{"account": "AKI的微信"}, limit=50)
    print("=== /api/search (all) ===", s_all["count"], "| (甲)", s_a["count"])
    check("全库搜索命中 10 条", s_all["count"] == 10, f"实际 {s_all['count']}")
    check("甲号搜索命中 5 条", s_a["count"] == 5, f"实际 {s_a['count']}")

    # 统计按账号过滤
    # 口径：TXT 首行是会话标题，不计入消息。
    #   张三会话 = 5 条，两个账号各一份 → 10
    #   工作群（仅甲号）= 1 条
    #   全库合计 11；甲号 = 5 + 1 = 6
    st_all = get("/api/stats")
    st_a   = get("/api/stats", **{"account": "AKI的微信"})
    print("=== /api/stats ===", st_all["total"], "| 甲", st_a["total"])
    check("全局统计 11 条", st_all["total"] == 11, f"实际 {st_all['total']}")
    check("甲号统计 6 条", st_a["total"] == 6, f"实际 {st_a['total']}")

    # 首页与静态资源
    for p in ["/", "/app.js", "/app.css"]:
        with urllib.request.urlopen(base + p, timeout=10) as r:
            body = r.read()
        check(f"静态资源 {p}", r.status == 200 and len(body) > 100, f"{len(body)}B")

    with urllib.request.urlopen(base + "/", timeout=10) as r:
        html = r.read().decode("utf-8")
    check("首页含账号切换器", 'id="acctSwitcher"' in html)
    check("首页含归档按钮", 'id="btnArchive"' in html)

finally:
    proc.terminate()
    try:
        proc.wait(timeout=6)
    except Exception:
        proc.kill()

print()
print("=" * 50)
print("E2E 结果:", "全部通过" if ok else "有失败项")
print("临时目录:", work)
shutil.rmtree(work, ignore_errors=True)
sys.exit(0 if ok else 1)
