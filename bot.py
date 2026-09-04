import os
import sys
import json
import asyncio
import logging
import re
import zipfile
import time
import shutil
import uuid
import subprocess
import urllib.request
import urllib.parse
import html
from pathlib import Path
import yaml
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeAudio
from telethon.errors import MessageNotModifiedError

# ---------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Base working directory
BASE_DIR = Path(__file__).resolve().parent
BIN_DIR = BASE_DIR / "bin"
DOWNLOADS_DIR = BASE_DIR / "AM-DL downloads"
ARCHIVES_DIR = DOWNLOADS_DIR / "Archives"
EXE_PATH = BASE_DIR / "am-dl.exe"
CONFIG_PATH = BASE_DIR / "config.yaml"
ENV_PATH = BASE_DIR / ".env"

# Prepend bin/ directory to system PATH so MP4Box and FFmpeg are always found
if BIN_DIR.exists():
    os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ.get("PATH", "")

# In-memory caches for interactive buttons
PENDING_JOBS = {}
SEARCH_CACHE = {}

# ---------------------------------------------------------
# Load Settings from config.yaml, .env, or Environment
# ---------------------------------------------------------
def load_env_file():
    """Simple parser for .env file."""
    if ENV_PATH.exists():
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip('"').strip("'")
                        if key not in os.environ:
                            os.environ[key] = val
        except Exception as e:
            logger.warning(f"Could not read .env: {e}")

def load_bot_settings():
    load_env_file()
    api_id = os.environ.get("TG_API_ID")
    api_hash = os.environ.get("TG_API_HASH")
    bot_token = os.environ.get("TG_BOT_TOKEN")
    allowed_users = []
    auto_clean = False
    storefront = "in"

    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
                api_id = api_id or cfg.get("telegram-api-id")
                api_hash = api_hash or cfg.get("telegram-api-hash")
                bot_token = bot_token or cfg.get("telegram-bot-token")
                allowed_users = cfg.get("allowed-users", []) or []
                auto_clean = bool(cfg.get("auto-clean-downloads", False))
                storefront = cfg.get("storefront", "in") or "in"
        except Exception as e:
            logger.warning(f"Could not read config.yaml: {e}")

    # Fallback defaults
    api_id = api_id or 36379870
    api_hash = api_hash or "f9a516a50ef4f5727a055e148d49005b"
    bot_token = bot_token or "8825166911:AAGLDIMebmDkWUjTRsLL95-LM4HLMn0DDos"

    try:
        api_id = int(api_id)
    except Exception:
        api_id = 0

    return {
        "api_id": api_id,
        "api_hash": str(api_hash).strip(),
        "bot_token": str(bot_token).strip(),
        "allowed_users": allowed_users,
        "auto_clean": auto_clean,
        "storefront": str(storefront).strip(),
    }

# ---------------------------------------------------------
# Security & Whitelist Verification
# ---------------------------------------------------------
def is_user_authorized(sender_id: int, username: str, allowed_users: list) -> bool:
    """Returns True if user is allowed. If allowed_users is empty, bot is public."""
    if not allowed_users:
        return True
    clean_list = [str(u).lower().lstrip("@") for u in allowed_users]
    if str(sender_id) in clean_list:
        return True
    if username and username.lower().lstrip("@") in clean_list:
        return True
    return False

# ---------------------------------------------------------
# DRM Wrapper Detection & Auto-Launcher
# ---------------------------------------------------------
def is_wrapper_running() -> bool:
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 20020))
        s.close()
        return True
    except Exception:
        return False

def ensure_wrapper_running() -> bool:
    """Checks if wrapper is alive. If not, auto-starts it with auto-restart loop in WSL."""
    if is_wrapper_running():
        return True

    logger.info("DRM Wrapper is offline. Auto-starting persistent wrapper daemon in WSL...")
    try:
        wrapper_sh = (
            "if [ ! -f ~/wrapper/wrapper ]; then "
            "mkdir -p ~/wrapper && cd ~/wrapper && "
            "curl -fL 'https://github.com/WorldObservationLog/wrapper/releases/download/wrapper.x86_64.latest/Wrapper.x86_64.latest.zip' -o Wrapper.zip && "
            "unzip -o Wrapper.zip && chmod +x wrapper; "
            "fi; "
            "cd ~/wrapper && while true; do ./wrapper; sleep 2; done"
        )
        cmd = ["wsl", "-e", "bash", "-c", wrapper_sh]
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for _ in range(16):
            time.sleep(0.5)
            if is_wrapper_running():
                logger.info("DRM Wrapper auto-started successfully!")
                return True
    except Exception as e:
        logger.error(f"Failed to auto-start wrapper via WSL: {e}")

    return is_wrapper_running()

def inspect_audio_quality(track_path: Path) -> dict:
    """Inspects the downloaded M4A file via ffmpeg to extract true bit depth, sample rate, and bitrate."""
    fallback = {"display": "24-bit Lossless ALAC"}
    ffmpeg_exe = BIN_DIR / "ffmpeg.exe"
    if not ffmpeg_exe.exists():
        ffmpeg_exe = Path("ffmpeg")
    if not track_path.exists():
        return fallback

    try:
        res = subprocess.run(
            [str(ffmpeg_exe), "-i", str(track_path)],
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = res.stderr or ""
        m_audio = re.search(r"Audio:\s*(\w+).*?,\s*(\d+)\s*Hz.*?(?:\((.*?bit)\))?.*?,\s*(\d+)\s*kb/s", output)
        if m_audio:
            codec_raw = m_audio.group(1).lower()
            sample_rate_hz = int(m_audio.group(2))
            bit_depth = m_audio.group(3) or "24-bit"
            bitrate_kbs = m_audio.group(4)
            rate_khz = f"{sample_rate_hz / 1000:.1f} kHz"

            if "alac" in codec_raw:
                tier = "Hi-Res Lossless" if sample_rate_hz >= 88200 else "Lossless"
                display = f"{bit_depth} / {rate_khz} {tier} ALAC ({bitrate_kbs} kbps)"
            elif "eac3" in codec_raw:
                display = f"Dolby Atmos (Spatial Audio {rate_khz} {bitrate_kbs} kbps)"
            elif "aac" in codec_raw:
                display = f"AAC {bit_depth} / {rate_khz} ({bitrate_kbs} kbps)"
            else:
                display = f"{codec_raw.upper()} {bit_depth} / {rate_khz} ({bitrate_kbs} kbps)"

            return {
                "codec": codec_raw,
                "bit_depth": bit_depth,
                "sample_rate": rate_khz,
                "bitrate": bitrate_kbs,
                "display": display,
            }
    except Exception as e:
        logger.debug(f"inspect_audio_quality note: {e}")

    return fallback

def is_playlist_url(url: str) -> bool:
    """Checks if the URL is an Apple Music playlist."""
    if not url:
        return False
    return "/playlist/" in url or bool(re.search(r"/pl\.[a-zA-Z0-9_-]+", url))

def fetch_playlist_info(url: str) -> dict:
    """Extracts playlist title and description/song count from Apple Music web preview."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw_html = resp.read().decode("utf-8", errors="ignore")
            title_m = re.search(r'property="og:title"\s+content="([^"]+)"', raw_html)
            if not title_m:
                title_m = re.search(r'<title>(.*?)</title>', raw_html)
            desc_m = re.search(r'property="og:description"\s+content="([^"]+)"', raw_html)

            title = title_m.group(1).strip() if title_m else "Apple Music Playlist"
            title = re.sub(r"\s+on\s+Apple\s+Music.*$", "", title)
            title = re.sub(r"\s+-\s+Apple\s+Music.*$", "", title)
            title = html.unescape(title).lstrip("‎").strip()

            desc = html.unescape(desc_m.group(1).strip()) if desc_m else "Curated Playlist"
            return {"title": title, "desc": desc, "description": desc}
    except Exception as e:
        logger.debug(f"fetch_playlist_info note: {e}")
        return {"title": "Apple Music Playlist", "desc": "Curated Playlist", "description": "Curated Playlist"}

# ---------------------------------------------------------
# Apple Music Catalog Search API (Songs & Albums)
# ---------------------------------------------------------
def clean_album_url(url: str) -> str:
    """Strips ?i=... and &uo=... from URLs so they always point to the full album."""
    if not url:
        return ""
    url = re.sub(r"[\?&]uo=\d+", "", url)
    url = re.sub(r"[\?&]i=\d+", "", url)
    if "?" in url and not url.split("?")[1]:
        url = url.rstrip("?")
    return url

def search_apple_music(query: str, storefront: str = "in", entity: str = "song", limit: int = 5):
    """Searches Apple Music catalog for tracks or albums and returns structured results."""
    clean_query = query.strip()
    encoded_query = urllib.parse.quote(clean_query)
    url = f"https://itunes.apple.com/search?term={encoded_query}&entity={entity}&limit={limit}&country={storefront}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = []
            for r in data.get("results", []):
                t_url = r.get("trackViewUrl", "")
                if "&uo=" in t_url:
                    t_url = t_url.split("&uo=")[0]
                a_url = r.get("collectionViewUrl", "")
                if "&uo=" in a_url:
                    a_url = a_url.split("&uo=")[0]

                album_link = clean_album_url(a_url or t_url)
                year = r.get("releaseDate", "")[:4]

                results.append({
                    "is_album": (entity == "album"),
                    "track_name": r.get("trackName") or r.get("collectionName", "Unknown Track"),
                    "artist_name": r.get("artistName", "Unknown Artist"),
                    "album_name": r.get("collectionName", "Unknown Album"),
                    "year": year,
                    "track_count": r.get("trackCount", 0),
                    "track_url": t_url or album_link,
                    "album_url": album_link,
                })
            return results
    except Exception as e:
        logger.error(f"Search failed for '{query}' (entity={entity}): {e}")
        return []

# ---------------------------------------------------------
# Helper to build premium search cards and buttons
# ---------------------------------------------------------
def render_search_view(search_id: str, query: str, entity: str, results: list):
    is_album_search = (entity == "album")
    type_str = "💿 Albums" if is_album_search else "🎵 Songs"
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "✦  **APPLE MUSIC CATALOG**  ✦",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🔍  **Query**   │ `{query}`",
        f"📂  **Section** │ `{type_str}`",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    ]
    buttons = []

    for i, item in enumerate(results):
        num = i + 1
        if is_album_search:
            year_prefix = f"{item['year']} — " if item.get("year") else ""
            lines.append(f"**{num}.** 💿 **{year_prefix}{item['album_name']}**")
            lines.append(f"     👤 {item['artist_name']}  •  📊 {item['track_count']} Tracks\n")
            btn_title = f"{num}. {year_prefix}{item['album_name']}"
        else:
            lines.append(f"**{num}.** 🎶 **{item['track_name']}**")
            lines.append(f"     👤 {item['artist_name']}  •  💿 *{item['album_name']}*\n")
            btn_title = f"{num}. {item['track_name']}"

        if len(btn_title) > 34:
            btn_title = btn_title[:31] + "..."
        buttons.append([Button.inline(btn_title, data=f"sel:{search_id}:{i}".encode())])

    # Add toggle button between Songs and Albums
    if is_album_search:
        buttons.append([Button.inline("🎵 Switch to Songs Search", data=f"tgl:{search_id}:song".encode())])
    else:
        buttons.append([Button.inline("💿 Switch to Albums Search", data=f"tgl:{search_id}:album".encode())])

    lines.append("────────────────────────────")
    lines.append("👇 *Tap an item below to select format:*")
    return "\n".join(lines), buttons

# ---------------------------------------------------------
# URL & Track Helpers
# ---------------------------------------------------------
def extract_url(text: str) -> str:
    match = re.search(r"https?://(?:beta\.music|music|classical\.music)\.apple\.com/\S+", text)
    return match.group(0) if match else ""

def parse_tracks_from_output(stdout_text: str):
    """Parses JSON track array output by am-dl --json."""
    try:
        json_match = re.search(r"\[\s*\{.*\}\s*\]", stdout_text, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(0))
            return data
    except Exception as e:
        logger.error(f"Failed to parse JSON from output: {e}")
    return []

def zip_entire_album(audio_files: list, cover_path: Path, archive_name: str) -> Path:
    """Creates a single .zip containing all album tracks and cover.jpg."""
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[\\/*?:"<>|]', "_", archive_name)
    zip_path = ARCHIVES_DIR / f"{safe_name}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        if cover_path and cover_path.exists():
            zipf.write(cover_path, "cover.jpg")
        for trk in audio_files:
            zipf.write(trk["path"], trk["path"].name)

    return zip_path

# ---------------------------------------------------------
# Core Downloader & Delivery Pipeline
# ---------------------------------------------------------
async def execute_download(client, chat_id, status_msg, url: str, is_single_song: bool, dl_atmos: bool, auto_clean: bool, is_playlist: bool = False):
    """Orchestrates the am-dl.exe download, real-time status updates, and Telegram upload."""
    # Prevent unhandled MessageNotModifiedError when Telegram rejects identical edits
    orig_edit = status_msg.edit
    async def safe_status_edit(*args, **kwargs):
        try:
            return await orig_edit(*args, **kwargs)
        except MessageNotModifiedError:
            return status_msg
        except Exception as err:
            logger.debug(f"Status edit suppressed: {err}")
            return status_msg
    status_msg.edit = safe_status_edit

    ensure_wrapper_running()
    wrapper_warning = "" if is_wrapper_running() else "\n⚠️ `DRM Wrapper offline (Port 20020)`"

    # If full album, ensure any ?i= is stripped so am-dl downloads all tracks
    if not is_single_song and not is_playlist:
        url = clean_album_url(url)

    mode_name = "Dolby Atmos" if dl_atmos else "24-bit Lossless ALAC"
    if is_playlist:
        target_type = "Full Playlist"
    else:
        target_type = "Single Track" if is_single_song else "Full Album"

    await status_msg.edit(
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✦  **APPLE MUSIC**  ✦\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯  **Target**   │ `{target_type}`\n"
        f"💎  **Quality**  │ `{mode_name}`\n"
        f"⚡  **Status**   │ `Connecting to Apple Music API...`" + wrapper_warning + "\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        buttons=None
    )

    cmd = [str(EXE_PATH), "--json"]
    if dl_atmos:
        cmd.append("--atmos")
    if is_single_song:
        cmd.append("--song")
    cmd.append(url)

    logger.info(f"Running downloader: {' '.join(cmd)}")

    stdout_lines = []
    last_edit_time = 0

    async def update_status_live(new_text: str):
        nonlocal last_edit_time
        now = time.time()
        if now - last_edit_time > 2.0:
            last_edit_time = now
            try:
                await status_msg.edit(new_text)
            except Exception:
                pass

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(BASE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        current_song_name = ""
        current_track_num = ""
        buffer = ""

        while True:
            chunk = await process.stdout.read(2048)
            if not chunk:
                break
            text_chunk = chunk.decode("utf-8", errors="replace")
            stdout_lines.append(text_chunk)
            buffer += text_chunk

            parts = re.split(r"[\r\n]+", buffer)
            buffer = parts[-1]

            for line in parts[:-1]:
                line = line.strip()
                if not line:
                    continue

                if "Track " in line and " of " in line:
                    current_track_num = line
                elif re.match(r"^\d{2}\.\s+", line):
                    current_song_name = line
                elif "Downloading..." in line or "Decrypting..." in line:
                    msg_text = (
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"✦  **DOWNLOADING MASTER**  ✦\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎶  **Track**    │ `{current_song_name}`\n"
                        f"📊  **Index**    │ `{current_track_num}`\n"
                        f"⚡  **State**    │ `{line}`\n"
                        f"💎  **Audio**    │ `{mode_name}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                    await update_status_live(msg_text)
                elif "Decrypted" in line or "Downloaded" in line:
                    msg_text = (
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"✦  **DOWNLOADING MASTER**  ✦\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎶  **Track**    │ `{current_song_name}`\n"
                        f"📊  **Index**    │ `{current_track_num}`\n"
                        f"🔓  **State**    │ `{line}` ✅\n"
                        f"💎  **Audio**    │ `{mode_name}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                    await update_status_live(msg_text)
                elif "Track already exists locally" in line:
                    msg_text = (
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"✦  **DOWNLOADING MASTER**  ✦\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎶  **Track**    │ `{current_song_name}`\n"
                        f"📊  **Index**    │ `{current_track_num}`\n"
                        f"⚡  **State**    │ `Cached on disk` ✅\n"
                        f"💎  **Audio**    │ `{mode_name}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                    )
                    await update_status_live(msg_text)

        await process.wait()

        full_stdout = "".join(stdout_lines)
        tracks_info = parse_tracks_from_output(full_stdout)

        audio_files = []
        if tracks_info:
            for item in tracks_info:
                p_str = item.get("path", "")
                if p_str:
                    p = Path(p_str)
                    track_path = p if p.is_absolute() else (BASE_DIR / p)
                    if track_path.exists():
                        audio_files.append({
                            "path": track_path,
                            "title": item.get("song", track_path.stem),
                            "artist": item.get("artist", "Unknown Artist"),
                            "album": item.get("album", "Unknown Album"),
                        })

        if not audio_files and DOWNLOADS_DIR.exists():
            album_dirs = [
                d for d in DOWNLOADS_DIR.glob("*")
                if d.is_dir() and list(d.glob("*.m4a"))
            ] or [
                d for d in DOWNLOADS_DIR.glob("*/*")
                if d.is_dir() and list(d.glob("*.m4a"))
            ]
            if album_dirs:
                latest_album = max(album_dirs, key=lambda d: d.stat().st_mtime)
                for p in sorted(latest_album.glob("*.m4a")):
                    audio_files.append({
                        "path": p,
                        "title": p.stem,
                        "artist": latest_album.parent.name if latest_album.parent != DOWNLOADS_DIR else "Apple Music",
                        "album": latest_album.name,
                    })

        if not audio_files:
            await status_msg.edit(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"❌  **DOWNLOAD FAILED**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"No audio files found. Please verify that `wrapper` is running in WSL.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            return

        album_dir = audio_files[0]["path"].parent
        folder_display_name = album_dir.name
        cover_path = album_dir / "cover.jpg"
        if not cover_path.exists():
            cover_path = None

        # Inspect downloaded file to extract true bit depth, sample rate, and bitrate
        quality_info = inspect_audio_quality(audio_files[0]["path"])
        exact_quality_str = quality_info.get("display", mode_name)
        item_type = "Playlist" if is_playlist else "Album"
        item_icon = "📜" if is_playlist else "💿"

        # -----------------------------------------------------
        # Delivery Mode 1: Single Audio Track
        # -----------------------------------------------------
        if is_single_song and len(audio_files) == 1:
            track = audio_files[0]
            trk_path = track["path"]
            trk_size_mb = trk_path.stat().st_size / (1024 * 1024)

            # Check if single track exceeds Telegram's 2,000 MB limit
            if trk_size_mb >= 2000:
                await status_msg.edit(
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✦  **TRACK STORED LOCALLY**  ✦\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎵  **Title**     │ `{track['title']}`\n"
                    f"👤  **Artist**    │ `{track['artist']}`\n"
                    f"💿  **Album**     │ `{track['album']}`\n"
                    f"📁  **Size**      │ `{trk_size_mb / 1024:.2f} GB` ({trk_size_mb:.1f} MB)\n"
                    f"💎  **Quality**   │ `{exact_quality_str}`\n\n"
                    f"⚠️  *Exceeds Telegram's 2,000 MB upload limit.*\n\n"
                    f"📂  **Saved Locally on PC:**\n"
                    f"`{trk_path}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✨ *Decrypted and saved in bit-perfect studio quality!*"
                )
                return

            await status_msg.edit(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✦  **UPLOADING TO TELEGRAM**  ✦\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎵  **Track**    │ `{track['title']}`\n"
                f"👤  **Artist**   │ `{track['artist']}`\n"
                f"📁  **Size**     │ `{trk_size_mb:.1f} MB`\n"
                f"💎  **Quality**  │ `{exact_quality_str}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

            last_upload_edit = 0
            async def upload_progress(current, total):
                nonlocal last_upload_edit
                now = time.time()
                if now - last_upload_edit > 2.5:
                    last_upload_edit = now
                    pct = (current / total) * 100
                    try:
                        await status_msg.edit(
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"✦  **UPLOADING TO TELEGRAM**  ✦\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"🎵  **Track**    │ `{trk_path.name}`\n"
                            f"📁  **Size**     │ `{total / (1024*1024):.1f} MB`\n"
                            f"⚡  **Progress** │ `{pct:.1f}%` ({current / (1024*1024):.1f} MB)\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                        )
                    except Exception:
                        pass

            # Send directly as playable Telegram Audio
            track_caption = (
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✦  **APPLE MUSIC LOSSLESS**  ✦\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎵  **Title**     │ {track['title']}\n"
                f"👤  **Artist**    │ {track['artist']}\n"
                f"💿  **Album**     │ {track['album']}\n"
                f"💎  **Quality**   │ {exact_quality_str}\n"
                f"📁  **Size**      │ {trk_size_mb:.1f} MB\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

            await client.send_file(
                chat_id,
                trk_path,
                caption=track_caption,
                thumb=cover_path if cover_path else None,
                progress_callback=upload_progress,
                attributes=[
                    DocumentAttributeFilename(trk_path.name),
                    DocumentAttributeAudio(
                        duration=0,
                        title=track["title"],
                        performer=track["artist"],
                    ),
                ],
            )

            await status_msg.edit(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅  **TRACK DELIVERED**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎵  **Title**    │ `{track['title']}`\n"
                f"📁  **Size**     │ `{trk_size_mb:.1f} MB`\n"
                f"💎  **Quality**  │ `{exact_quality_str}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

        # -----------------------------------------------------
        # Delivery Mode 2: Full Album / Playlist (.zip archive)
        # -----------------------------------------------------
        else:
            total_tracks = len(audio_files)
            album_artist = audio_files[0]["artist"]
            total_size_mb = sum(t["path"].stat().st_size for t in audio_files) / (1024 * 1024)

            # Check if total album/playlist exceeds Telegram's 2,000 MB limit
            if total_size_mb >= 2000:
                await status_msg.edit(
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✦  **{item_type.upper()} STORED LOCALLY**  ✦\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{item_icon}  **{item_type}**    │ `{folder_display_name}`\n"
                    f"👤  **Artist**   │ `{album_artist}`\n"
                    f"📊  **Tracks**   │ `{total_tracks} tracks` (100% Downloaded)\n"
                    f"📁  **Size**     │ `{total_size_mb / 1024:.2f} GB` ({total_size_mb:.1f} MB)\n"
                    f"💎  **Quality**  │ `{exact_quality_str}`\n\n"
                    f"⚠️  *Exceeds Telegram's 2,000 MB upload limit.*\n\n"
                    f"📂  **Saved Locally on PC:**\n"
                    f"`{album_dir}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✨ *All {total_tracks} tracks are saved in studio quality!*"
                )
                return

            await status_msg.edit(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✦  **PACKAGING {item_type.upper()}**  ✦\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{item_icon}  **{item_type}**    │ `{folder_display_name}`\n"
                f"📊  **Tracks**   │ `{total_tracks} track(s)` + Cover Art\n"
                f"📁  **Total**    │ `{total_size_mb:.1f} MB`\n"
                f"🗜️  **State**    │ `Creating {folder_display_name}.zip...`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

            loop = asyncio.get_running_loop()
            zip_path = await loop.run_in_executor(
                None,
                lambda: zip_entire_album(audio_files, cover_path, folder_display_name)
            )

            zip_size_mb = zip_path.stat().st_size / (1024 * 1024)

            # Check if created zip archive exceeds 2,000 MB limit
            if zip_size_mb >= 2000:
                if zip_path.exists():
                    zip_path.unlink()
                await status_msg.edit(
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✦  **{item_type.upper()} STORED LOCALLY**  ✦\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{item_icon}  **{item_type}**    │ `{folder_display_name}`\n"
                    f"👤  **Artist**   │ `{album_artist}`\n"
                    f"📊  **Tracks**   │ `{total_tracks} tracks` (100% Downloaded)\n"
                    f"📁  **Size**     │ `{zip_size_mb / 1024:.2f} GB` ({zip_size_mb:.1f} MB)\n"
                    f"💎  **Quality**  │ `{exact_quality_str}`\n\n"
                    f"⚠️  *Exceeds Telegram's 2,000 MB upload limit.*\n\n"
                    f"📂  **Saved Locally on PC:**\n"
                    f"`{album_dir}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✨ *All {total_tracks} tracks are saved in studio quality!*"
                )
                return

            last_upload_edit = 0
            async def upload_progress_callback(current, total):
                nonlocal last_upload_edit
                now = time.time()
                if now - last_upload_edit > 2.5:
                    last_upload_edit = now
                    pct = (current / total) * 100
                    try:
                        await status_msg.edit(
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"✦  **UPLOADING TO TELEGRAM**  ✦\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"📦  **Archive**  │ `{zip_path.name}`\n"
                            f"📁  **Size**     │ `{zip_size_mb:.1f} MB`\n"
                            f"⚡  **Progress** │ `{pct:.1f}%` ({current / (1024*1024):.1f} MB)\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                        )
                    except Exception:
                        pass

            # Upload single zip directly over MTProto
            header_title = "APPLE MUSIC PLAYLIST" if is_playlist else "APPLE MUSIC ALBUM"
            album_caption = (
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✦  **{header_title}**  ✦\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{item_icon}  **{item_type}**    │ {folder_display_name}\n"
                f"👤  **Artist**   │ {album_artist}\n"
                f"📊  **Tracks**   │ {total_tracks} Tracks (Complete)\n"
                f"🖼️  **Cover**    │ Included (`cover.jpg`)\n"
                f"💎  **Quality**  │ {exact_quality_str}\n"
                f"📦  **Archive**  │ {zip_size_mb:.1f} MB\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

            await client.send_file(
                chat_id,
                zip_path,
                caption=album_caption,
                progress_callback=upload_progress_callback,
                attributes=[DocumentAttributeFilename(zip_path.name)],
            )

            delivered_header = "PLAYLIST DELIVERED" if is_playlist else "ALBUM DELIVERED"
            await status_msg.edit(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅  **{delivered_header}**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{item_icon}  **{item_type}**    │ `{folder_display_name}`\n"
                f"📊  **Tracks**   │ `{total_tracks} Tracks`\n"
                f"📦  **Size**     │ `{zip_size_mb:.1f} MB`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

            if zip_path.exists():
                zip_path.unlink()

        # -----------------------------------------------------
        # Disk Auto-Cleanup (if enabled)
        # -----------------------------------------------------
        if auto_clean and album_dir and album_dir.exists():
            try:
                shutil.rmtree(album_dir, ignore_errors=True)
                logger.info(f"Auto-cleaned local download directory: {album_dir}")
            except Exception as e:
                logger.warning(f"Failed to auto-clean {album_dir}: {e}")

    except Exception as e:
        logger.exception("Error during processing:")
        await status_msg.edit(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"❌  **ERROR ENCOUNTERED**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"`{str(e)}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

# ---------------------------------------------------------
# Main Bot Application
# ---------------------------------------------------------
def main():
    print("=" * 65)
    print("🎵 Apple Music 2GB Downloader Bot (Native Windows & MTProto)")
    print("⚡ Max Upload Size: 2,000 MB (2 GB)")
    
    # Check / Auto-start DRM Wrapper
    if ensure_wrapper_running():
        print("✅ DRM Wrapper Status: ONLINE (Port 20020 Reachable)")
    else:
        print("⚠️ DRM Wrapper Status: OFFLINE (Port 20020 unreachable)")
        print("👉 Please run 'start_wrapper.bat' manually if needed.")
    print("=" * 65)

    settings = load_bot_settings()
    api_id = settings["api_id"]
    api_hash = settings["api_hash"]
    bot_token = settings["bot_token"]
    allowed_users = settings["allowed_users"]
    auto_clean = settings["auto_clean"]
    storefront = settings["storefront"]

    if not api_id or not api_hash or not bot_token or bot_token == "your-telegram-bot-token":
        print("\n❌ Error: Telegram credentials not configured!")
        print("Please configure 'telegram-api-id', 'telegram-api-hash', and 'telegram-bot-token'")
        print("in config.yaml or pass them via environment variables.")
        print("=" * 65)
        sys.exit(1)

    if not EXE_PATH.exists():
        print("\n⚠️ am-dl.exe not found! Building from source...")
        try:
            subprocess.run(["go", "build", "-o", "am-dl.exe", "main.go"], cwd=str(BASE_DIR), check=True)
            print("✅ am-dl.exe built successfully!\n")
        except Exception as e:
            print(f"❌ Failed to build am-dl.exe: {e}")
            sys.exit(1)

    client = TelegramClient("am_bot_session", api_id, api_hash)

    @client.on(events.NewMessage(pattern=r"^/start"))
    async def start_handler(event):
        sender = await event.get_sender()
        sender_id = event.sender_id
        username = getattr(sender, "username", "")

        if not is_user_authorized(sender_id, username, allowed_users):
            await event.reply(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⛔  **ACCESS DENIED**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"You are not authorized to use this bot.\n\n"
                f"👤  **Your User ID:** `{sender_id}`\n\n"
                f"Add this ID to `allowed-users` in `config.yaml` to grant access.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            return

        await event.reply(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✦  **APPLE MUSIC STUDIO**  ✦\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Welcome to **Apple Music Lossless Bot**.\n\n"
            f"• **Search Songs**   │ `/search <song name>`\n"
            f"• **Search Albums**  │ `/search album <album name>`\n"
            f"• **Spatial Audio**  │ `/atmos <apple music link>`\n"
            f"• **Quick Download** │ Paste any Apple Music link\n\n"
            f"💎  **Audio Quality** │ 24-bit Studio Lossless ALAC\n"
            f"📦  **Upload Limit**  │ 2,000 MB (2 GB) direct to chat\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Paste an Apple Music link or try `/search` to begin!"
        )

    @client.on(events.NewMessage(pattern=r"^/help"))
    async def help_handler(event):
        await event.reply(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✦  **COMMAND REFERENCE**  ✦\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"• `/search <query>`\n"
            f"  Search songs in Apple Music catalog.\n\n"
            f"• `/search album <name>`\n"
            f"  Search full albums with year and track counts.\n\n"
            f"• `<apple music link>`\n"
            f"  Displays instant Quality Selector buttons.\n\n"
            f"• `/atmos <link>`\n"
            f"  Download Dolby Atmos (Spatial Audio E-AC-3 JOC).\n\n"
            f"• `/album <link>`\n"
            f"  Force-download full album in a packaged `.zip` archive.\n\n"
            f"• `/song <link>`\n"
            f"  Force-download as single playable audio track.\n\n"
            f"📂  **Folder Structure:**\n"
            f"`AM-DL downloads/{{Year}} - {{AlbumName}}/`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    # -----------------------------------------------------
    # /search command handler
    # -----------------------------------------------------
    # -----------------------------------------------------
    @client.on(events.NewMessage(pattern=r"^/search(?:\s+(.+))?$"))
    async def search_handler(event):
        sender = await event.get_sender()
        sender_id = event.sender_id
        username = getattr(sender, "username", "")

        if not is_user_authorized(sender_id, username, allowed_users):
            await event.reply(f"⛔ **Access Denied**: Your User ID: `{sender_id}`")
            return

        raw_query = event.pattern_match.group(1)
        if not raw_query:
            await event.reply(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔍  **SEARCH USAGE**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• **Songs**   │ `/search <song name>`\n"
                f"• **Albums**  │ `/search album <album name>`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            return

        entity = "song"
        query = raw_query.strip()
        if query.lower().startswith("album "):
            entity = "album"
            query = query[6:].strip()
        elif query.lower().startswith("song "):
            entity = "song"
            query = query[5:].strip()

        entity_label = "albums" if entity == "album" else "songs"
        search_msg = await event.reply(f"🔍 Searching Apple Music for `{query}` ({entity_label})...")

        results = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: search_apple_music(query, storefront=storefront, entity=entity, limit=5)
        )

        if not results:
            await search_msg.edit(f"❌ No {entity_label} found for `{query}`.")
            return

        search_id = str(uuid.uuid4())[:8]
        SEARCH_CACHE[search_id] = {
            "query": query,
            "entity": entity,
            "results": results,
        }

        msg_text, buttons = render_search_view(search_id, query, entity, results)
        await search_msg.edit(msg_text, buttons=buttons)

    # -----------------------------------------------------
    # Callback query handler for inline keyboard buttons
    # -----------------------------------------------------
    async def safe_event_edit(event, *args, **kwargs):
        try:
            return await event.edit(*args, **kwargs)
        except MessageNotModifiedError:
            return None
        except Exception as e:
            logger.debug(f"Event edit suppressed: {e}")
            return None

    @client.on(events.CallbackQuery)
    async def callback_handler(event):
        sender = await event.get_sender()
        sender_id = event.sender_id
        username = getattr(sender, "username", "")

        if not is_user_authorized(sender_id, username, allowed_users):
            await event.answer("Access Denied", alert=True)
            return

        data = event.data.decode("utf-8")

        # 1. Toggle between Song Search and Album Search
        if data.startswith("tgl:"):
            _, search_id, target_entity = data.split(":")
            cached = SEARCH_CACHE.get(search_id)
            if not cached:
                await event.answer("Search session expired. Please search again.", alert=True)
                return

            query = cached["query"]
            entity_label = "albums" if target_entity == "album" else "songs"
            await event.answer(f"Fetching {entity_label}...")

            results = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: search_apple_music(query, storefront=storefront, entity=target_entity, limit=5)
            )

            if not results:
                await event.answer(f"No {entity_label} found.", alert=True)
                return

            cached["entity"] = target_entity
            cached["results"] = results

            msg_text, buttons = render_search_view(search_id, query, target_entity, results)
            await safe_event_edit(event, msg_text, buttons=buttons)
            return

        # 2. Select a Search Result
        if data.startswith("sel:"):
            _, search_id, idx_str = data.split(":")
            idx = int(idx_str)
            cached = SEARCH_CACHE.get(search_id)
            if not cached or idx >= len(cached["results"]):
                await event.answer("Search item expired. Please search again.", alert=True)
                return

            item = cached["results"][idx]
            job_id = str(uuid.uuid4())[:8]
            PENDING_JOBS[job_id] = {
                "track_name": item["track_name"],
                "artist_name": item["artist_name"],
                "album_name": item["album_name"],
                "year": item.get("year", ""),
                "track_url": item["track_url"],
                "album_url": clean_album_url(item["album_url"] or item["track_url"]),
            }

            # If selected item is an ALBUM
            if item.get("is_album"):
                buttons = [
                    [
                        Button.inline("💎 24-bit Lossless Album (.zip)", data=f"dl:album:{job_id}".encode()),
                        Button.inline("🌌 Dolby Atmos Album (.zip)", data=f"dl:atmos_album:{job_id}".encode()),
                    ]
                ]
                year_str = f" ({item['year']})" if item.get("year") else ""
                await safe_event_edit(
                    event,
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✦  **ALBUM SELECTED**  ✦\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💿  **Album**    │ `{item['album_name']}{year_str}`\n"
                    f"👤  **Artist**   │ `{item['artist_name']}`\n"
                    f"📊  **Tracks**   │ `{item.get('track_count', 0)} Songs`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👇 *Choose download format for this entire album:*",
                    buttons=buttons
                )
            # If selected item is a SONG
            else:
                buttons = [
                    [
                        Button.inline("💎 24-bit Lossless ALAC", data=f"dl:alac:{job_id}".encode()),
                        Button.inline("🌌 Dolby Atmos", data=f"dl:atmos:{job_id}".encode()),
                    ],
                    [
                        Button.inline(f"📦 Download Full Album (.zip)", data=f"dl:album:{job_id}".encode()),
                    ]
                ]
                await safe_event_edit(
                    event,
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✦  **TRACK SELECTED**  ✦\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎵  **Track**    │ `{item['track_name']}`\n"
                    f"👤  **Artist**   │ `{item['artist_name']}`\n"
                    f"💿  **Album**    │ `{item['album_name']}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👇 *Choose download format:*",
                    buttons=buttons
                )
            return

        # 3. Handle Download Format Selection
        if data.startswith("dl:"):
            parts = data.split(":")
            mode = parts[1]
            job_id = parts[2]

            job = PENDING_JOBS.get(job_id)
            if not job:
                await event.answer("Job session expired. Please re-send link or search again.", alert=True)
                return

            is_playlist = job.get("is_playlist", False)
            if mode == "alac":
                url = job["track_url"]
                is_single_song = True
                dl_atmos = False
            elif mode == "atmos":
                url = job["track_url"]
                is_single_song = True
                dl_atmos = True
            elif mode == "playlist":
                url = job["playlist_url"]
                is_single_song = False
                dl_atmos = False
                is_playlist = True
            elif mode == "atmos_playlist":
                url = job["playlist_url"]
                is_single_song = False
                dl_atmos = True
                is_playlist = True
            elif mode == "atmos_album":
                url = clean_album_url(job["album_url"])
                is_single_song = False
                dl_atmos = True
            else:  # mode == "album"
                url = clean_album_url(job["album_url"])
                is_single_song = False
                dl_atmos = False

            status_msg = await event.get_message()
            await event.answer("Starting download...")

            await execute_download(
                client=client,
                chat_id=event.chat_id,
                status_msg=status_msg,
                url=url,
                is_single_song=is_single_song,
                dl_atmos=dl_atmos,
                auto_clean=auto_clean,
                is_playlist=is_playlist,
            )
            return

    # -----------------------------------------------------
    # Raw Message handler (links and text commands)
    # -----------------------------------------------------
    @client.on(events.NewMessage)
    async def message_handler(event):
        text = event.text.strip()
        if text.startswith(("/start", "/help", "/search")):
            return

        sender = await event.get_sender()
        sender_id = event.sender_id
        username = getattr(sender, "username", "")

        # Whitelist Security Check
        if not is_user_authorized(sender_id, username, allowed_users):
            await event.reply(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⛔  **ACCESS DENIED**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"You are not authorized to use this bot.\n\n"
                f"👤  **Your User ID:** `{sender_id}`\n\n"
                f"Add this ID to `allowed-users` in `config.yaml` to grant access.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
            return

        url = extract_url(text)
        if not url:
            return

        # Explicit commands
        dl_atmos = bool(re.search(r"^/(?:atmos|spatial)\b", text, re.IGNORECASE) or "--atmos" in text)
        force_album = bool(re.search(r"^/album\b", text, re.IGNORECASE) or "--album" in text)
        force_song = bool(re.search(r"^/song\b", text, re.IGNORECASE) or "--song" in text)

        if dl_atmos or force_album or force_song:
            if is_playlist_url(url):
                status_msg = await event.reply("⏳ Initializing Playlist Download...")
                await execute_download(
                    client=client,
                    chat_id=event.chat_id,
                    status_msg=status_msg,
                    url=url,
                    is_single_song=False,
                    dl_atmos=dl_atmos,
                    auto_clean=auto_clean,
                    is_playlist=True,
                )
                return

            is_single_song = force_song or (not force_album and ("?i=" in url or "/song/" in url))
            target_url = url if is_single_song else clean_album_url(url)
            status_msg = await event.reply("⏳ Initializing...")
            await execute_download(
                client=client,
                chat_id=event.chat_id,
                status_msg=status_msg,
                url=target_url,
                is_single_song=is_single_song,
                dl_atmos=dl_atmos,
                auto_clean=auto_clean,
            )
            return

        # 1. Playlist URL Handling
        if is_playlist_url(url):
            info = await asyncio.get_running_loop().run_in_executor(
                None, lambda: fetch_playlist_info(url)
            )
            job_id = str(uuid.uuid4())[:8]
            PENDING_JOBS[job_id] = {
                "playlist_url": url,
                "is_playlist": True,
                "title": info["title"],
            }
            buttons = [
                [
                    Button.inline("💎 24-bit Lossless Playlist (.zip)", data=f"dl:playlist:{job_id}".encode()),
                    Button.inline("🌌 Dolby Atmos Playlist (.zip)", data=f"dl:atmos_playlist:{job_id}".encode()),
                ]
            ]
            desc_line = f"📝  **Info**       │ `{info['description']}`\n" if info.get("description") else ""
            await event.reply(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✦  **PLAYLIST DETECTED**  ✦\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📜  **Playlist**   │ `{info['title']}`\n"
                f"{desc_line}"
                f"🔗  **URL**        │ [Open in Apple Music]({url})\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👇 *Choose download format for this entire playlist:*",
                buttons=buttons,
                link_preview=False
            )
            return

        # 2. Interactive Quality Selector Buttons for Songs & Albums
        has_song_id = ("?i=" in url or "/song/" in url)
        job_id = str(uuid.uuid4())[:8]
        PENDING_JOBS[job_id] = {
            "track_url": url,
            "album_url": clean_album_url(url),
        }

        if has_song_id:
            buttons = [
                [
                    Button.inline("💎 24-bit Lossless ALAC", data=f"dl:alac:{job_id}".encode()),
                    Button.inline("🌌 Dolby Atmos", data=f"dl:atmos:{job_id}".encode()),
                ],
                [
                    Button.inline("📦 Download Full Album (.zip)", data=f"dl:album:{job_id}".encode()),
                ]
            ]
            await event.reply(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✦  **TRACK LINK DETECTED**  ✦\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👇 *Choose your desired audio format:*",
                buttons=buttons
            )
        else:
            buttons = [
                [
                    Button.inline("💎 24-bit Lossless Album (.zip)", data=f"dl:album:{job_id}".encode()),
                    Button.inline("🌌 Dolby Atmos Album (.zip)", data=f"dl:atmos_album:{job_id}".encode()),
                ]
            ]
            await event.reply(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✦  **ALBUM LINK DETECTED**  ✦\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👇 *Choose your desired audio format:*",
                buttons=buttons
            )

    print("✅ 2GB Telegram Bot is online and listening for Apple Music links!")
    client.start(bot_token=bot_token)
    client.run_until_disconnected()

if __name__ == "__main__":
    main()
