@echo off
REM ============================================================
REM  WeChat Vault - Launcher (ASCII only, no encoding issues)
REM  Calls the PowerShell script next to this file.
REM ============================================================
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0wechat-vault-sync.ps1" %*
if errorlevel 1 pause
