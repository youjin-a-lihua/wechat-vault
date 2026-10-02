#!/bin/sh
# ============================================================
#  WeChat Vault · 手动归档（在 NAS 宿主机上执行）
#  用途：不想等到 03:00，想立刻把收件箱里的新导出归档 + 冷备
#  用法：sudo sh /vol3/1000/wechat-vault/deploy/scan-now.sh
#
#  NAS 上的 deploy/ 目录里如果已有旧版脚本，请用本文件覆盖。
# ============================================================
set -e
ROOT=/vol3/1000/wechat-vault
COLD=/vol00/HSH721414ALN6M0/wechat-vault-cold

# 以 AKI(1000:1001) 身份在容器内跑，保证文件属主一致
echo "== 1/4 可读轨归档（inbox → vault.db）=="
docker exec -u 1000:1001 wechat-vault \
  python /app/archiver/wv_archiver.py scan --source /data/inbox --store /data

echo "== 2/4 存档轨归档（inbox/raw → raw-snapshots）=="
docker exec -u 1000:1001 wechat-vault \
  python /app/archiver/wv_raw.py ingest \
  --source /data/inbox/raw --dest /data/raw-snapshots --manifest /data/MANIFEST.json

echo "== 3/4 通知查看器重载 =="
curl -s -X POST http://127.0.0.1:8790/api/reload && echo

echo "== 4/4 冷备镜像（热侧 → HC620，排除 logs）=="
docker exec -u 1000:1001 wechat-vault \
  python /app/archiver/wv_mirror.py --src /data --dst /data-cold --exclude logs

echo "== 完成 =="
# 打 /healthz（公开端点）。/api/status 在开启访问密码后需登录，会返回 401。
curl -s http://127.0.0.1:8790/healthz && echo
