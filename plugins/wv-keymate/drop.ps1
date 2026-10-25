# ============================================================
#  wv-keymate 一键投递 —— 密钥包 → NAS → 触发同步
#
#  用法：
#    1) 右键 - 使用 PowerShell 运行
#    2) 命令行：powershell -ExecutionPolicy Bypass -File drop.ps1 -KeysDir <密钥包目录>
#    3) 放在任务计划程序里，取完密钥后定时投递
#
#  投递内容（密钥契约，勿改名）：
#    passphrase.json   账号级 64 hex passphrase（含元数据）
#    all_keys.json     逐库 enc_key / salt
#
#  目标：\\<NAS>\wechat-vault\wx_backup\  ←→  容器内 /data/wx_backup
#        （wv_sync --keys 的默认目录）。全程只走 SMB 内网，不开公网。
# ============================================================

param(
    [string]$KeysDir = "",
    [string]$NasHost = ""
)

# ---------- 配置区 ----------
# NAS 地址：建议设环境变量 WV_NAS_HOST（只允许私网；192.0.2.10 是占位示例）
$NasIp = if ($NasHost) { $NasHost } elseif ($env:WV_NAS_HOST) { $env:WV_NAS_HOST } else { "192.0.2.10" }
$Share = "wechat-vault"
$RemoteDir = "wx_backup"
$NasWeb = "http://${NasIp}:8790"
# ----------------------------

$ErrorActionPreference = "Continue"
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

if ([string]::IsNullOrWhiteSpace($KeysDir)) { $KeysDir = "keymate_out" }

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "   wv-keymate 密钥包一键投递" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# ---- 私网白名单（绝不开公网）----
$ipOk = $false
try {
    $ip = [System.Net.IPAddress]::Parse($NasIp)
    $b = $ip.GetAddressBytes()
    if ($ip.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
        $ipOk = ($b[0] -eq 10) -or
                ($b[0] -eq 172 -and $b[1] -ge 16 -and $b[1] -le 31) -or
                ($b[0] -eq 192 -and $b[1] -eq 168) -or
                ($b[0] -eq 127)
    }
} catch { $ipOk = $false }
if (-not $ipOk) {
    Write-Host "  [拒绝] 目标 $NasIp 不在私网白名单" -ForegroundColor Red
    Write-Host "         只允许 10/8、172.16/12、192.168/16、127/8。本工具绝不开公网。" -ForegroundColor Red
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

# ---- 源目录校验 ----
if (-not (Test-Path -LiteralPath $KeysDir)) {
    Write-Host "  [错误] 找不到密钥包目录：" -ForegroundColor Red
    Write-Host "         $KeysDir"
    Write-Host ""
    Write-Host "  请先跑 keymate.py 生成 passphrase.json / all_keys.json"
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

$want = @("passphrase.json", "all_keys.json")
$have = @()
foreach ($f in $want) {
    if (Test-Path -LiteralPath (Join-Path $KeysDir $f)) { $have += $f }
}
if ($have.Count -eq 0) {
    Write-Host "  [错误] 目录里没有密钥契约文件（passphrase.json / all_keys.json）" -ForegroundColor Red
    Write-Host "         $KeysDir"
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

$Dst = "\\$NasIp\$Share\$RemoteDir"

Write-Host "  密钥包 : $KeysDir"
Write-Host "  文件   : $($have -join ', ')"
Write-Host "  NAS    : $NasIp"
Write-Host "  目标   : $Dst"
Write-Host ""

# ---- 1/3 连通性 ----
Write-Host "[1/3] 检查 NAS 连通性..."
$ok = Test-Connection -ComputerName $NasIp -Count 1 -Quiet -ErrorAction SilentlyContinue
if (-not $ok) {
    Write-Host "       [失败] 连不上 NAS $NasIp" -ForegroundColor Red
    Write-Host "       请确认 NAS 已开机、和本机在同一局域网。" -ForegroundColor Red
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}
Write-Host "       OK" -ForegroundColor Green

# ---- 2/3 同步密钥包 ----
Write-Host "[2/3] 投递密钥包到 NAS..."
# 与 deploy/windows/sync-now.ps1 同一套 robocopy 参数：0-7 视为成功
$rc = robocopy $KeysDir $Dst $have /R:1 /W:2 /NP /NDL /NJH /NJS /NC /NS
if ($LASTEXITCODE -ge 8) {
    Write-Host "       [失败] 投递出错（robocopy 代码 $LASTEXITCODE）" -ForegroundColor Red
    Write-Host "       请确认共享可访问：$Dst"
    Write-Host ""
    Read-Host "按回车键退出"
    exit $LASTEXITCODE
}
Write-Host "       OK" -ForegroundColor Green
Write-Host "       提示：请在 NAS 端执行 chmod 600 $RemoteDir/passphrase.json $RemoteDir/all_keys.json" -ForegroundColor Yellow

# ---- 3/3 触发同步 ----
Write-Host "[3/3] 通知 NAS 立即同步..."
$resp = $null
try {
    $r = Invoke-WebRequest -UseBasicParsing -Method POST -Uri "$NasWeb/api/ingest" -TimeoutSec 180
    $resp = $r.Content
} catch {
    try { $resp = & curl.exe -s -m 180 -X POST "$NasWeb/api/ingest" 2>$null } catch {}
}

if ([string]::IsNullOrWhiteSpace($resp)) {
    Write-Host "       提示：未收到同步响应。" -ForegroundColor Yellow
    Write-Host "       NAS 端定时任务会自动续跑，也可手动再触发一次。" -ForegroundColor Yellow
} else {
    Write-Host "       OK" -ForegroundColor Green
    Write-Host "       $resp"
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "   完成！密钥包已投递到 $Dst" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Start-Sleep -Seconds 2
