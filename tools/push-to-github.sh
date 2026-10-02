#!/usr/bin/env bash
# =============================================================================
# 微信数据方舟 · 一键推送脚本
# -----------------------------------------------------------------------------
# 本地 commit 已就绪（23409c8）。本脚本用你的 GitHub PAT 完成：
#   1) 创建公开仓库 wechat-vault（若不存在）
#   2) push 全部 99 个文件
#
# 用法（在仓库根目录执行）：
#   export GITHUB_TOKEN=ghp_你的token
#   bash tools/push-to-github.sh
#
# ⚠️ 用完请立即到 https://github.com/settings/tokens 撤销该 token。
# =============================================================================
set -euo pipefail

TOKEN="${GITHUB_TOKEN:-}"
OWNER="youjin-a-lihua"
REPO="wechat-vault"

if [[ -z "$TOKEN" ]]; then
  echo "❌ 缺少 GITHUB_TOKEN。请先：export GITHUB_TOKEN=ghp_xxx"
  exit 1
fi

REMOTE="https://${TOKEN}@github.com/${OWNER}/${REPO}.git"

# 1) 建仓库（若不存在）。已存在会返回 422，忽略即可。
echo "▶ 创建仓库 ${OWNER}/${REPO} …"
HTTP=$(curl -s -o /tmp/gh-create.json -w "%{http_code}" \
  -H "Authorization: token ${TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/user/repos \
  -d "{\"name\":\"${REPO}\",\"private\":false,\"description\":\"微信数据方舟 —— 开源、可 Docker 部署的微信聊天记录归档与查看系统\"}")
if [[ "$HTTP" == "201" ]]; then
  echo "  ✅ 仓库已创建"
elif [[ "$HTTP" == "422" ]]; then
  echo "  ⚠️ 仓库已存在，直接推送"
else
  echo "  ❌ 建仓失败（HTTP $HTTP）：$(cat /tmp/gh-create.json | head -c 300)"
  exit 1
fi

# 2) 推送（用 token 内嵌的 remote，不写进磁盘）
echo "▶ 推送到 ${REMOTE%%@*}…"
git push "$REMOTE" HEAD:main 2>&1 | tail -8

echo ""
echo "✅ 完成！仓库地址：https://github.com/${OWNER}/${REPO}"
echo "⚠️ 立即撤销 token：https://github.com/settings/tokens"
