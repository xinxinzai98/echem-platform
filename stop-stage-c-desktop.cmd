@echo off
setlocal
title Stop EchemPlatform Stage C Desktop Monitor
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-stage-c-desktop.ps1"
if errorlevel 1 pause
endlocal
