#!/bin/sh
# ============================================================
#  WeChat Vault 容器入口
#  职责：① 首次建库 ② 启动即归档一次 ③ 后台定时归档 ④ 前台跑查看器
#  冷备：归档后自动镜像到 HC620（纯 Python，容器内无需 rsync）
#
#  目录结构（设计文档规定，热侧全部落在 $ROOT 之下）：
#    $ROOT/inbox/           投放口
#    $ROOT/inbox/<微信号>/  一个子目录 = 一个微信号（多账号隔离）
#    $ROOT/inbox/raw/       存档轨投放口（.bak / xwechat_files 全量）
#    $ROOT/media/           解码后的图片/视频
#    $ROOT/raw-snapshots/   存档轨原始包，按日期分代
#    $ROOT/vault.db         归档主库
#    $ROOT/MANIFEST.json    完整性指纹清单
#    $ROOT/logs/            运行日志（不参与冷备镜像）
# ============================================================
set -e

# ---- 自愈：确保静态资源可读 ----
# 镜像里已 chmod，但若有人用 bind mount 覆盖 /app，或从 tar 解包带入
# 600/700 权限，非 root 进程会读不到 index.html（首页 500）。
# 失败不致命（只读挂载时无权限改），继续往下走。
if [ -w /app ]; then
    find /app -type d -exec chmod 755 {} + 2>/dev/null || true
    find /app -type f -exec chmod 644 {} + 2>/dev/null || true
    chmod +x /app/entrypoint.sh 2>/dev/null || true
fi

ROOT="${WV_ROOT:-/data}"                       # 热侧根（对应 /vol3/1000/wechat-vault）
STORE="${WV_STORE:-$ROOT}"                     # 归档库 == 热侧根
INBOX="${WV_INBOX:-$ROOT/inbox}"               # 投放口
RAW_INBOX="${WV_RAW_INBOX:-$INBOX/raw}"        # 存档轨投放口
RAW_SNAPS="${WV_RAW_SNAPSHOTS:-$ROOT/raw-snapshots}"
LOGS="${WV_LOGS:-$ROOT/logs}"
COLD="${WV_COLD:-/data-cold}"                  # HC620 冷备挂载点（可选）
SCAN_HOUR="${WV_SCAN_HOUR:-3}"                 # 每天几点归档（默认 03:00）
SYNC_COLD="${WV_SYNC_COLD:-1}"                 # 是否冷备（1=是）
ARCHIVE_RAW="${WV_ARCHIVE_RAW:-1}"             # 是否处理存档轨（1=是）

mkdir -p "$INBOX" "$RAW_INBOX" "$RAW_SNAPS" "$LOGS" "$ROOT/media" 2>/dev/null || true

echo "=========================================="
echo "  WeChat Vault · 微信数据方舟"
echo "=========================================="
echo "  运行身份: $(id -u):$(id -g)"
echo "  热侧根:   $ROOT"
echo "  归档库:   $STORE/vault.db"
echo "  投放口:   $INBOX"
echo "  存档轨:   $RAW_INBOX  →  $RAW_SNAPS ($([ "$ARCHIVE_RAW" = "1" ] && echo 启用 || echo 关闭))"
echo "  日志:     $LOGS"
echo "  冷备:     $COLD ($([ "$SYNC_COLD" = "1" ] && echo 启用 || echo 关闭))"
echo "  定时:     每天 ${SCAN_HOUR}:00"
echo "=========================================="

# ---- 启动即归档一次（把收件箱里已有的东西吃进来）----
# 注意：只扫 inbox 里**真实投放的文件**；演示/测试文件请放在 do-not-scan/ 或
#       用 _ / . 开头命名，归档器会自动跳过（见 wv_archiver.scan 的过滤规则）。
echo "[启动] 首次归档扫描…"
{
  echo "[$(date '+%F %T')] ===== 容器启动 ====="
  echo "  运行身份: $(id -u):$(id -g)   归档库: $STORE"
  # ① 可读轨：inbox 下的 TXT/HTML/CSV/JSON（按子目录分账号）
  #    ⚠ 必须排除 raw/ —— 那是存档轨投放口，里面的 .db / BAK_* 不是可读轨文件
  python /app/archiver/wv_archiver.py scan --source "$INBOX" --store "$STORE" \
    --exclude-dir raw 2>&1
  # ② 存档轨：inbox/raw 下的 .bak / 全量目录 → raw-snapshots/YYYY-MM-DD/
  if [ "$ARCHIVE_RAW" = "1" ]; then
    python /app/archiver/wv_raw.py ingest \
      --source "$RAW_INBOX" --dest "$RAW_SNAPS" --manifest "$STORE/MANIFEST.json" 2>&1 || true
  fi
} >> "$LOGS/scan.log" 2>&1 || true
tail -4 "$LOGS/scan.log" 2>/dev/null || true

# 启动即冷备一次，保证 HC620 与热库一致
# 排除 logs/（运行日志会不断变化，镜像它会造成无意义抖动）
if [ "$SYNC_COLD" = "1" ] && [ -d "$COLD" ]; then
    echo "[启动] 首次冷备到 $COLD …"
    python /app/archiver/wv_mirror.py --src "$STORE" --dst "$COLD" \
        --exclude logs --quiet 2>&1 >> "$LOGS/scan.log" || true
    tail -2 "$LOGS/scan.log" 2>/dev/null || true
fi

# ---- 后台定时归档循环 ----
cat > /app/scheduler.sh <<'SCHED'
#!/bin/sh
STORE="$WV_STORE"; INBOX="$WV_INBOX"; LOGS="$WV_LOGS"
RAW_INBOX="$WV_RAW_INBOX"; RAW_SNAPS="$WV_RAW_SNAPSHOTS"; ARCHIVE_RAW="$WV_ARCHIVE_RAW"
COLD="$WV_COLD"; SCAN_HOUR="$WV_SCAN_HOUR"; SYNC_COLD="$WV_SYNC_COLD"
while true; do
    NOW_H=$(date +%H); NOW_M=$(date +%M)
    # 到点（HH:00~HH:04 之间）执行一次；LAST_DAY 防重复
    DAY=$(date +%F)
    if [ "$NOW_H" = "$SCAN_HOUR" ] && [ "$NOW_M" -lt 5 ] && [ "$LAST_DAY" != "$DAY" ]; then
        LAST_DAY="$DAY"
        echo "[$(date '+%F %T')] 定时归档开始" >> "$LOGS/scan.log"
        # ① 可读轨（排除 raw/，防止存档轨包被当可读文件解析）
        python /app/archiver/wv_archiver.py scan --source "$INBOX" --store "$STORE" \
            --exclude-dir raw >> "$LOGS/scan.log" 2>&1 || true
        # ② 存档轨
        if [ "$ARCHIVE_RAW" = "1" ]; then
            python /app/archiver/wv_raw.py ingest \
                --source "$RAW_INBOX" --dest "$RAW_SNAPS" --manifest "$STORE/MANIFEST.json" \
                >> "$LOGS/scan.log" 2>&1 || true
        fi
        # 通知查看器重载（archiver 直连 SQLite，查看器需重读）
        python -c "
import urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:8790/api/reload', data=b'', timeout=10)
except Exception:
    pass
" 2>/dev/null || true
        # 冷备到 HC620（排除 logs）
        if [ "$SYNC_COLD" = "1" ] && [ -d "$COLD" ]; then
            echo "[$(date '+%F %T')] 冷备到 $COLD" >> "$LOGS/scan.log"
            python /app/archiver/wv_mirror.py --src "$STORE" --dst "$COLD" --exclude logs \
                >> "$LOGS/scan.log" 2>&1 || true
        fi
        echo "[$(date '+%F %T')] 定时归档完成" >> "$LOGS/scan.log"
        sleep 300   # 跳过一个 5 分钟窗口，避免重复
    fi
    sleep 60
done
SCHED
chmod +x /app/scheduler.sh

echo "[启动] 后台定时归档调度器…"
/app/scheduler.sh &

echo "[启动] 查看器…"
exec python /app/viewer/wv_server.py --store "$STORE" --host 0.0.0.0 --port 8790
