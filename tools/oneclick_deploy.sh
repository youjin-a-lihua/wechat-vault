#!/usr/bin/env bash
# oneclick_deploy.sh —— 一条命令完成「环境检查 → 密钥材料装载 → 容器启动 → 健康验收」
#
# 面向人类与 AI Agent 共用：全部失败项以非零码退出，Agent 可直接据退出码判定成败。
#
# 用法：
#   bash tools/oneclick_deploy.sh                 # 本机 docker compose 部署
#   WV_SKIP_BUILD=1 bash tools/oneclick_deploy.sh # 已有镜像，跳过构建
#
# 环境变量（也可直接改下面的默认值）：
#   WV_NAS_HOST   面板内网 IP（默认取 deploy/.env 里的 WV_BIND_IP）
#   WV_STORE      数据目录绝对路径（默认取 deploy/.env 里的 WV_STORE）
#   WV_SKIP_BUILD 置 1 跳过镜像构建
#
# 红线（脚本强制检查）：
#   · 绝不开公网 —— WV_BIND_IP 必须是私网地址
#   · 密钥材料只从本地路径读，绝不打印内容
set -euo pipefail

say()  { printf '\033[1;36m[oneclick]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[  OK   ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[ WARN  ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[ FAIL  ]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 0. 定位仓库与 deploy 目录 ─────────────────────────────
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY="$ROOT/deploy"
[ -f "$DEPLOY/docker-compose.single.yml" ] || die "找不到 $DEPLOY/docker-compose.single.yml"
[ -d "$DEPLOY" ] || die "找不到 deploy 目录"
say "仓库根：$ROOT"

# ── 1. 前置工具检查 ───────────────────────────────────────
for bin in docker; do
    command -v "$bin" >/dev/null 2>&1 || die "缺少必需命令：$bin"
done
if command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
elif docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
else
    die "缺少 docker compose 插件"
fi
ok "docker / compose 就绪"

# ── 2. 读 deploy/.env，校验必填项与红线 ───────────────────
ENVF="$DEPLOY/.env"
if [ ! -f "$ENVF" ]; then
    if [ -f "$ROOT/env.example" ]; then
        warn "deploy/.env 不存在，从 env.example 复制模板（请填入真实值后重跑）"
        cp "$ROOT/env.example" "$ENVF"
        die "已生成 $ENVF —— 填好 WV_BIND_IP / WV_PASSWORD / WV_STORE 后重跑"
    fi
    die "缺少 $ENVF，且找不到 env.example"
fi

env_get() { grep -E "^$1=" "$ENVF" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true; }

BIND_IP="$(env_get WV_BIND_IP)"
STORE="${WV_STORE:-$(env_get WV_STORE)}"
NAS_HOST="${WV_NAS_HOST:-$BIND_IP}"
PORT="${WV_PORT:-8790}"

[ -n "$BIND_IP" ] || die "WV_BIND_IP 未设置（必填）"
[ -n "$STORE" ]   || die "WV_STORE 未设置（必填，必须绝对路径）"

# 红线：绝不开公网
case "$BIND_IP" in
    10.*|192.168.*|127.*|localhost) : ;;
    172.1[6-9].*|172.2[0-9].*|172.3[0-1].*) : ;;
    *) die "WV_BIND_IP=$BIND_IP 不是私网地址 —— 红线「绝不开公网」，拒绝部署" ;;
esac
ok "WV_BIND_IP=$BIND_IP（私网）  WV_STORE=$STORE"

# ── 3. 数据目录就位 ───────────────────────────────────────
mkdir -p "$STORE/wx_backup" "$STORE/media" "$STORE/logs" "$STORE/inbox"
ok "数据目录就绪：$STORE"

# 密钥材料检查（只查存在性，绝不打印内容）
if [ -f "$STORE/wx_backup/passphrase.json" ]; then
    ok "已找到 passphrase.json（账号级，推荐）"
elif [ -f "$STORE/wx_backup/all_keys.json" ]; then
    warn "只有 all_keys.json（库快照，库重建后会失效）—— 建议改用 passphrase.json"
else
    warn "未找到密钥材料（$STORE/wx_backup/）—— 部署可以继续，但解密会失败"
    warn "  取钥方式见 README「首次使用：密钥从哪来」，或跑 plugins/wv-keymate/"
fi

# ── 4. 构建镜像（可跳过）────────────────────────────────────
if [ "${WV_SKIP_BUILD:-0}" = "1" ]; then
    say "跳过镜像构建（WV_SKIP_BUILD=1）"
else
    say "构建镜像 wechat-vault:single ..."
    ( cd "$DEPLOY" && "${COMPOSE[@]}" -f docker-compose.single.yml build )
    ok "镜像构建完成"
fi

# ── 5. 启动容器 ───────────────────────────────────────────
say "启动容器 ..."
( cd "$DEPLOY" && "${COMPOSE[@]}" -f docker-compose.single.yml up -d )
ok "容器已启动"

# ── 6. 健康验收（最长等 120s）───────────────────────────────
say "等待服务就绪 ..."
HEALTH_URL="http://${BIND_IP}:${PORT}/healthz"
STATUS_URL="http://${BIND_IP}:${PORT}/api/status"
READY=0
for i in $(seq 1 60); do
    if curl -fsS -m 5 "$HEALTH_URL" >/dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 2
done
[ "$READY" = "1" ] || die "服务 120s 内未就绪：$HEALTH_URL （看日志：docker logs wechat-vault）"

HEALTH_JSON="$(curl -fsS -m 5 "$HEALTH_URL" || true)"
ok "healthz：$HEALTH_JSON"

STATUS_JSON="$(curl -fsS -m 5 "$STATUS_URL" || true)"
ok "status：$STATUS_JSON"

# ── 7. 输出部署结果 ───────────────────────────────────────
cat <<EOF

════════════════════════════════════════════════════════
 部署完成
════════════════════════════════════════════════════════
 面板地址    http://${BIND_IP}:${PORT}
 桌面地址    http://${BIND_IP}:13000
 数据目录    ${STORE}
 健康检查    ${HEALTH_JSON}

 下一步：
   1. 打开面板，完成首次安装向导（设访问密码）
   2. 打开微信桌面（KasmVNC）扫码登录
   3. 登录后自动同步会接手；或手动触发：
        curl -X POST http://${BIND_IP}:${PORT}/api/ingest
   4. 若解密失败，取钥方式见 README「首次使用：密钥从哪来」

 Agent 验收命令：
   curl -fsS ${HEALTH_URL}        # 期望 {"ok":true,"db":"ok"}
   curl -fsS ${STATUS_URL}        # 期望 conv_count > 0
════════════════════════════════════════════════════════
EOF
exit 0
