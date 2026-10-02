#!/bin/bash
# 在 NAS 上清理 demo 数据（改为归档，不删除，可回滚）
set -e
ROOT=/vol3/1000/wechat-vault
cd "$ROOT"

docker stop wechat-vault >/dev/null 2>&1 || true

D="_demo_backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$D"

# 主库与清单
[ -f vault.db ] && mv vault.db "$D/"
[ -f MANIFEST.json ] && mv MANIFEST.json "$D/"

# inbox 下的账号目录（每个子目录 = 一个假账号）
for a in inbox/*/; do
  [ -d "$a" ] || continue
  name=$(basename "$a")
  [ "$name" = "raw" ] && continue
  mv "$a" "$D/inbox_$name"
done

# 存档轨投放口内容
if [ -d inbox/raw ]; then
  mkdir -p "$D/inbox_raw"
  find inbox/raw -mindepth 1 -maxdepth 1 -exec mv {} "$D/inbox_raw/" \; 2>/dev/null || true
fi

# 存档快照
[ -d raw-snapshots ] && mv raw-snapshots "$D/raw-snapshots"

# 重建干净的骨架
mkdir -p inbox/raw raw-snapshots media logs

chown -R 1000:1001 "$ROOT" 2>/dev/null || true

echo "=== 已归档到 $D ==="
ls -la "$D/" 2>/dev/null
echo
echo "=== 现在的 $ROOT ==="
ls -la "$ROOT/"
echo
echo "=== inbox 结构（应为空，仅 raw） ==="
find "$ROOT/inbox"
echo
echo "=== raw-snapshots（应为空） ==="
ls -la "$ROOT/raw-snapshots/"
