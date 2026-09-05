#!/usr/bin/env bash
# Apple Music DRM Wrapper Launcher for Linux

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER_DIR="$HOME/wrapper"
CONFIG_FILE="$SCRIPT_DIR/config.yaml"
TARGET_TOKEN_DIR="$WRAPPER_DIR/rootfs/data/data/com.apple.android.music/files"

echo "================================================================"
echo "            APPLE MUSIC DRM WRAPPER LAUNCHER (LINUX)"
echo "================================================================"
echo ""

mkdir -p "$WRAPPER_DIR"

if [ ! -f "$WRAPPER_DIR/wrapper" ]; then
    echo "[+] Downloading wrapper binary..."
    cd "$WRAPPER_DIR"
    curl -fL 'https://github.com/WorldObservationLog/wrapper/releases/download/wrapper.x86_64.latest/Wrapper.x86_64.latest.zip' -o Wrapper.zip
    unzip -o Wrapper.zip
    chmod +x wrapper
fi

# Sync media-user-token from config.yaml into cached MUSIC_TOKEN
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
        print('[+] Synced media-user-token from config.yaml to wrapper cache.')
except Exception as e:
    print(f'[-] Token sync warning: {e}')
" 2>/dev/null || true
fi

echo "[+] Starting wrapper service on ports 10020, 20020, 30020, 40020..."
cd "$WRAPPER_DIR"
while true; do
    ./wrapper "$@"
    EXIT_CODE=$?
    echo "[!] Wrapper session ended (exit code: $EXIT_CODE), restarting in 2s..."
    sleep 2
done
