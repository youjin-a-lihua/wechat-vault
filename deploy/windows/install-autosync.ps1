# ============================================================
#  微信数据方舟 (WeChat Vault) · Windows 侧自动同步安装
#
#  作用：注册每日计划任务，自动把微信导出目录同步到 NAS 并归档
#
#  用法：以【管理员 PowerShell】运行
#        powershell -ExecutionPolicy Bypass -File install-autosync.ps1
# ============================================================

$ErrorActionPreference = "Stop"

Write-Host "=== 微信数据方舟 · Windows 自动同步安装 ===" -ForegroundColor Cyan

# ---- 配置区 ----
$Src      = "D:\WeChatExport"                            # 微信导出目录
$NasIp = if ($env:WV_NAS_HOST) { $env:WV_NAS_HOST } else { "192.0.2.10" }   # 示例地址（RFC 5737）；建议设 WV_NAS_HOST
$SyncPs1  = "$PSScriptRoot\sync-now.ps1"                 # 同步脚本（PowerShell）
$Launcher = "$PSScriptRoot\sync-now.cmd"                 # 启动器（ASCII，无编码问题）
$Task     = "WeChatVault-SyncToNAS"
$Time     = "12:30"                                      # 每天 12:30 同步（NAS 03:00 归档）
# ----------------

if (-not (Test-Path $Launcher)) {
    Write-Host "找不到同步启动器: $Launcher" -ForegroundColor Red
    exit 1
}

# 创建导出目录（若不存在）
if (-not (Test-Path $Src)) {
    New-Item -ItemType Directory -Path $Src -Force | Out-Null
    Write-Host "已创建导出目录: $Src" -ForegroundColor Yellow
}

# 删除同名旧任务
if (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $Task -Confirm:$false
    Write-Host "已移除旧任务" -ForegroundColor Yellow
}

$Action   = New-ScheduledTaskAction -Execute "$Launcher"
$Trigger  = New-ScheduledTaskTrigger -Daily -At $Time
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RunOnlyIfNetworkAvailable

Register-ScheduledTask -TaskName $Task -Action $Action -Trigger $Trigger `
    -Settings $Settings -Description "微信数据方舟：每日把微信导出同步到 NAS 并归档" | Out-Null

Write-Host "计划任务已注册: $Task（每天 $Time）" -ForegroundColor Green
Write-Host ""
Write-Host "接下来：" -ForegroundColor Cyan
Write-Host "  1. 微信 -> 设置 -> 聊天记录管理 -> 导入与导出 -> 导出聊天记录"
Write-Host "     导出位置选：$Src"
Write-Host "  2. 打开 http://${NasIp}:8790 查看归档"
Write-Host ""
Write-Host "手动立即同步一次：" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName $Task"
Write-Host ""
Write-Host "也可以用桌面上的 wechat-vault-sync.cmd 随时手动投递。" -ForegroundColor Cyan
