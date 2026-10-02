"""把本地新版 wechat-vault 推到 NAS，重组目录结构，重建容器。

策略：
  1. 上传全部源码到 /vol2/1000/docker/wechat-vault-src（构建上下文，放在 SSD）
  2. 在 /vol3/1000/wechat-vault 下建立设计文档规定的目录结构
     （保留已有的 vault.db / MANIFEST.json，不销毁数据）
  3. docker compose build + up -d
  4. 打印结果
"""
import os
import io
import posixpath
import sys
import tarfile
import time
from pathlib import Path

import paramiko

HOST = os.environ.get("WV_NAS_HOST", "192.0.2.10")   # 示例地址（RFC 5737）；设 WV_NAS_HOST 覆盖
USER = "AKI"
PWD = os.environ.get("WV_SUDO_PASS", "")
assert PWD, "请先设置 WV_SUDO_PASS 环境变量（NAS 的 sudo 口令）"

SRC = Path(__file__).parent / "wechat-vault"
REMOTE_SRC = "/vol2/1000/docker/wechat-vault-src"
HOT = "/vol3/1000/wechat-vault"
COLD = "/vol00/HSH721414ALN6M0/wechat-vault-cold"

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=20)
SUDO = f"echo '{PWD}' | sudo -S "


def run(cmd, sudo=True, timeout=600, quiet=False):
    full = (SUDO + cmd) if sudo else cmd
    _in, out, err = cli.exec_command(full, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    if not quiet:
        clean = "\n".join(l for l in e.splitlines() if "password for" not in l)
        if clean.strip():
            print("  [err]", clean.strip()[:600])
    return o, e


def step(t):
    print(f"\n{'='*60}\n{t}\n{'='*60}")


# ── 1. 打包本地源码（排除缓存/样本/本地库） ──
step("1. 打包本地源码")
EXCLUDE_DIRS = {"__pycache__", ".git", "samples", "store", "venv", ".venv"}
files = []
for p in SRC.rglob("*"):
    if not p.is_file():
        continue
    if any(part in EXCLUDE_DIRS for part in p.relative_to(SRC).parts):
        continue
    files.append(p)
print(f"  待上传文件: {len(files)}")
for f in sorted(files):
    print("   ", f.relative_to(SRC))

buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w:gz") as tf:
    for f in files:
        tf.add(f, arcname=str(f.relative_to(SRC)).replace("\\", "/"))
buf.seek(0)
print(f"  包大小: {len(buf.getvalue())/1024:.1f} KB")

# ── 2. 建目录 + 解包 ──
step("2. 上传代码到 NAS")
run(f"mkdir -p {REMOTE_SRC} && chown -R 1000:1001 {REMOTE_SRC}")
sftp = cli.open_sftp()
with sftp.file("/tmp/wv_src.tar.gz", "wb") as fh:
    fh.write(buf.getvalue())
sftp.close()
o, _ = run(f"tar -xzf /tmp/wv_src.tar.gz -C {REMOTE_SRC} && "
           f"chown -R 1000:1001 {REMOTE_SRC} && ls {REMOTE_SRC}")
print(o.strip()[:900])

# ── 3. 重组热侧目录（设计文档规定结构） ──
step("3. 重组热侧目录结构")
# 先把旧结构里散落的东西归位（若存在）
run(f"""
mkdir -p {HOT}/inbox/raw {HOT}/media {HOT}/raw-snapshots {HOT}/logs
# 旧结构若把 inbox 直挂 /data/inbox，这里可能是别的布局 —— 仅补齐缺失目录，不动数据
chown -R 1000:1001 {HOT}/inbox {HOT}/media {HOT}/raw-snapshots {HOT}/logs 2>/dev/null || true
""")
o, _ = run(f"ls -la {HOT}/ && echo '--- inbox ---' && ls -la {HOT}/inbox/")
print(o.strip()[:1200])

# ── 4. 冷侧补齐 ──
step("4. 补齐冷侧目录")
run(f"mkdir -p {COLD} && chown -R 1000:1001 {COLD}")

# ── 5. 重建容器 ──
step("5. 重建容器")
o, _ = run(f"cd {REMOTE_SRC}/deploy && docker compose down --remove-orphans 2>&1 | tail -5", timeout=300)
print(o.strip()[:500])
o, _ = run(f"cd {REMOTE_SRC}/deploy && docker compose build 2>&1 | tail -25", timeout=1800)
print(o.strip()[-2000:])

cli.close()
print("\n构建阶段结束。")
