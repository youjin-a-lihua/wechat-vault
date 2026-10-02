#!/bin/bash
# ============================================================================
#  启动钩子（00）：实例唯一 machine-id
# ============================================================================
#  容器镜像里烤死的 machine-id 被所有实例共用，会触发腾讯"设备农场"风控，
#  表现为登录后立即被强制退出。故每次启动生成/复用一个持久的唯一 ID。
#
#  持久化在数据卷 /config/.wv-machine-id，容器重建后不变。
# ============================================================================
set -eu

ID_FILE="/config/.wv-machine-id"

mkdir -p /config 2>/dev/null || true

if [ ! -s "${ID_FILE}" ]; then
    # 生成一个稳定的 32 位十六进制 ID
    cat /proc/sys/kernel/random/uuid | tr -d '-' > "${ID_FILE}" 2>/dev/null \
        || od -An -N16 -tx1 /dev/urandom | tr -d ' \n' > "${ID_FILE}"
    echo "[方舟] 已生成新 machine-id"
fi

MID="$(cat "${ID_FILE}")"
if [ -d /etc/machine-id ]; then
    echo "${MID}" > /etc/machine-id 2>/dev/null || true
fi
[ -f /etc/machine-id ] && echo "${MID}" > /etc/machine-id 2>/dev/null || true
mkdir -p /var/lib/dbus 2>/dev/null || true
echo "${MID}" > /var/lib/dbus/machine-id 2>/dev/null || true

echo "[方舟] machine-id: ${MID}"
