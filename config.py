"""Configuration for the Personal Telegram File Locker Bot."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

# --- Secrets / core settings ------------------------------------------------

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "8838748305:AAHwKnoS-wq3k6bpjA1RF_0GIsByB2KVOds")
OWNER_ID: int = int(os.getenv("OWNER_ID", "8493596199"))

# MongoDB (e.g. a free MongoDB Atlas cluster). Because the catalog lives
# in the cloud instead of a local file, moving the bot to a new server
# is just: set the same MONGO_URI there and start it up — zero data loss,
# and it keeps local disk usage near-zero (important on a 500MB box).
#
# NEVER hardcode a real token/URI here as a fallback default — this
# file (and this default) can end up committed to a public repo. Always
# set these via environment variables / your host's secrets manager.
MONGO_URI: str = os.getenv("MONGO_URI", "mongodb+srv://devilayush227_db_user:2LIDENsA70AnpWTa@cluster0.nv8dsfz.mongodb.net/?appName=Cluster0")
MONGO_DB_NAME: str = os.getenv("MONGO_DB_NAME", "file_locker")

# --- UI / behaviour tuning ---------------------------------------------------

PAGE_SIZE: int = 10

# How long (seconds) to wait after the *last* file in a forwarded burst
# before sending the consolidated "batch saved" summary. Every new file
# arrival resets this timer, so a burst of 100 forwards produces exactly
# one summary message a couple of seconds after the last one lands.
BATCH_SUMMARY_DELAY: float = 2.5

# While a burst is still coming in, a live "N files caught so far..."
# message is kept updated — but edited no more than once per this many
# seconds, so a very fast burst (e.g. a forwarded album) doesn't hammer
# Telegram with an edit per file.
INGEST_PROGRESS_EDIT_INTERVAL: float = 1.5

# --- Accounts ----------------------------------------------------------------
MIN_PASSWORD_LENGTH: int = 6

# --- Group / channel auto-backup -------------------------------------------
# Chat IDs (groups or channels) the bot is allowed to auto-catalog files
# from, in addition to the owner's private chat. Add the bot to a group
# or channel as an ADMIN, then put its numeric chat ID here (channel/
# group IDs are negative, e.g. -1001234567890). Anything posted there
# from now on gets catalogued automatically.
_raw_allowed = os.getenv("ALLOWED_CHAT_IDS", "-1004455954813")
ALLOWED_CHAT_IDS: set[int] = {
    int(cid.strip()) for cid in _raw_allowed.split(",") if cid.strip()
}

# --- Bulk delivery -----------------------------------------------------------
# "Get All" sends every file in a folder/section/search/favourites list in
# one go, running in the background. Photos and videos are grouped into
# albums of up to BULK_CHUNK_SIZE (Telegram's media-group limit), so e.g.
# 4,000 photos become ~400 messages instead of 4,000 — both far faster and
# much less likely to trip Telegram's flood limits than sending one by one.
BULK_CHUNK_SIZE: int = 10        # Telegram's max items per media group (album)
BULK_SEND_DELAY: float = 0.8     # seconds paused between each send
BULK_PROGRESS_EVERY: int = 3     # update the progress message every N chunks
BULK_MAX_RETRIES: int = 3        # retries on Telegram flood-control (RetryAfter)

# --- Commands registered with Telegram on startup ---------------------------
# These populate the "/" menu automatically via set_my_commands in main.py.
# To add more commands: append a (name, description) tuple here and restart
# the bot, or call /setcommands after the bot is running.
BOT_COMMANDS: list[tuple[str, str]] = [
    ("start",       "Open your file locker"),
    ("drive",       "Open your file locker"),
    ("stats",       "Show storage stats"),
    ("search",      "Search files by name"),
    ("sections",    "Browse your custom sections"),
    ("favourites",  "View your starred files"),
    ("recent",      "View recently accessed files"),
    ("admin",       "Admin panel — all users & stats (owner only)"),
    ("setcommands", "Re-register all commands with Telegram"),
    ("backup",      "Manage backup sources (admin channels/groups)"),
    ("signup",      "Create your account"),
    ("login",       "Log in to your account"),
    ("logout",      "Log out of the current session"),
    ("whoami",      "Show your account info"),
]

# --- Categories --------------------------------------------------------------
# Every file is classified by extension into one of these buckets.

CATEGORIES: dict[str, dict] = {
    "documents": {
        "label": "\U0001f4c4 Documents",
        "extensions": {
            "pdf", "doc", "docx", "txt", "rtf", "odt",
            "xls", "xlsx", "ppt", "pptx", "csv", "epub", "md",
        },
    },
    "archives": {
        "label": "\U0001f5c2 Archives",
        "extensions": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso"},
    },
    "videos": {
        "label": "\U0001f3ac Videos",
        "extensions": {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "3gp"},
    },
    "images": {
        "label": "\U0001f5bc Images",
        "extensions": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "heic", "tiff"},
    },
    "others": {
        "label": "\U0001f4e6 Others",
        "extensions": set(),  # catch-all
    },
}

CATEGORY_ORDER: list[str] = ["documents", "archives", "videos", "images", "others"]


def classify_extension(extension: str) -> str:
    """Map a file extension to its category key."""
    ext = extension.lower().lstrip(".")
    for key, meta in CATEGORIES.items():
        if key == "others":
            continue
        if ext in meta["extensions"]:
            return key
    return "others"
