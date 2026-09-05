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
EXE_PATH = BASE_DIR / ("am-dl" if sys.platform != "win32" else "am-dl.exe")
CONFIG_PATH = BASE_DIR / "config.yaml"
ENV_PATH = BASE_DIR / ".env"

# Prepend bin/ directory to system PATH so MP4Box and FFmpeg are always found
if BIN_DIR.exists():
    os.environ["PATH"] = str(BIN_DIR) + os.pathsep + os.environ.get("PATH", "")

# In-memory caches for interactive buttons
PENDING_JOBS = {}
SEARCH_CACHE = {}
DOWNLOAD_LOCK = None

def get_download_lock() -> asyncio.Lock:
    global DOWNLOAD_LOCK
    if DOWNLOAD_LOCK is None:
        DOWNLOAD_LOCK = asyncio.Lock()
    return DOWNLOAD_LOCK

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
    api_id = api_id or 0
    api_hash = str(api_hash or "").strip()
    bot_token = str(bot_token or "").strip()

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
    """Checks if wrapper is alive. If not, auto-starts it with auto-restart loop natively or in WSL."""
    if is_wrapper_running():
        return True

    logger.info("DRM Wrapper is offline. Auto-starting persistent wrapper daemon...")
    try:
        if sys.platform != "win32" and (BASE_DIR / "start_wrapper.sh").exists():
            cmd = ["bash", str(BASE_DIR / "start_wrapper.sh")]
        elif sys.platform == "win32":
            wrapper_sh = (
                "if [ ! -f ~/wrapper/wrapper ]; then "
                "mkdir -p ~/wrapper && cd ~/wrapper && "
                "curl -fL 'https://github.com/WorldObservationLog/wrapper/releases/download/wrapper.x86_64.latest/Wrapper.x86_64.latest.zip' -o Wrapper.zip && "
                "unzip -o Wrapper.zip && chmod +x wrapper; "
                "fi; "
                "cd ~/wrapper && while true; do ./wrapper; sleep 2; done"
            )
            cmd = ["wsl", "-e", "bash", "-c", wrapper_sh]
        else:
            wrapper_sh = (
                "if [ ! -f ~/wrapper/wrapper ]; then "
                "mkdir -p ~/wrapper && cd ~/wrapper && "
                "curl -fL 'https://github.com/WorldObservationLog/wrapper/releases/download/wrapper.x86_64.latest/Wrapper.x86_64.latest.zip' -o Wrapper.zip && "
                "unzip -o Wrapper.zip && chmod +x wrapper; "
                "fi; "
                "cd ~/wrapper && while true; do ./wrapper; sleep 2; done"
            )
            cmd = ["bash", "-c", wrapper_sh]

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
        logger.error(f"Failed to auto-start wrapper: {e}")

    return is_wrapper_running()

def inspect_audio_quality(track_path: Path) -> dict:
    """Inspects the downloaded M4A file via ffmpeg to extract true bit depth, sample rate, and bitrate."""
    fallback = {"display": "24-bit Lossless ALAC"}
    ffmpeg_exe = BIN_DIR / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
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
                display = f"{bit_depth} / {rate_khz} {tier} ALAC - {bitrate_kbs} kbps"
            elif "eac3" in codec_raw:
                display = f"Dolby Atmos Spatial Audio {rate_khz} - {bitrate_kbs} kbps"
            elif "aac" in codec_raw:
                display = f"AAC {bit_depth} / {rate_khz} - {bitrate_kbs} kbps"
            else:
                display = f"{codec_raw.upper()} {bit_depth} / {rate_khz} - {bitrate_kbs} kbps"

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

def strip_brackets(text: str) -> str:
    """Removes parenthetical substrings like (Original Background Score) and brackets."""
    if not text:
        return ""
    cleaned = re.sub(r"\s*\([^)]*\)", "", text)
    cleaned = re.sub(r"\s*\[[^\]]*\]", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned if cleaned else text

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
            title = strip_brackets(title)

            desc = html.unescape(desc_m.group(1).strip()) if desc_m else "Curated Playlist"
            desc = strip_brackets(desc)
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
                    "track_name": strip_brackets(r.get("trackName") or r.get("collectionName", "Unknown Track")),
                    "artist_name": r.get("artistName", "Unknown Artist"),
                    "album_name": strip_brackets(r.get("collectionName", "Unknown Album")),
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
    type_str = "Albums" if is_album_search else "Songs"
    lines = [
        f"**Search Results for \"{query}\" ({type_str})**\n"
    ]
    buttons = []

    for i, item in enumerate(results):
        num = i + 1
        if is_album_search:
            year_suffix = f" - {item['year']}" if item.get("year") else ""
            lines.append(f"**{num}. {item['album_name']}{year_suffix}**")
            lines.append(f"   {item['artist_name']} - {item['track_count']} tracks\n")
            btn_title = f"{num}. {item['album_name']}{year_suffix}"
        else:
            lines.append(f"**{num}. {item['track_name']}**")
            lines.append(f"   {item['artist_name']} - {item['album_name']}\n")
            btn_title = f"{num}. {item['track_name']}"

        if len(btn_title) > 34:
            btn_title = btn_title[:31] + "..."
        buttons.append([Button.inline(btn_title, data=f"sel:{search_id}:{i}".encode())])

    # Add toggle button between Songs and Albums
    if is_album_search:
        buttons.append([Button.inline("Switch to Songs Search", data=f"tgl:{search_id}:song".encode())])
    else:
        buttons.append([Button.inline("Switch to Albums Search", data=f"tgl:{search_id}:album".encode())])

    lines.append("Select an item to choose download format:")
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

def extract_release_year(track_path: Path) -> str:
    """Extracts 4-digit release year from audio file metadata via ffmpeg."""
    if not track_path or not track_path.exists():
        return ""
    ffmpeg_exe = BIN_DIR / "ffmpeg.exe"
    if not ffmpeg_exe.exists():
        ffmpeg_exe = Path("ffmpeg")
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
        m_date = re.search(r'^\s*date\s*:\s*(\d{4})', output, re.IGNORECASE | re.MULTILINE)
        if not m_date:
            m_date = re.search(r'^\s*releasetime\s*:\s*(\d{4})', output, re.IGNORECASE | re.MULTILINE)
        if not m_date:
            m_date = re.search(r'\b(19\d{2}|20\d{2})\b', output)
        return m_date.group(1) if m_date else ""
    except Exception:
        return ""

def zip_entire_album(audio_files: list, cover_path: Path, archive_name: str, is_playlist: bool = False) -> Path:
    """Creates a single .zip containing all album tracks and cover.jpg formatted as 'Year - Album Name.zip'."""
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    clean_name = strip_brackets(archive_name)

    # Format as 'Year - Album Name' if not already starting with a 4-digit year and not a playlist
    if not is_playlist and not re.match(r"^\d{4}\s*[-_]", clean_name):
        first_track = audio_files[0]["path"] if audio_files else None
        year = extract_release_year(first_track) if first_track else ""
        if year:
            clean_name = f"{year} - {clean_name}"

    safe_name = re.sub(r'[\\/*?:"<>|]', "_", clean_name)
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
async def execute_download(client, chat_id, status_msg, url: str, is_single_song: bool, dl_atmos: bool, auto_clean: bool, is_playlist: bool = False, send_as_zip: bool = True):
    """Orchestrates am-dl download, real-time status updates, and Telegram upload with sequential lock."""
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

    lock = get_download_lock()
    if lock.locked():
        await status_msg.edit(
            "⏳ **Queued**\n\n"
            "Another download is currently in progress.\n"
            "Your request is queued and will start automatically once the active download completes."
        )

    async with lock:
        try:
            ensure_wrapper_running()
            wrapper_warning = "\n(DRM Wrapper offline on port 20020)" if not is_wrapper_running() else ""

            # If full album, ensure any ?i= is stripped so am-dl downloads all tracks
            if not is_single_song and not is_playlist:
                url = clean_album_url(url)

            mode_name = "Dolby Atmos" if dl_atmos else "24-bit Lossless ALAC"
            if is_playlist:
                target_type = "Full Playlist"
            else:
                target_type = "Single Track" if is_single_song else "Full Album"

            delivery_label = ""
            if not is_single_song:
                delivery_label = " (ZIP Archive)" if send_as_zip else " (Individual Tracks)"

            await status_msg.edit(
                f"**Initializing Download**\n\n"
                f"Target: {target_type}{delivery_label}\n"
                f"Quality: {mode_name}\n"
                f"Status: Connecting to Apple Music API...{wrapper_warning}",
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
                            f"**Downloading**\n\n"
                            f"Track: {current_song_name}\n"
                            f"Progress: {current_track_num}\n"
                            f"Status: {line}\n"
                            f"Quality: {mode_name}"
                        )
                        await update_status_live(msg_text)
                    elif "Decrypted" in line or "Downloaded" in line:
                        msg_text = (
                            f"**Downloading**\n\n"
                            f"Track: {current_song_name}\n"
                            f"Progress: {current_track_num}\n"
                            f"Status: {line}\n"
                            f"Quality: {mode_name}"
                        )
                        await update_status_live(msg_text)
                    elif "Track already exists locally" in line:
                        msg_text = (
                            f"**Downloading**\n\n"
                            f"Track: {current_song_name}\n"
                            f"Progress: {current_track_num}\n"
                            f"Status: Cached on disk\n"
                            f"Quality: {mode_name}"
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
                    "**Download Failed**\n\n"
                    "No audio files were found. Please verify that the DRM wrapper is running."
                )
                return

            album_dir = audio_files[0]["path"].parent
            folder_display_name = strip_brackets(album_dir.name)
            for trk in audio_files:
                trk["title"] = strip_brackets(trk.get("title", ""))
                trk["album"] = strip_brackets(trk.get("album", ""))

            cover_path = album_dir / "cover.jpg"
            if not cover_path.exists():
                cover_path = None

            # Inspect downloaded file to extract true bit depth, sample rate, and bitrate
            quality_info = inspect_audio_quality(audio_files[0]["path"])
            exact_quality_str = quality_info.get("display", mode_name)
            item_type = "Playlist" if is_playlist else "Album"

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
                        f"**Track Stored Locally**\n\n"
                        f"Title: {track['title']}\n"
                        f"Artist: {track['artist']}\n"
                        f"Album: {track['album']}\n"
                        f"Size: {trk_size_mb / 1024:.2f} GB\n"
                        f"Quality: {exact_quality_str}\n\n"
                        f"File exceeds Telegram's 2,000 MB upload limit.\n"
                        f"Saved locally to:\n"
                        f"`{trk_path}`"
                    )
                    return

                await status_msg.edit(
                    f"**Uploading to Telegram**\n\n"
                    f"Track: {track['title']}\n"
                    f"Artist: {track['artist']}\n"
                    f"Size: {trk_size_mb:.1f} MB\n"
                    f"Quality: {exact_quality_str}"
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
                                f"**Uploading to Telegram**\n\n"
                                f"Track: {trk_path.name}\n"
                                f"Progress: {pct:.1f}% ({current / (1024*1024):.1f} MB / {total / (1024*1024):.1f} MB)"
                            )
                        except Exception:
                            pass

                # Send directly as playable Telegram Audio
                track_caption = (
                    f"**{track['title']}**\n"
                    f"{track['artist']} - {track['album']}\n\n"
                    f"Quality: {exact_quality_str}\n"
                    f"Size: {trk_size_mb:.1f} MB"
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
                    f"Track  : {track['title']}\n"
                    f"Artist : {track['artist']}\n"
                    f"Album  : {track['album']}\n"
                    f"Size   : {trk_size_mb:.1f} MB"
                )

            # -----------------------------------------------------
            # Delivery Mode 2: Full Album / Playlist as Individual Tracks
            # -----------------------------------------------------
            elif not send_as_zip:
                total_tracks = len(audio_files)
                album_artist = audio_files[0]["artist"]

                await status_msg.edit(
                    f"**Uploading {item_type} Tracks**\n\n"
                    f"{item_type}: {folder_display_name}\n"
                    f"Artist: {album_artist}\n"
                    f"Tracks: {total_tracks} tracks\n"
                    f"Quality: {exact_quality_str}\n"
                    f"Status: Sending tracks individually..."
                )

                uploaded_count = 0
                for idx, trk in enumerate(audio_files, 1):
                    trk_path = trk["path"]
                    trk_size_mb = trk_path.stat().st_size / (1024 * 1024)

                    if trk_size_mb >= 2000:
                        await client.send_message(
                            chat_id,
                            f"⚠️ **Track Exceeds Limit**: {trk['title']} ({trk_size_mb:.1f} MB) exceeds Telegram's 2,000 MB limit."
                        )
                        continue

                    await status_msg.edit(
                        f"**Uploading {item_type} ({idx}/{total_tracks})**\n\n"
                        f"Track: {trk['title']}\n"
                        f"Artist: {trk['artist']}\n"
                        f"Size: {trk_size_mb:.1f} MB\n"
                        f"Quality: {exact_quality_str}"
                    )

                    track_caption = (
                        f"**{trk['title']}**\n"
                        f"{trk['artist']} - {trk['album']}\n\n"
                        f"Quality: {exact_quality_str}\n"
                        f"Track {idx} of {total_tracks}"
                    )

                    await client.send_file(
                        chat_id,
                        trk_path,
                        caption=track_caption,
                        thumb=cover_path if cover_path else None,
                        attributes=[
                            DocumentAttributeFilename(trk_path.name),
                            DocumentAttributeAudio(
                                duration=0,
                                title=trk["title"],
                                performer=trk["artist"],
                            ),
                        ],
                    )
                    uploaded_count += 1
                    if idx < total_tracks:
                        await asyncio.sleep(0.5)

                key_label = "Playlist" if is_playlist else "Album "
                await status_msg.edit(
                    f"✅ **{key_label} Completed**\n\n"
                    f"{key_label} : {folder_display_name}\n"
                    f"Artist : {album_artist}\n"
                    f"Delivered: {uploaded_count}/{total_tracks} tracks\n"
                    f"Quality: {exact_quality_str}"
                )

            # -----------------------------------------------------
            # Delivery Mode 3: Full Album / Playlist (.zip archive)
            # -----------------------------------------------------
            else:
                total_tracks = len(audio_files)
                album_artist = audio_files[0]["artist"]
                total_size_mb = sum(t["path"].stat().st_size for t in audio_files) / (1024 * 1024)

                # Check if total album/playlist exceeds Telegram's 2,000 MB limit
                if total_size_mb >= 2000:
                    await status_msg.edit(
                        f"**{item_type} Stored Locally**\n\n"
                        f"{item_type}: {folder_display_name}\n"
                        f"Artist: {album_artist}\n"
                        f"Tracks: {total_tracks} tracks\n"
                        f"Size: {total_size_mb / 1024:.2f} GB\n"
                        f"Quality: {exact_quality_str}\n\n"
                        f"Archive exceeds Telegram's 2,000 MB limit.\n"
                        f"💡 Tip: Download as individual tracks to bypass the 2 GB archive limit.\n"
                        f"Saved locally to:\n"
                        f"`{album_dir}`"
                    )
                    return

                await status_msg.edit(
                    f"**Packaging {item_type}**\n\n"
                    f"{item_type}: {folder_display_name}\n"
                    f"Tracks: {total_tracks} tracks\n"
                    f"Status: Creating zip archive..."
                )

                loop = asyncio.get_running_loop()
                zip_path = await loop.run_in_executor(
                    None,
                    lambda: zip_entire_album(audio_files, cover_path, folder_display_name, is_playlist=is_playlist)
                )

                zip_size_mb = zip_path.stat().st_size / (1024 * 1024)

                # Check if created zip archive exceeds 2,000 MB limit
                if zip_size_mb >= 2000:
                    if zip_path.exists():
                        zip_path.unlink()
                    await status_msg.edit(
                        f"**{item_type} Stored Locally**\n\n"
                        f"{item_type}: {folder_display_name}\n"
                        f"Artist: {album_artist}\n"
                        f"Tracks: {total_tracks} tracks\n"
                        f"Size: {zip_size_mb / 1024:.2f} GB\n"
                        f"Quality: {exact_quality_str}\n\n"
                        f"Archive exceeds Telegram's 2,000 MB limit.\n"
                        f"💡 Tip: Download as individual tracks to bypass the 2 GB archive limit.\n"
                        f"Saved locally to:\n"
                        f"`{album_dir}`"
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
                                f"**Uploading to Telegram**\n\n"
                                f"Archive: {zip_path.name}\n"
                                f"Progress: {pct:.1f}% ({current / (1024*1024):.1f} MB / {total / (1024*1024):.1f} MB)"
                            )
                        except Exception:
                            pass

                # Upload single zip directly over MTProto
                album_caption = (
                    f"**{folder_display_name}**\n"
                    f"{album_artist}\n\n"
                    f"Tracks: {total_tracks} tracks\n"
                    f"Quality: {exact_quality_str}\n"
                    f"Archive: {zip_size_mb:.1f} MB"
                )

                await client.send_file(
                    chat_id,
                    zip_path,
                    caption=album_caption,
                    progress_callback=upload_progress_callback,
                    attributes=[DocumentAttributeFilename(zip_path.name)],
                )

                key_label = "Playlist" if is_playlist else "Album "
                await status_msg.edit(
                    f"{key_label} : {folder_display_name}\n"
                    f"Artist : {album_artist}\n"
                    f"Tracks : {total_tracks} tracks\n"
                    f"Size   : {zip_size_mb:.1f} MB"
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
                    parent_dir = album_dir.parent
                    if parent_dir != DOWNLOADS_DIR and parent_dir.exists() and not any(parent_dir.iterdir()):
                        parent_dir.rmdir()
                except Exception as e:
                    logger.warning(f"Failed to auto-clean {album_dir}: {e}")

        except Exception as e:
            logger.exception("Error during processing:")
            await status_msg.edit(
                f"**Error**\n\n"
                f"{str(e)}"
            )

# ---------------------------------------------------------
# Main Bot Application
# ---------------------------------------------------------
def main():
    print("=" * 65)
    print("Apple Music Downloader Bot (Native Windows & MTProto)")
    print("Max Upload Size: 2,000 MB (2 GB)")
    
    # Check / Auto-start DRM Wrapper
    if ensure_wrapper_running():
        print("[+] DRM Wrapper Status: ONLINE (Port 20020 Reachable)")
    else:
        print("[!] DRM Wrapper Status: OFFLINE (Port 20020 unreachable)")
        print("[i] Run 'start_wrapper.bat' if needed.")
    print("=" * 65)

    settings = load_bot_settings()
    api_id = settings["api_id"]
    api_hash = settings["api_hash"]
    bot_token = settings["bot_token"]
    allowed_users = settings["allowed_users"]
    auto_clean = settings["auto_clean"]
    storefront = settings["storefront"]

    if not api_id or not api_hash or not bot_token or bot_token == "your-telegram-bot-token":
        print("\n[!] Error: Telegram credentials not configured!")
        print("Please configure 'telegram-api-id', 'telegram-api-hash', and 'telegram-bot-token'")
        print("in config.yaml or pass them via environment variables.")
        print("=" * 65)
        sys.exit(1)

    if not EXE_PATH.exists():
        print(f"\n[!] {EXE_PATH.name} not found! Building from source...")
        try:
            subprocess.run(["go", "build", "-o", EXE_PATH.name, "main.go"], cwd=str(BASE_DIR), check=True)
            print(f"[+] {EXE_PATH.name} built successfully!\n")
        except Exception as e:
            print(f"[!] Failed to build {EXE_PATH.name}: {e}")
            sys.exit(1)

    client = TelegramClient("am_bot_session", api_id, api_hash)

    @client.on(events.NewMessage(pattern=r"^/start"))
    async def start_handler(event):
        sender = await event.get_sender()
        sender_id = event.sender_id
        username = getattr(sender, "username", "")

        if not is_user_authorized(sender_id, username, allowed_users):
            await event.reply(
                f"**Access Denied**\n\n"
                f"Your User ID: `{sender_id}`\n"
                f"Add this ID to `allowed-users` in `config.yaml` to grant access."
            )
            return

        await event.reply(
            "**Apple Music Downloader**\n\n"
            "Download bit-perfect lossless audio and full albums from Apple Music.\n\n"
            "• Search songs: `/search <song name>`\n"
            "• Search albums: `/search album <album name>`\n"
            "• Dolby Atmos: `/atmos <apple music link>`\n"
            "• Direct download: Paste any Apple Music link\n\n"
            "Audio Quality: 24-bit Lossless ALAC / Dolby Atmos\n"
            "Upload Limit: 2,000 MB (2 GB) direct to chat\n\n"
            "Paste a link or type `/search` to begin."
        )

    @client.on(events.NewMessage(pattern=r"^/help"))
    async def help_handler(event):
        await event.reply(
            "**Commands & Usage**\n\n"
            "• `/search <query>`\n"
            "  Search songs in the Apple Music catalog.\n\n"
            "• `/search album <name>`\n"
            "  Search albums with track count and release year.\n\n"
            "• `<apple music link>`\n"
            "  Paste any track, album, or playlist link to choose formats:\n"
            "  - 📦 ZIP archive (single file)\n"
            "  - 🎵 Individual audio tracks\n\n"
            "• `/atmos <link>`\n"
            "  Download Dolby Atmos (Spatial Audio E-AC-3 JOC).\n\n"
            "• `/album <link> [--tracks]`\n"
            "  Download full album (.zip by default, or `--tracks` for individual files).\n\n"
            "• `/song <link>`\n"
            "  Download as a single playable audio track."
        )

    # -----------------------------------------------------
    # /search command handler
    # -----------------------------------------------------
    @client.on(events.NewMessage(pattern=r"^/search(?:\s+(.+))?$"))
    async def search_handler(event):
        sender = await event.get_sender()
        sender_id = event.sender_id
        username = getattr(sender, "username", "")

        if not is_user_authorized(sender_id, username, allowed_users):
            await event.reply(f"**Access Denied**: Your User ID: `{sender_id}`")
            return

        raw_query = event.pattern_match.group(1)
        if not raw_query:
            await event.reply(
                "**Search Usage**\n\n"
                "• `/search <song name>`\n"
                "• `/search album <album name>`"
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
        search_msg = await event.reply(f"Searching Apple Music for \"{query}\" ({entity_label})...")

        results = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: search_apple_music(query, storefront=storefront, entity=entity, limit=5)
        )

        if not results:
            await search_msg.edit(f"No {entity_label} found for \"{query}\".")
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
                        Button.inline("📦 Lossless (ZIP)", data=f"dl:album_zip:{job_id}".encode()),
                        Button.inline("🎵 Lossless (Tracks)", data=f"dl:album_tracks:{job_id}".encode()),
                    ],
                    [
                        Button.inline("📦 Atmos (ZIP)", data=f"dl:atmos_zip:{job_id}".encode()),
                        Button.inline("🎵 Atmos (Tracks)", data=f"dl:atmos_tracks:{job_id}".encode()),
                    ],
                ]
                year_str = f" - {item['year']}" if item.get("year") else ""
                await safe_event_edit(
                    event,
                    f"**Album Selected**\n\n"
                    f"Album: {item['album_name']}{year_str}\n"
                    f"Artist: {item['artist_name']}\n"
                    f"Tracks: {item.get('track_count', 0)} tracks\n\n"
                    f"Choose download format & delivery:",
                    buttons=buttons
                )
            # If selected item is a SONG
            else:
                buttons = [
                    [
                        Button.inline("Lossless ALAC", data=f"dl:alac:{job_id}".encode()),
                        Button.inline("Dolby Atmos", data=f"dl:atmos:{job_id}".encode()),
                    ],
                    [
                        Button.inline("📦 Album (ZIP)", data=f"dl:album_zip:{job_id}".encode()),
                        Button.inline("🎵 Album (Tracks)", data=f"dl:album_tracks:{job_id}".encode()),
                    ],
                ]
                await safe_event_edit(
                    event,
                    f"**Track Selected**\n\n"
                    f"Title: {item['track_name']}\n"
                    f"Artist: {item['artist_name']}\n"
                    f"Album: {item['album_name']}\n\n"
                    f"Choose download format:",
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
            send_as_zip = True

            if mode == "alac":
                url = job["track_url"]
                is_single_song = True
                dl_atmos = False
                send_as_zip = False
            elif mode == "atmos":
                url = job["track_url"]
                is_single_song = True
                dl_atmos = True
                send_as_zip = False
            elif mode in ("playlist_zip", "playlist"):
                url = job["playlist_url"]
                is_single_song = False
                dl_atmos = False
                is_playlist = True
                send_as_zip = True
            elif mode == "playlist_tracks":
                url = job["playlist_url"]
                is_single_song = False
                dl_atmos = False
                is_playlist = True
                send_as_zip = False
            elif mode in ("atmos_playlist_zip", "atmos_playlist"):
                url = job["playlist_url"]
                is_single_song = False
                dl_atmos = True
                is_playlist = True
                send_as_zip = True
            elif mode == "atmos_playlist_tracks":
                url = job["playlist_url"]
                is_single_song = False
                dl_atmos = True
                is_playlist = True
                send_as_zip = False
            elif mode in ("atmos_album_zip", "atmos_zip", "atmos_album"):
                url = clean_album_url(job["album_url"])
                is_single_song = False
                dl_atmos = True
                send_as_zip = True
            elif mode in ("atmos_album_tracks", "atmos_tracks"):
                url = clean_album_url(job["album_url"])
                is_single_song = False
                dl_atmos = True
                send_as_zip = False
            elif mode == "album_tracks":
                url = clean_album_url(job["album_url"])
                is_single_song = False
                dl_atmos = False
                send_as_zip = False
            else:  # mode in ("album_zip", "album")
                url = clean_album_url(job["album_url"])
                is_single_song = False
                dl_atmos = False
                send_as_zip = True

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
                send_as_zip=send_as_zip,
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
                f"**Access Denied**\n\n"
                f"Your User ID: `{sender_id}`\n"
                f"Add this ID to `allowed-users` in `config.yaml` to grant access."
            )
            return

        url = extract_url(text)
        if not url:
            return

        # Explicit commands
        dl_atmos = bool(re.search(r"^/(?:atmos|spatial)\b", text, re.IGNORECASE) or "--atmos" in text)
        force_album = bool(re.search(r"^/album\b", text, re.IGNORECASE) or "--album" in text)
        force_song = bool(re.search(r"^/song\b", text, re.IGNORECASE) or "--song" in text)
        force_tracks = bool("--tracks" in text)

        if dl_atmos or force_album or force_song:
            send_as_zip = not force_tracks
            if is_playlist_url(url):
                status_msg = await event.reply("Initializing playlist download...")
                await execute_download(
                    client=client,
                    chat_id=event.chat_id,
                    status_msg=status_msg,
                    url=url,
                    is_single_song=False,
                    dl_atmos=dl_atmos,
                    auto_clean=auto_clean,
                    is_playlist=True,
                    send_as_zip=send_as_zip,
                )
                return

            is_single_song = force_song or (not force_album and ("?i=" in url or "/song/" in url))
            target_url = url if is_single_song else clean_album_url(url)
            status_msg = await event.reply("Initializing download...")
            await execute_download(
                client=client,
                chat_id=event.chat_id,
                status_msg=status_msg,
                url=target_url,
                is_single_song=is_single_song,
                dl_atmos=dl_atmos,
                auto_clean=auto_clean,
                send_as_zip=send_as_zip,
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
                    Button.inline("📦 Lossless (ZIP)", data=f"dl:playlist_zip:{job_id}".encode()),
                    Button.inline("🎵 Lossless (Tracks)", data=f"dl:playlist_tracks:{job_id}".encode()),
                ],
                [
                    Button.inline("📦 Atmos (ZIP)", data=f"dl:atmos_playlist_zip:{job_id}".encode()),
                    Button.inline("🎵 Atmos (Tracks)", data=f"dl:atmos_playlist_tracks:{job_id}".encode()),
                ],
            ]
            desc_line = f"Details: {info['description']}\n" if info.get("description") else ""
            await event.reply(
                f"**Playlist Detected**\n\n"
                f"Playlist: {info['title']}\n"
                f"{desc_line}"
                f"URL: {url}\n\n"
                f"Choose download format & delivery:",
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
                    Button.inline("Lossless ALAC", data=f"dl:alac:{job_id}".encode()),
                    Button.inline("Dolby Atmos", data=f"dl:atmos:{job_id}".encode()),
                ],
                [
                    Button.inline("📦 Album (ZIP)", data=f"dl:album_zip:{job_id}".encode()),
                    Button.inline("🎵 Album (Tracks)", data=f"dl:album_tracks:{job_id}".encode()),
                ],
            ]
            await event.reply(
                f"**Track Link Detected**\n\n"
                f"URL: {url}\n\n"
                f"Choose download format:",
                buttons=buttons,
                link_preview=False
            )
        else:
            buttons = [
                [
                    Button.inline("📦 Lossless (ZIP)", data=f"dl:album_zip:{job_id}".encode()),
                    Button.inline("🎵 Lossless (Tracks)", data=f"dl:album_tracks:{job_id}".encode()),
                ],
                [
                    Button.inline("📦 Atmos (ZIP)", data=f"dl:atmos_zip:{job_id}".encode()),
                    Button.inline("🎵 Atmos (Tracks)", data=f"dl:atmos_tracks:{job_id}".encode()),
                ],
            ]
            await event.reply(
                f"**Album Link Detected**\n\n"
                f"URL: {url}\n\n"
                f"Choose download format & delivery:",
                buttons=buttons,
                link_preview=False
            )

    print("[+] Telegram Bot is online and listening for Apple Music links.")
    client.start(bot_token=bot_token)
    client.run_until_disconnected()

if __name__ == "__main__":
    main()
