# ============================================================
#  微信数据方舟 (WeChat Vault) - 一键投递到 NAS
#
#  用法（三种都行）：
#    1) 右键 - 使用 PowerShell 运行
#    2) 拖拽文件夹运行：先在本文件上右键 → 发送到 → 桌面快捷方式，
#       然后把导出的文件夹拖到那个快捷方式上
#    3) 放在任务计划程序里，每天定时自动跑
#
#  多账号：在脚本顶部的 $DefaultAccount 填你的微信号名（如 "主号"），
#          导出文件就会投到 NAS 上 inbox\主号\ 这个专属子目录，
#          不同微信号的数据互不干扰。留空则投到 inbox 根（归入默认账号）。
#
#  自动完成：同步到 NAS -> 立即归档 -> 打开查看页
# ============================================================

param(
    [string]$Source = "",
    [string]$Account = ""
)

# ---------- 配置区 ----------
$DefaultSrc = "D:\WeChatExport"
$NasIp = if ($env:WV_NAS_HOST) { $env:WV_NAS_HOST } else { "192.0.2.10" }   # 示例地址（RFC 5737）；建议设 WV_NAS_HOST
# 默认账号名：导出文件会投到 inbox\<账号名>\ 之下
# 一个微信号一个子目录，多账号互不干扰。留空则投到 inbox 根目录（归入"默认账号"）
$DefaultAccount = ""
$NasWeb     = "http://${NasIp}:8790"
# ----------------------------

$ErrorActionPreference = "Continue"
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8

if ([string]::IsNullOrWhiteSpace($Source)) { $Source = $DefaultSrc }
if ([string]::IsNullOrWhiteSpace($Account)) { $Account = $DefaultAccount }

# 投放目标：有账号名则用子目录（多账号隔离），否则用 inbox 根
if ([string]::IsNullOrWhiteSpace($Account)) {
    $Dst = "\\$NasIp\wechat-vault\inbox"
} else {
    $Dst = "\\$NasIp\wechat-vault\inbox\$Account"
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "   微信数据方舟 - 一键投递" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# ---- 源目录校验 ----
if (-not (Test-Path -LiteralPath $Source)) {
    Write-Host "  [错误] 找不到源目录：" -ForegroundColor Red
    Write-Host "         $Source"
    Write-Host ""
    Write-Host "  请先做以下任一件事："
    Write-Host "    1) 微信 -> 设置 -> 聊天记录管理 -> 导入与导出"
    Write-Host "       把聊天记录导出到：$DefaultSrc"
    Write-Host "    2) 或把导出好的文件夹拖到本脚本的快捷方式上运行"
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

$files = @(Get-ChildItem -LiteralPath $Source -Recurse -File -ErrorAction SilentlyContinue)
if ($files.Count -eq 0) {
    Write-Host "  [提示] 源目录里还没有文件：" -ForegroundColor Yellow
    Write-Host "         $Source"
    Write-Host ""
    Write-Host "  请先在微信里执行导出。"
    Write-Host "  微信路径：设置 -> 聊天记录管理 -> 导入与导出 -> 导出聊天记录"
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

Write-Host "  源目录 : $Source"
Write-Host "  文件数 : $($files.Count)"
Write-Host "  NAS    : $NasIp"
if ([string]::IsNullOrWhiteSpace($Account)) {
    Write-Host "  账号   : (默认账号，投到 inbox 根目录)" -ForegroundColor Yellow
} else {
    Write-Host "  账号   : $Account"
}
Write-Host "  目标   : $Dst"
Write-Host ""

# ---- 1/4 连通性 ----
Write-Host "[1/4] 检查 NAS 连通性..."
$ok = Test-Connection -ComputerName $NasIp -Count 1 -Quiet -ErrorAction SilentlyContinue
if (-not $ok) {
    Write-Host "       [失败] 连不上 NAS $NasIp" -ForegroundColor Red
    Write-Host "       请确认 NAS 已开机、和本机在同一局域网。"
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}
Write-Host "       OK" -ForegroundColor Green

# ---- 2/4 同步 ----
Write-Host "[2/4] 同步文件到 NAS..."
$rc = robocopy $Source $Dst /E /XO /R:1 /W:2 /NP /NDL /NJH /NJS /NC /NS
# robocopy 返回码 0-7 视为成功
if ($LASTEXITCODE -ge 8) {
    Write-Host "       [失败] 同步出错（robocopy 代码 $LASTEXITCODE）" -ForegroundColor Red
    Write-Host "       请确认共享可访问：$Dst"
    Write-Host ""
    Read-Host "按回车键退出"
    exit $LASTEXITCODE
}
Write-Host "       OK" -ForegroundColor Green

# ---- 3/4 立即归档 ----
Write-Host "[3/4] 通知 NAS 立即归档..."
$resp = $null
try {
    $r = Invoke-WebRequest -UseBasicParsing -Method POST -Uri "$NasWeb/api/ingest" -TimeoutSec 180
    $resp = $r.Content
} catch {
    # 退回 curl
    try { $resp = & curl.exe -s -m 180 -X POST "$NasWeb/api/ingest" 2>$null } catch {}
}

if ([string]::IsNullOrWhiteSpace($resp)) {
    Write-Host "       提示：未收到归档响应。" -ForegroundColor Yellow
    Write-Host "       NAS 每天 03:00 会自动归档，届时刷新页面即可看到。"
} else {
    Write-Host "       OK" -ForegroundColor Green
    Write-Host "       $resp"
}

# ---- 4/4 打开查看页 ----
Write-Host "[4/4] 打开查看页面..."
Start-Process $NasWeb

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "   完成！浏览器已打开：$NasWeb" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Start-Sleep -Seconds 3
