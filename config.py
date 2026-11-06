"""Configuration loading from environment / .env file."""
import os

from dotenv import load_dotenv

load_dotenv()


def _req(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required config: {name} (set it in .env)")
    return val


def _ints_csv(name: str) -> list[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]


TELEGRAM_BOT_TOKEN = _req("TELEGRAM_BOT_TOKEN")
TELEGRAM_SOURCE_CHAT_ID = _req("TELEGRAM_SOURCE_CHAT_ID")

BSKY_HANDLE = _req("BSKY_HANDLE").lstrip("@")
BSKY_APP_PASSWORD = _req("BSKY_APP_PASSWORD")

ADMIN_USER_IDS = _ints_csv("ADMIN_USER_IDS")

DB_PATH = os.environ.get("DB_PATH", "app.db").strip() or "app.db"
TG_POLL_TIMEOUT = int(os.environ.get("TG_POLL_TIMEOUT", "30") or "30")
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "5") or "5")
RETRY_BASE_DELAY = int(os.environ.get("RETRY_BASE_DELAY", "60") or "60")

# Bluesky limits (kept central so they are easy to review/adjust)
BSKY_MAX_GRAPHEMES = 300
BSKY_MAX_IMAGES = 4
BSKY_MAX_IMAGE_BYTES = 2 * 1024 * 1024
BSKY_MAX_VIDEO_BYTES = 100 * 1024 * 1024
