"""
Personal Telegram File Locker Bot — entry point.

Run with:  python main.py
Requires BOT_TOKEN, OWNER_ID, and MONGO_URI in the environment (or a
.env file).
"""

from __future__ import annotations

import logging
import sys

from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import handlers
from config import BOT_COMMANDS, BOT_TOKEN, MONGO_DB_NAME, MONGO_URI, OWNER_ID
from db import Database

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def _post_init(application: Application) -> None:
    """Open the MongoDB connection and register the command menu."""
    db = Database(MONGO_URI, MONGO_DB_NAME, owner_id=OWNER_ID)
    await db.connect()
    application.bot_data["db"] = db
    logger.info("Connected to MongoDB database '%s'", MONGO_DB_NAME)

    # Automatically register all commands from config.BOT_COMMANDS so the
    # "/" menu in Telegram is always up to date — no manual @BotFather step
    # needed. Run /setcommands inside the bot to re-register at any time.
    await application.bot.set_my_commands(
        [BotCommand(name, desc) for name, desc in BOT_COMMANDS]
    )
    logger.info("Command menu registered: %s", [name for name, _ in BOT_COMMANDS])


async def _post_shutdown(application: Application) -> None:
    db: Database | None = application.bot_data.get("db")
    if db is not None:
        await db.close()


def build_application() -> Application:
    if not BOT_TOKEN:
        sys.exit("ERROR: BOT_TOKEN is not set. Add it to your environment or .env file.")
    if not OWNER_ID:
        sys.exit("ERROR: OWNER_ID is not set. Add it to your environment or .env file.")
    if not MONGO_URI:
        sys.exit("ERROR: MONGO_URI is not set. Add it to your environment or .env file.")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # ── Core commands ─────────────────────────────────────────────────────────
    application.add_handler(CommandHandler(["start", "drive"], handlers.cmd_start))
    application.add_handler(CommandHandler("stats",       handlers.cmd_stats))
    application.add_handler(CommandHandler("search",      handlers.cmd_search))

    # ── New feature commands ──────────────────────────────────────────────────
    application.add_handler(CommandHandler("sections",    handlers.cmd_sections))
    application.add_handler(CommandHandler("favourites",  handlers.cmd_favourites))
    application.add_handler(CommandHandler(
        ["favorites", "favs"],               # accept both spellings
        handlers.cmd_favourites,
    ))
    application.add_handler(CommandHandler("recent",      handlers.cmd_recent))
    application.add_handler(CommandHandler("setcommands", handlers.cmd_setcommands))
    application.add_handler(CommandHandler("admin",       handlers.cmd_admin))
    application.add_handler(CommandHandler("backup",      handlers.cmd_backup))
    application.add_handler(CommandHandler("signup",      handlers.cmd_signup))
    application.add_handler(CommandHandler("login",       handlers.cmd_login))
    application.add_handler(CommandHandler("logout",      handlers.cmd_logout))
    application.add_handler(CommandHandler("whoami",      handlers.cmd_whoami))

    # ── Admin-promotion detection (powers "Backup Sources") ──────────────────
    application.add_handler(ChatMemberHandler(
        handlers.handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER
    ))

    # ── Incoming files ────────────────────────────────────────────────────────
    file_filter = (
        filters.Document.ALL
        | filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.PHOTO
    )
    application.add_handler(MessageHandler(file_filter, handlers.handle_incoming_file))

    # ── Free text (rename / search / section-name awaiting input) ────────────
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handlers.handle_text
    ))

    # ── Inline keyboard taps ──────────────────────────────────────────────────
    application.add_handler(CallbackQueryHandler(handlers.handle_callback))

    return application


def main() -> None:
    application = build_application()
    logger.info("Bot starting — owner_id=%s", OWNER_ID)
    application.run_polling(
        allowed_updates=["message", "channel_post", "callback_query", "my_chat_member"]
    )


if __name__ == "__main__":
    main()
