@echo off
setlocal
title EchemPlatform Stage C - Locked Desktop Monitor
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-stage-c-desktop.ps1"
if errorlevel 1 pause
endlocal
