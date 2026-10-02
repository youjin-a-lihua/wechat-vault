#!/bin/sh
# ============================================================
#  WeChat Vault · 清空全部归档数据（危险操作，需二次确认）
#
#  用途：把收件箱 / 热库 / 存档轨 / 冷备 / 日志全部清空，回到全新状态。
#        用于清除测试数据、或要彻底重新归档。
#
#  用法：sudo sh /vol3/1000/wechat-vault/deploy/purge-all.sh
#        加 --yes 跳过交互确认（供脚本调用）
# ============================================================
set -e
ROOT=/vol3/1000/wechat-vault
COLD=/vol00/HSH721414ALN6M0/wechat-vault-cold

if [ "$1" != "--yes" ]; then
    echo "=========================================="
    echo "  ⚠️  即将清空以下全部数据："
    echo "    投放口:   $ROOT/inbox"
    echo "    热  库:   $ROOT/vault.db + MANIFEST.json"
    echo "    存档轨:   $ROOT/raw-snapshots"
    echo "    媒  体:   $ROOT/media"
    echo "    日  志:   $ROOT/logs"
    echo "    冷  备:   $COLD"
    echo "=========================================="
    printf "确认要清空吗？输入 yes 继续："
    read -r ans
    if [ "$ans" != "yes" ]; then
        echo "已取消。"
        exit 0
    fi
fi

echo "[1/3] 停容器（避免文件占用与启动重扫）…"
cd "$ROOT/deploy" && docker compose down

echo "[2/3] 清空数据…"
# 只删内容，保留目录本身，避免卷挂载点被删掉
rm -rf "$ROOT"/inbox/* "$ROOT"/raw-snapshots/* "$ROOT"/media/* "$ROOT"/logs/* 2>/dev/null || true
rm -f "$ROOT"/vault.db "$ROOT"/vault.db-wal "$ROOT"/vault.db-shm "$ROOT"/MANIFEST.json 2>/dev/null || true
rm -rf "$COLD"/* 2>/dev/null || true

echo "[3/3] 重启容器（自动建空库）…"
cd "$ROOT/deploy" && docker compose up -d
sleep 8

echo "== 完成 =="
# 打 /healthz（公开端点）。/api/status 在开启访问密码后需登录，会返回 401。
curl -s http://127.0.0.1:8790/healthz && echo
