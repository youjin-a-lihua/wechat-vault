"""NAS 上重建容器并验证。"""
import os
import paramiko

HOST = os.environ.get("WV_NAS_HOST", "192.0.2.10")   # 示例地址（RFC 5737）；设 WV_NAS_HOST 覆盖
USER = "AKI"
PWD = os.environ.get("WV_SUDO_PASS", "")
assert PWD, "请先设置 WV_SUDO_PASS 环境变量（NAS 的 sudo 口令）"
REMOTE_SRC = "/vol2/1000/docker/wechat-vault-src"
HOT = "/vol3/1000/wechat-vault"

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=20)
SUDO = f"echo '{PWD}' | sudo -S "


def run(cmd, timeout=1800, quiet=False):
    # 用 sudo sh -c 包住，解决 cd 是内建命令的问题
    full = f"{SUDO} sh -c {shq(cmd)}"
    _in, out, err = cli.exec_command(full, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    if not quiet:
        clean = "\n".join(l for l in e.splitlines()
                          if "password for" not in l and "sudo: " not in l)
        if clean.strip():
            print("  [err]", clean.strip()[:800])
    return o, e


def shq(s):
    return "'" + s.replace("'", "'\"'\"'") + "'"


def step(t):
    print(f"\n{'='*60}\n{t}\n{'='*60}")


step("迁移旧库数据到新结构")
# 旧结构的归档库在 store/ 下；新结构要求 vault.db 直接在 $ROOT
run(f"""
set -e
if [ -f {HOT}/store/vault.db ] && [ ! -f {HOT}/vault.db ]; then
  cp -a {HOT}/store/vault.db {HOT}/vault.db
  echo '已迁移 vault.db'
fi
if [ -f {HOT}/store/MANIFEST.json ] && [ ! -f {HOT}/MANIFEST.json ]; then
  cp -a {HOT}/store/MANIFEST.json {HOT}/MANIFEST.json
  echo '已迁移 MANIFEST.json'
fi
chown -R 1000:1001 {HOT}
ls -la {HOT}/ | head -20
""")
o, _ = run(f"ls -la {HOT}/")
print(o.strip()[:1200])

step("docker compose 构建")
o, _ = run(f"cd {REMOTE_SRC}/deploy && docker compose down --remove-orphans 2>&1 | tail -6")
print(o.strip()[:600])
o, _ = run(f"cd {REMOTE_SRC}/deploy && docker compose build --no-cache 2>&1 | tail -30")
print(o.strip()[-2500:])

step("启动容器")
o, _ = run(f"cd {REMOTE_SRC}/deploy && docker compose up -d 2>&1 | tail -15")
print(o.strip()[:800])

step("等待就绪")
import time
for i in range(30):
    o, _ = run("docker ps --filter name=wechat-vault --format '{{.Status}}'", quiet=True)
    s = o.strip()
    if "healthy" in s:
        print(f"  就绪: {s}")
        break
    if i % 5 == 0:
        print(f"  [{i*2}s] {s or '(暂无)'}")
    time.sleep(2)

step("容器状态 + 日志")
o, _ = run("docker ps -a --filter name=wechat-vault --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'")
print(o.strip())
o, _ = run("docker logs wechat-vault --tail 40 2>&1")
print(o.strip()[-2500:])

step("容器内挂载确认")
o, _ = run("docker inspect wechat-vault --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'")
print(o.strip())

step("API 验证")
o, _ = run("curl -s --max-time 10 http://127.0.0.1:8790/api/status", quiet=True)
print("status:", o.strip()[:500])
o, _ = run("curl -s --max-time 10 http://127.0.0.1:8790/api/accounts", quiet=True)
print("accounts:", o.strip()[:500])
o, _ = run("curl -s --max-time 10 http://127.0.0.1:8790/api/archive", quiet=True)
print("archive:", o.strip()[:500])

cli.close()
