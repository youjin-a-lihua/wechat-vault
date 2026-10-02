"""验证旧库（无 account 列）能被自动迁移，不丢数据。"""
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

work = Path(tempfile.mkdtemp(prefix="wv_old_"))
data = work / "data"
data.mkdir(parents=True)
db = data / "vault.db"
empty_inbox = work / "empty_inbox"
empty_inbox.mkdir(parents=True, exist_ok=True)

# ── 手工造一个「旧版 schema」库：没有 account 列，msg_fts 也没有 ──
con = sqlite3.connect(str(db))
con.executescript("""
CREATE TABLE conv (
    conv_id TEXT PRIMARY KEY, title TEXT, is_group INTEGER DEFAULT 0,
    members TEXT DEFAULT '[]', n_msg INTEGER DEFAULT 0,
    start_ts INTEGER, end_ts INTEGER, sources TEXT DEFAULT '[]',
    created_at INTEGER, updated_at INTEGER
);
CREATE TABLE msg (
    msg_id TEXT PRIMARY KEY, conv_id TEXT NOT NULL, seq INTEGER,
    ts INTEGER, time_str TEXT, sender TEXT, is_self INTEGER DEFAULT 0,
    type TEXT, content TEXT, media TEXT, source_file TEXT, created_at INTEGER
);
CREATE TABLE source_file (path TEXT PRIMARY KEY, sha256 TEXT, size INTEGER,
    mtime REAL, conv_id TEXT, n_msg INTEGER DEFAULT 0, fmt TEXT,
    archived_at INTEGER, updated_at INTEGER);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE archive_log (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT,
    detail TEXT, n_ok INTEGER DEFAULT 0, n_skip INTEGER DEFAULT 0,
    n_fail INTEGER DEFAULT 0, created_at INTEGER);
CREATE VIRTUAL TABLE msg_fts USING fts5(content, sender,
    conv_id UNINDEXED, msg_id UNINDEXED, tokenize='trigram');
""")
con.execute("INSERT INTO conv (conv_id,title,n_msg,start_ts,end_ts) VALUES (?,?,?,?,?)",
            ("老会话", "老会话", 2, 1788000000, 1788000100))
con.execute("INSERT INTO msg (msg_id,conv_id,ts,sender,content,type) VALUES (?,?,?,?,?,?)",
            ("m1", "老会话", 1788000000, "老王", "这是旧库里的第一条消息", "text"))
con.execute("INSERT INTO msg (msg_id,conv_id,ts,sender,content,type) VALUES (?,?,?,?,?,?)",
            ("m2", "老会话", 1788000100, "我", "这是旧库里的第二条消息", "text"))
con.execute("INSERT INTO msg_fts (content,sender,conv_id,msg_id) VALUES (?,?,?,?)",
            ("这是旧库里的第一条消息", "老王", "老会话", "m1"))
con.commit()
con.close()
print(f"旧库已造好: {db}")

# ── 跑归档器（应触发迁移） ──
r = subprocess.run([PY, str(ROOT / "archiver" / "wv_archiver.py"), "scan",
                    "--source", str(empty_inbox),
                    "--store", str(data)],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
print("--- 归档器输出 ---")
print((r.stdout or "")[-900:])
if r.returncode != 0:
    print("[stderr]", (r.stderr or "")[-1200:])

# ── 核对 ──
con = sqlite3.connect(str(db))
con.row_factory = sqlite3.Row
ok = True

def check(label, cond, detail=""):
    global ok
    if not cond: ok = False
    print(f"[{'PASS' if cond else 'FAIL'}] {label} {detail}")

conv_cols = {r[1] for r in con.execute("PRAGMA table_info(conv)")}
msg_cols  = {r[1] for r in con.execute("PRAGMA table_info(msg)")}
fts_cols  = {r[1] for r in con.execute("PRAGMA table_info(msg_fts)")}
check("conv 补上 account", "account" in conv_cols, str(sorted(conv_cols)))
check("msg 补上 account", "account" in msg_cols)
check("msg_fts 补上 account", "account" in fts_cols, str(sorted(fts_cols)))

n_msg = con.execute("SELECT COUNT(*) FROM msg").fetchone()[0]
check("旧消息未丢失（2 条）", n_msg == 2, f"实际 {n_msg}")

acct = con.execute("SELECT account FROM conv WHERE conv_id='老会话'").fetchone()[0]
check("旧会话回填了默认账号", acct == "默认账号", repr(acct))

n_fts = con.execute("SELECT COUNT(*) FROM msg_fts").fetchone()[0]
check("FTS 已重建且回填（2 条）", n_fts == 2, f"实际 {n_fts}")

hit = con.execute("SELECT COUNT(*) FROM msg_fts WHERE msg_fts MATCH ?",
                  ("第一条",)).fetchone()[0]
check("FTS 可搜（trigram 命中）", hit >= 1, f"命中 {hit}")

# 再跑一次，确认幂等
r2 = subprocess.run([PY, str(ROOT / "archiver" / "wv_archiver.py"), "scan",
                     "--source", str(empty_inbox),
                     "--store", str(data)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
check("二次运行不报错（幂等）", r2.returncode == 0, (r2.stderr or "")[-300:])
n2 = con.execute("SELECT COUNT(*) FROM msg").fetchone()[0]
check("二次运行后消息数不变", n2 == 2, f"实际 {n2}")
con.close()

print()
print("=" * 46)
print("旧库迁移:", "全部通过" if ok else "有失败项")
import shutil
shutil.rmtree(work, ignore_errors=True)
sys.exit(0 if ok else 1)
