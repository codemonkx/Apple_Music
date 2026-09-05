#!/usr/bin/env bash
# ==============================================================================
# Apple Music Telegram Bot & DRM Wrapper - Unified Launcher for Linux
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

WRAPPER_DIR="$HOME/wrapper"
WRAPPER_LOG="$WRAPPER_DIR/wrapper.log"
CONFIG_FILE="$SCRIPT_DIR/config.yaml"
TARGET_TOKEN_DIR="$WRAPPER_DIR/rootfs/data/data/com.apple.android.music/files"

# 1. Setup environment paths
export PATH="$SCRIPT_DIR/bin:$PATH"

CODIUM_PY_PKG="$HOME/.var/app/com.vscodium.codium-insiders/data/python/lib/python3.13/site-packages"
if [ -d "$CODIUM_PY_PKG" ]; then
    export PYTHONPATH="$CODIUM_PY_PKG:${PYTHONPATH:-}"
fi

echo "======================================================="
echo "   APPLE MUSIC TELEGRAM BOT (UNIFIED LINUX LAUNCHER)   "
echo "======================================================="
echo ""

# 2. Check dependencies
if ! command -v python3 >/dev/null 2>&1; then
    echo "[!] Error: python3 is not installed or not in PATH."
    exit 1
fi

# 3. Ensure am-dl executable is ready
if [ ! -f "$SCRIPT_DIR/am-dl" ]; then
    echo "[i] am-dl binary not found. Compiling with Go..."
    if command -v go >/dev/null 2>&1; then
        go build -o am-dl main.go
        chmod +x am-dl
        echo "[+] am-dl compiled successfully."
    else
        echo "[!] Error: 'go' is not installed to compile am-dl."
        echo "    Install Go or provide a pre-compiled 'am-dl' binary."
        exit 1
    fi
fi

# 4. Sync media-user-token into wrapper cache
if [ -f "$CONFIG_FILE" ]; then
    mkdir -p "$TARGET_TOKEN_DIR"
    python3 -c "
import yaml, os
try:
    with open('$CONFIG_FILE') as f:
        cfg = yaml.safe_load(f)
    token = (cfg.get('media-user-token') or '').strip()
    if token:
        with open('$TARGET_TOKEN_DIR/MUSIC_TOKEN', 'w') as tf:
            tf.write(token)
except Exception:
    pass
" 2>/dev/null || true
fi

# 5. Check / Launch DRM Wrapper Daemon
is_wrapper_running() {
    python3 -c "
import socket, sys
s = socket.socket()
s.settimeout(0.5)
try:
    s.connect(('127.0.0.1', 20020))
    s.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
" >/dev/null 2>&1
}

if is_wrapper_running; then
    echo "[+] DRM Wrapper Status: ONLINE (Port 20020 Reachable)"
else
    echo "[!] DRM Wrapper is offline."
    echo "[i] Starting DRM wrapper daemon in background..."

    if [ -x "$SCRIPT_DIR/start_wrapper.sh" ]; then
        nohup "$SCRIPT_DIR/start_wrapper.sh" > "$WRAPPER_LOG" 2>&1 &
    else
        mkdir -p "$WRAPPER_DIR"
        nohup bash -c "cd $WRAPPER_DIR && while true; do ./wrapper; sleep 2; done" > "$WRAPPER_LOG" 2>&1 &
    fi

    echo -n "[i] Waiting for DRM wrapper to come online"
    for i in {1..20}; do
        if is_wrapper_running; then
            echo " [OK]"
            echo "[+] DRM Wrapper Status: ONLINE (Port 20020 Reachable)"
            break
        fi
        echo -n "."
        sleep 0.5
    done

    if ! is_wrapper_running; then
        echo ""
        echo "[!] Warning: DRM Wrapper did not respond within 10s."
        echo "    Check logs at: $WRAPPER_LOG"
    fi
fi

echo ""
echo "[+] Starting Telegram Bot..."
echo "======================================================="

exec python3 bot.py "$@"
