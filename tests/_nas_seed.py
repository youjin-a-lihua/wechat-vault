"""往 NAS 投放口放真实样本（可读轨 + 存档轨），跑通双轨链路，验证端到端。"""
import os
import io
import tarfile
import time
from pathlib import Path

import paramiko

HOST = os.environ.get("WV_NAS_HOST", "192.0.2.10")   # 示例地址（RFC 5737）；设 WV_NAS_HOST 覆盖
USER = "AKI"
PWD = os.environ.get("WV_SUDO_PASS", "")
assert PWD, "请先设置 WV_SUDO_PASS 环境变量（NAS 的 sudo 口令）"
HOT = "/vol3/1000/wechat-vault"

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=20)
SUDO = f"echo '{PWD}' | sudo -S "


def shq(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def run(cmd, timeout=600, quiet=False):
    _in, out, err = cli.exec_command(f"{SUDO} sh -c {shq(cmd)}", timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    if not quiet:
        clean = "\n".join(l for l in e.splitlines()
                          if "password for" not in l and "sudo: " not in l)
        if clean.strip():
            print("  [err]", clean.strip()[:500])
    return o


def step(t):
    print(f"\n{'='*60}\n{t}\n{'='*60}")


# ── 造样本 tar（两个账号 + 存档轨包） ──
files = {
    "AKI的微信/家人群.txt": "\n".join(
        ["家人群"] +
        [f"2026-09-0{i} 20:1{i}:00 {'妈妈' if i%2 else '我'}\n"
         f"{'记得吃饭' if i%2 else '好的我知道了'} 这是第{i}条家常" for i in range(1, 9)]
    ),
    "AKI的微信/老同学.txt": "\n".join(
        ["老同学"] +
        [f"2026-08-1{i} 12:0{i}:00 小李\n周末聚一下？消息编号第{i}"
         for i in range(1, 6)]
    ),
    "小号2024/家人群.txt": "\n".join(
        ["家人群"] +
        [f"2026-09-0{i} 21:0{i}:00 爸爸\n小号里同名群的消息 第{i}条" for i in range(1, 6)]
    ),
    "小号2024/客户-张总.csv": "会话名,时间,发送者,内容\n"
        + "\n".join(f"客户-张总,2026-09-1{i} 10:00:00,{'张总' if i%2 else '我'},"
                    f"合同细节确认第{i}项" for i in range(1, 6)),
}

step("1. 投放可读轨样本")
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tf:
    for name, content in files.items():
        b = content.encode("utf-8")
        ti = tarfile.TarInfo(name=f"readable/{name}")
        ti.size = len(b)
        ti.mtime = int(time.time())
        tf.addfile(ti, io.BytesIO(b))
buf.seek(0)

sftp = cli.open_sftp()
with sftp.file("/tmp/wv_samples.tar.gz", "wb") as fh:
    fh.write(buf.getvalue())
sftp.close()

o = run(f"mkdir -p {HOT}/inbox 2>/dev/null; "
        f"cd {HOT}/inbox && tar -xzf /tmp/wv_samples.tar.gz && "
        f"rm -rf {HOT}/inbox/readable && "
        f"mv /dev/null /dev/null 2>/dev/null; true")
# readable/ 下的是相对路径 → 解到 inbox 根，让 AKI的微信/ 成为第一级
o = run(f"rm -rf /tmp/wv_extract && mkdir -p /tmp/wv_extract && "
        f"tar -xzf /tmp/wv_samples.tar.gz -C /tmp/wv_extract && "
        f"ls -R /tmp/wv_extract")
print(o.strip()[:600])

o = run(f"cp -r /tmp/wv_extract/readable/* {HOT}/inbox/ 2>/dev/null; "
        f"chown -R 1000:1001 {HOT}/inbox && "
        f"find {HOT}/inbox -type f | head -20")
print(o.strip()[:900])

step("2. 投放存档轨样本（伪造微信备份包，含无扩展名 BAK_*）")
# 直接构造 + base64 写入，避免多文件 sftp 往返
import base64
payloads = {
    "Backup.db": b"SQLite format 3\x00" + b"WX-BACKUP-FAKE" * 200,
    "BAK_0_TEXT": b"WX-TEXT-BAK-" * 400,
    "BAK_0_MEDIA": b"WX-MEDIA-BAK-" * 800,
}
o = run(f"mkdir -p {HOT}/inbox/raw")
for name, data in payloads.items():
    b64 = base64.b64encode(data).decode()
    o = run(f"echo {shq(b64)} | base64 -d > {shq(HOT + '/inbox/raw/' + name)}")
o = run(f"chown -R 1000:1001 {HOT}/inbox/raw && ls -la {HOT}/inbox/raw/")
print(o.strip()[:700])

step("3. 触发归档（容器内执行，与定时任务同路径）")
o = run("docker exec wechat-vault sh -c '"
        "python /app/archiver/wv_archiver.py scan --source /data/inbox --store /data "
        "--exclude-dir raw -v 2>&1 | tail -20'", timeout=300)
print(o.strip()[:1500])

o = run("docker exec wechat-vault sh -c '"
        "python /app/archiver/wv_raw.py ingest --source /data/inbox/raw "
        "--dest /data/raw-snapshots --manifest /data/MANIFEST.json 2>&1 | tail -15'",
        timeout=300)
print(o.strip()[:1200])

step("4. 通知查看器重载")
o = run("docker exec wechat-vault sh -c '"
        "python -c \"import urllib.request;"
        "print(urllib.request.urlopen(\\\"http://127.0.0.1:8790/api/reload\\\","
        "data=b\\\"\\\",timeout=15).read().decode())\"'", timeout=120)
print(o.strip()[:600])

step("5. 冷备")
o = run("docker exec wechat-vault sh -c '"
        "python /app/archiver/wv_mirror.py --src /data --dst /data-cold "
        "--exclude logs 2>&1 | tail -8'", timeout=300)
print(o.strip()[:800])

step("6. 端到端验证")
for ep in ["/api/status", "/api/accounts", "/api/archive"]:
    o = run(f"curl -s --max-time 10 http://127.0.0.1:8790{ep}", quiet=True)
    print(f"{ep}: {o.strip()[:600]}")

o = run("curl -s --max-time 10 'http://127.0.0.1:8790/api/conversations?limit=50'", quiet=True)
import json
try:
    d = json.loads(o)
    print(f"\n会话数: {d['total']}")
    for it in d["items"]:
        print(f"  [{it['account']}] {it['title']}  {it['n_msg']} 条  {it.get('start_ts')}~{it.get('end_ts')}")
except Exception as ex:
    print("解析失败", ex, o[:300])

o = run("curl -s --max-time 10 'http://127.0.0.1:8790/api/search?q=%E5%AE%B6%E5%B8%B8&limit=20'", quiet=True)
try:
    d = json.loads(o)
    print(f"\n搜索「家常」命中: {d['count']}")
except Exception as ex:
    print("搜索解析失败", ex, o[:300])

o = run(f"ls -la {HOT}/raw-snapshots/*/ 2>/dev/null | head -12")
print("\n存档轨落盘:\n" + o.strip()[:700])

o = run(f"ls -la /vol00/HSH721414ALN6M0/wechat-vault-cold/ 2>/dev/null | head -14")
print("\n冷备:\n" + o.strip()[:800])

cli.close()
