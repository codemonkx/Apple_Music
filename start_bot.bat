@echo off
title Apple Music Telegram Bot (1-Click Unified Launcher)
color 0A
cd /d "%~dp0"
set "PATH=%~dp0bin;%PATH%"

echo =======================================================
echo          APPLE MUSIC TELEGRAM BOT (1-CLICK LAUNCHER)
echo =======================================================
echo.
echo Starting DRM Wrapper Daemon and Telegram Bot...
echo.

python bot.py
pause
