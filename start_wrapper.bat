@echo off
title Apple Music DRM Wrapper Launcher
color 0B
cd /d "%~dp0"

echo ================================================================
echo             APPLE MUSIC DRM WRAPPER LAUNCHER
echo ================================================================
echo.
echo Checking WSL environment...
wsl uname -a >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: WSL is not enabled on this machine.
    echo Please enable WSL2: wsl --install
    pause
    exit /b
)

echo.
echo Launching wrapper inside WSL (Ubuntu)...
echo Keep this window open while downloading or using the bot.
echo.

wsl -e bash -c "if [ ! -f ~/wrapper/wrapper ]; then mkdir -p ~/wrapper && cd ~/wrapper && curl -fL 'https://github.com/WorldObservationLog/wrapper/releases/download/wrapper.x86_64.latest/Wrapper.x86_64.latest.zip' -o Wrapper.zip && unzip -o Wrapper.zip && chmod +x wrapper; fi; cd ~/wrapper && while true; do ./wrapper; echo '[!] Wrapper session ended, restarting in 2s...'; sleep 2; done"
pause

