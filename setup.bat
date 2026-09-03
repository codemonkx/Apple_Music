@echo off
title Apple Music Downloader - Quick Setup
color 0A
cd /d "%~dp0"

echo ================================================================
echo       APPLE MUSIC LOSSLESS DOWNLOADER - QUICK SETUP
echo ================================================================
echo.

echo [1/3] Checking Python installation...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Python is not installed or not in PATH!
    echo Please install Python 3.10+ from python.org
    pause
    exit /b
)
echo Python is installed.
echo.

echo [2/3] Installing Python dependencies (Telethon, PyYAML, etc.)...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo Warning: Failed to install some Python packages.
)
echo.

echo [3/3] Checking Downloader Executable...
if not exist "am-dl.exe" (
    echo Building am-dl.exe from Go source...
    go build -o am-dl.exe main.go
    if exist "am-dl.exe" (
        echo am-dl.exe built successfully!
    ) else (
        echo Warning: Go compiler not found. am-dl.exe could not be compiled.
    )
echo.
echo Checking bin/ dependencies (MP4Box and FFmpeg)...
if not exist "bin\mp4box.exe" (
    echo Warning: bin\mp4box.exe not found!
) else (
    echo MP4Box binary is ready in bin\
)
if not exist "bin\ffmpeg.exe" (
    echo Note: bin\ffmpeg.exe not found (required only for conversion and animated artwork).
) else (
    echo FFmpeg binary is ready in bin\
)
echo.

if not exist "config.yaml" (
    copy config.yaml.example config.yaml >nul
    echo Created config.yaml from template.
)

echo ================================================================
echo                SETUP COMPLETED SUCCESSFULLY!
echo ================================================================
echo.
echo Next steps:
echo 1. Edit config.yaml if you need to adjust storefront or token.
echo 2. Start the DRM wrapper in WSL: start_wrapper.bat
echo 3. Start your Telegram Bot: start_bot.bat
echo.
pause

