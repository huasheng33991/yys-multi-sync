@echo off
title YYS-Sync (console)

rem check elevation; if not admin, relaunch self via UAC
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting admin rights... click YES on the UAC popup.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo [OK] Elevated. Starting console sync tool...
echo.
rem cmd /k keeps this window open even if python crashes
cmd /k python -I "D:\demo\dj\yys_sync\sync_full.py"
