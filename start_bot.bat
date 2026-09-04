@echo off
title Apple Music Telegram Bot (1-Click Unified Launcher)
color 0A
cd /d "%~dp0"
set "PATH=%~dp0bin;%PATH%"

echo =======================================================
echo          APPLE MUSIC TELEGRAM BOT (1-CLICK LAUNCHER)
echo =======================================================
echo.

:: Check if WSL wrapper is already listening on port 20020
powershell -NoProfile -Command "$client = New-Object System.Net.Sockets.TcpClient; try { $client.Connect('127.0.0.1', 20020); $client.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] DRM Wrapper daemon is offline.
    echo [i] Starting background DRM daemon in WSL...
    powershell -NoProfile -Command "Start-Process wsl -ArgumentList '-e', 'bash', '-c', 'cd ~/wrapper && while true; do ./wrapper; sleep 2; done' -WindowStyle Hidden"
    echo [i] Waiting for DRM daemon to come online...
    timeout /t 2 >nul
) else (
    echo [i] DRM Wrapper is already active.
)

echo Starting Telegram Bot...
echo.
python bot.py
pause
