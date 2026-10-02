#!/bin/bash
# ============================================================================
#  微信运行时控制脚本
# ============================================================================
#  职责：从腾讯官方 CDN 下载微信 Linux 版 deb，解压到数据卷。
#        /data/wechat/opt/wechat/wechat
#
#  镜像不打包微信本体（190~210MB），保持镜像小、构建快、不依赖 CDN。
#  下载源与主备回退逻辑复用 WechatOnCloud 的成熟实现。
#
#  用法：
#    wechat-runtime.sh install    下载并安装微信
#    wechat-runtime.sh status     查看状态（JSON）
# ============================================================================

set -eu

INSTALL_DIR="/data/wechat"
STATE_DIR="/data/wechat/.state"
STATUS_FILE="${STATE_DIR}/status.json"
VERSION_FILE="${STATE_DIR}/version"

# 腾讯官方 CDN（主 + 备）
CDN_PRIMARY="${WECHAT_CDN:-https://dldir1.qq.com/weixin/Universal/Linux}"
CDN_FALLBACK="${WECHAT_CDN_FALLBACK:-https://dldir1v6.qq.com/weixin/Universal/Linux}"
UA="Mozilla/5.0"

wechat_bin() { echo "${INSTALL_DIR}/opt/wechat/wechat"; }
is_installed() { [ -x "$(wechat_bin)" ]; }
cur_version() { [ -f "${VERSION_FILE}" ] && cat "${VERSION_FILE}" || echo ""; }

deb_filename() {
    case "$(dpkg --print-architecture 2>/dev/null)" in
        amd64) echo "WeChatLinux_x86_64.deb" ;;
        arm64) echo "WeChatLinux_arm64.deb" ;;
        *) echo "" ;;
    esac
}

write_status() {
    local phase="$1" percent="$2" message="$3"
    local installed=false
    is_installed && installed=true
    mkdir -p "${STATE_DIR}"
    cat > "${STATUS_FILE}.tmp" << EOF
{"phase":"${phase}","percent":${percent},"installed":${installed},"version":"$(cur_version)","message":"${message}","updatedAt":$(date +%s)}
EOF
    mv -f "${STATUS_FILE}.tmp" "${STATUS_FILE}"
}

print_status() {
    if [ -f "${STATUS_FILE}" ]; then
        cat "${STATUS_FILE}"
    else
        local installed=false
        is_installed && installed=true
        echo "{\"phase\":\"idle\",\"percent\":0,\"installed\":${installed},\"version\":\"$(cur_version)\",\"message\":\"尚未安装\"}"
    fi
}

# ---------------------------------------------------------------------------
# 下载（主备 CDN 回退 + 断点续传，逻辑来自 WOC 的成熟实现）
# ---------------------------------------------------------------------------
download_deb() {
    local file="$1" out="$2"
    for base in "${CDN_PRIMARY}" "${CDN_FALLBACK}"; do
        echo "[方舟] 尝试下载: ${base}/${file}"
        for attempt in 1 2 3; do
            if curl -fSL -C - \
                    --connect-timeout 20 --max-time 1800 \
                    --speed-limit 1024 --speed-time 60 \
                    -A "${UA}" \
                    "${base}/${file}" -o "${out}"; then
                echo "[方舟] 下载成功: $(stat -c%s "${out}" 2>/dev/null || echo 0) 字节"
                return 0
            fi
            echo "[方舟] 第 ${attempt} 次失败，重试…"
            sleep 2
        done
    done
    return 1
}

# ---------------------------------------------------------------------------
do_install() {
    local file; file="$(deb_filename)"
    if [ -z "${file}" ]; then
        write_status "error" 0 "不支持的 CPU 架构：$(dpkg --print-architecture)"
        echo "[方舟] 错误：不支持的架构"
        return 1
    fi

    write_status "downloading" 5 "开始下载微信（${file}）"
    mkdir -p "${INSTALL_DIR}" "${STATE_DIR}"

    local tmp="${STATE_DIR}/${file}"
    # 复用已下完的包，避免重复下载
    if [ ! -s "${tmp}" ]; then
        if ! download_deb "${file}" "${tmp}"; then
            write_status "error" 0 "下载失败（主备 CDN 均不可用）"
            return 1
        fi
    fi
    write_status "downloading" 60 "下载完成"

    # 校验是合法 deb
    if ! dpkg-deb --info "${tmp}" >/dev/null 2>&1; then
        write_status "error" 0 "下载的包不是有效 deb（可能被网络劫持/不完整）"
        rm -f "${tmp}"
        return 1
    fi

    write_status "extracting" 70 "解压中…"
    local staging="${INSTALL_DIR}.new"
    rm -rf "${staging}"; mkdir -p "${staging}"
    if ! dpkg-deb -x "${tmp}" "${staging}"; then
        write_status "error" 0 "解压失败"
        rm -rf "${staging}"
        return 1
    fi

    write_status "installing" 90 "安装中…"
    # 原子替换：先把旧的挪走，再把新的就位
    local old="${INSTALL_DIR}.old"
    rm -rf "${old}" 2>/dev/null || true
    [ -d "${INSTALL_DIR}" ] && mv "${INSTALL_DIR}" "${old}" 2>/dev/null || true
    mv "${staging}" "${INSTALL_DIR}"
    rm -rf "${old}" 2>/dev/null || true

    # 记录版本
    if [ -x "${INSTALL_DIR}/opt/wechat/wechat" ]; then
        v="$(dpkg-deb -f "${tmp}" Version 2>/dev/null || echo unknown)"
        echo "${v}" > "${VERSION_FILE}"
        rm -f "${tmp}"
        write_status "done" 100 "安装完成（版本 ${v}）"
        echo "[方舟] 微信安装完成: $(wechat_bin)"
        return 0
    else
        write_status "error" 0 "安装后找不到可执行文件（包结构异常）"
        return 1
    fi
}

case "${1:-status}" in
    install|update) do_install ;;
    status) print_status ;;
    *) echo "用法: $0 {install|status}"; exit 2 ;;
esac
