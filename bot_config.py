#!/usr/bin/env python3
"""
================================================================================
🎬 Auto DL & Multi-Channel Upload Configuration
================================================================================
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / "config.env"

def _load_env_file(filepath: Path):
    """Fallback manual .env loader if python-dotenv is not installed."""
    if not filepath.exists():
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("\"'").replace("\\n", "\n")
                if k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass

# Load env variables
_load_env_file(ENV_FILE)

# Telegram Bot Credentials
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

# Bots
MAIN_BOT_TOKEN = os.environ.get("MAIN_BOT_TOKEN", "").strip()
MAIN_BOT_NAME = os.environ.get("MAIN_BOT_NAME", "DramaAutoDLBot").strip()

HELPER_BOT_TOKEN = os.environ.get("HELPER_BOT_TOKEN", "").strip()
HELPER_BOT_NAME = os.environ.get("HELPER_BOT_NAME", "DramaAutoDLBot").strip()

# User 4GB String Session
USER_SESSION_STRING = os.environ.get("USER_SESSION_STRING", "").strip()
HAS_PREMIUM_SESSION = bool(USER_SESSION_STRING)

# Channel IDs
PRIMARY_CHANNEL_ID = int(os.environ.get("PRIMARY_CHANNEL_ID", "0"))
raw_fwd = os.environ.get("FORWARD_CHANNEL_IDS", "")
FORWARD_CHANNEL_IDS = [int(x.strip()) for x in raw_fwd.split(",") if x.strip()]
CHANNEL_DELAY_SECONDS = float(os.environ.get("CHANNEL_DELAY_SECONDS", "3.0"))

# High-speed upload environment tuning for wzgram / pyrogram
os.environ["WZGRAM_UPLOAD_RATE_BOT"] = "120"
os.environ["WZGRAM_UPLOAD_POOL_BOT"] = "12"
os.environ["WZGRAM_UPLOAD_RATE_PREMIUM"] = "120"
os.environ["WZGRAM_UPLOAD_POOL_PREMIUM"] = "12"

# Bot Switch & Limits
BOT_SWITCH_COUNT = int(os.environ.get("BOT_SWITCH_COUNT", "50"))
MAX_BOT_FILE_SIZE_BYTES = int(float(os.environ.get("MAX_BOT_FILE_SIZE_GB", "1.89")) * 1024 * 1024 * 1024)
FORCE_PREMIUM_UPLOAD = os.environ.get("FORCE_PREMIUM_UPLOAD", "false").lower() in ("true", "1", "yes")

# Watermarking & Branding
ENABLE_WATERMARK = os.environ.get("ENABLE_WATERMARK", "true").lower() in ("true", "1", "yes")
WATERMARK_TEXT = os.environ.get("WATERMARK_TEXT", "").strip()
RELEASE_TAG = os.environ.get("RELEASE_TAG", "").strip()

# Custom Caption for Last 2 Channels
CUSTOM_CAPTION_TEXT = os.environ.get("CUSTOM_CAPTION_TEXT", "").strip()
raw_custom_chans = os.environ.get("CUSTOM_CAPTION_CHANNELS", "")
CUSTOM_CAPTION_CHANNELS = [int(x.strip()) for x in raw_custom_chans.split(",") if x.strip()]

# Fetch Source: 'lipat' or 'kuku'
FETCH_SOURCE = os.environ.get("FETCH_SOURCE", "lipat").strip().lower()
LIPAT_ADMIN_TG_ID = os.environ.get("LIPAT_ADMIN_TG_ID", "999888777").strip()

# Paths
CATALOG_PATH = BASE_DIR / os.environ.get("CATALOG_PATH", "all_kuku_shows.json")
LOG_FILE = BASE_DIR / "log.txt"
TEMP_DIR = BASE_DIR / "temp"
DOWNLOADS_DIR = BASE_DIR / "downloads"
ASSETS_DIR = BASE_DIR / "assets"
AUDIO_WATERMARK_FILE = ASSETS_DIR / "audio_watermark.mp3"

# Throttling
PROGRESS_UPDATE_INTERVAL = float(os.environ.get("PROGRESS_UPDATE_INTERVAL_SECONDS", "6.0"))
PARALLEL_DOWNLOAD_WORKERS = int(os.environ.get("PARALLEL_DOWNLOAD_WORKERS", "12"))

# Create directories
TEMP_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
