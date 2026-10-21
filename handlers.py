"""
Handlers for the Personal Telegram File Locker Bot.

Multi-user design
-----------------
  * Any Telegram user can use the bot — everyone gets their own private locker.
  * No one can see another user's files.
  * Owner (OWNER_ID) has an extra /admin command that lists all users and their
    file counts.

Section upload context
----------------------
  When a user opens a section the bot records the section ID in
  context.user_data[ACTIVE_SECTION_ID].  Any file uploaded while that context
  is active is automatically added to that section.  The context is cleared
  when the user navigates back to the main menu or opens a folder/search.

Commands
--------
  /start | /drive   — open main menu
  /stats            — storage overview
  /search           — search by filename
  /sections         — jump straight to sections list
  /favourites       — jump to starred files
  /recent           — jump to recently accessed files
  /setcommands      — re-register command menu with Telegram
  /admin            — owner only: list all users with file counts
"""

from __future__ import annotations

import asyncio
import functools
import logging
import mimetypes
import os
import time
import uuid
from collections import Counter
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

from passlib.context import CryptContext
from telegram import BotCommand, InputMediaPhoto, InputMediaVideo, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import ContextTypes

from config import (
    ALLOWED_CHAT_IDS,
    BOT_COMMANDS,
    BULK_CHUNK_SIZE,
    BULK_MAX_RETRIES,
    BULK_PROGRESS_EVERY,
    BULK_SEND_DELAY,
    CATEGORIES,
    INGEST_BASE_DELAY,
    INGEST_BATCH_IDLE_SECONDS,
    INGEST_CPU_THROTTLE_WARNING_RATIO,
    INGEST_HIGH_PRESSURE_DELAY,
    INGEST_MEMORY_CRITICAL_RATIO,
    INGEST_MEMORY_WARNING_RATIO,
    INGEST_PRESSURE_DELAY,
    INGEST_PROGRESS_EDIT_INTERVAL,
    INGEST_QUEUE_MAXSIZE,
    LINKED_HISTORY_IMPORT_DELAY,
    LINKED_HISTORY_IMPORT_MAX_IDS,
    LINKED_HISTORY_IMPORT_PROGRESS_EVERY,
    MIN_PASSWORD_LENGTH,
    OWNER_ID,
    PAGE_SIZE,
    classify_extension,
)
from db import Database, FileRecord
from keyboards import (
    admin_users_menu,
    back_to_menu,
    backup_source_detail,
    backup_sources_menu,
    bulk_progress_keyboard,
    favourites_view,
    file_detail,
    folder_view,
    link_prompt_keyboard,
    main_menu,
    pick_section_menu,
    recent_view,
    recategorize_picker,
    search_results_view,
    section_file_detail,
    section_share_user_view,
    section_shares_view,
    section_view,
    sections_menu,
)
from utils import human_date, human_size

logger = logging.getLogger(__name__)

# ── state-machine keys stored in context.user_data ───────────────────────────
AWAITING_SEARCH          = "awaiting_search"
AWAITING_RENAME          = "awaiting_rename_for_id"
AWAITING_SECTION_NAME    = "awaiting_section_name"
AWAITING_SECTION_RENAME  = "awaiting_section_rename"   # value = section id
AWAITING_SECTION_SHARE   = "awaiting_section_share"    # value = section id
ACTIVE_SECTION_ID        = "active_section_id"         # upload-to-section ctx
ACTIVE_SECTION_NAME      = "active_section_name"       # human label for above

# ── Accounts ──────────────────────────────────────────────────────────────
AWAITING_SIGNUP_PASSWORD = "awaiting_signup_password"
AWAITING_LOGIN_PASSWORD  = "awaiting_login_password"
AUTH_SESSION_KEY         = "authenticated"   # user_data flag, this runtime only

# Bcrypt is deliberately slow (that's the point); hashing/verifying is
# always offloaded to a thread (asyncio.to_thread) so it never blocks
# the event loop for other users while it runs.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

Responder = Callable[..., Awaitable[None]]


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.bot_data["db"]


def _uid(update: Update) -> int:
    """Return the effective user's Telegram ID (safe — only called after auth)."""
    return update.effective_user.id  # type: ignore[union-attr]


def _clear_section_ctx(user_data: dict) -> None:
    """Erase any active-section upload context."""
    user_data.pop(ACTIVE_SECTION_ID, None)
    user_data.pop(ACTIVE_SECTION_NAME, None)


# =============================================================================
# Auth decorators
# =============================================================================

def any_user(handler):
    """Allow any Telegram user — just reject messages with no user attached."""
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user is None:
            return
        return await handler(update, context)
    return wrapper


def owner_only(handler):
    """Only the bot owner may use this handler."""
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user is None or user.id != OWNER_ID:
            if user is not None:
                logger.info("Blocked non-owner access from user_id=%s", user.id)
            return
        return await handler(update, context)
    return wrapper


async def _require_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Gate for /start and private uploads: True if the user may proceed,
    otherwise sends a signup/login prompt and returns False.

    This only covers the two places explicitly gated (see cmd_start and
    handle_incoming_file) — every other command still works the same as
    before for a Telegram-identified user, since the bot's actual access
    control has always been the verified Telegram user_id, not this. The
    account layer sits on top of that as an extra, optional gate.
    """
    uid = _uid(update)
    db = _db(context)
    user = await db.get_user_by_telegram_id(uid)
    if not user:
        await update.effective_message.reply_text(
            "\U0001f44b Welcome! Please create an account first \u2014 use /signup to get started."
        )
        return False
    if not context.user_data.get(AUTH_SESSION_KEY):
        await update.effective_message.reply_text(
            "\U0001f512 Please /login first to continue."
        )
        return False
    return True


def any_upload_source(handler):
    """Allow incoming files from:
    - Any private DM (user gets their own locker)
    - ALLOWED_CHAT_IDS groups/channels (legacy, set via env var)
    - Any chat linked via the in-bot "Backup Sources" flow

    Channel posts never have an attached user (Telegram doesn't expose
    "who posted this" for channels), so this intentionally does NOT
    reject update.effective_user is None — only private DMs require one.
    """
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if chat is None:
            return
        if chat.type == "private":
            if update.effective_user is None:
                return
            return await handler(update, context)

        if chat.id in ALLOWED_CHAT_IDS:
            return await handler(update, context)

        db = _db(context)
        if await db.get_backup_source_by_chat(chat.id) is not None:
            return await handler(update, context)
        return
    return wrapper


# =============================================================================
# Message-shape helpers
# =============================================================================

def _is_media_message(message) -> bool:
    return bool(message.photo or message.video or message.document or message.audio)


async def _show_text(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup=None,
    parse_mode: Optional[str] = "Markdown",
) -> None:
    message = query.message
    if _is_media_message(message):
        try:
            await message.delete()
        except TelegramError:
            pass
        await context.bot.send_message(
            chat_id=message.chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )
    else:
        await query.edit_message_text(
            text, parse_mode=parse_mode, reply_markup=reply_markup
        )


async def _show_media(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    kind: str,
    file_id: str,
    caption: str,
    reply_markup=None,
) -> None:
    message = query.message
    chat_id = message.chat_id
    try:
        await message.delete()
    except TelegramError:
        pass

    if kind == "photo":
        await context.bot.send_photo(
            chat_id=chat_id, photo=file_id, caption=caption,
            parse_mode="Markdown", reply_markup=reply_markup,
        )
    elif kind == "video":
        await context.bot.send_video(
            chat_id=chat_id, video=file_id, caption=caption,
            parse_mode="Markdown", reply_markup=reply_markup,
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id, text=caption,
            parse_mode="Markdown", reply_markup=reply_markup,
        )


def _callback_responder(
    query, context: ContextTypes.DEFAULT_TYPE
) -> Responder:
    async def responder(
        text: str,
        parse_mode: Optional[str] = "Markdown",
        reply_markup=None,
    ) -> None:
        await _show_text(
            query, context, text,
            reply_markup=reply_markup, parse_mode=parse_mode,
        )
    return responder


# =============================================================================
# Navigation helper
# =============================================================================

async def _build_nav_callbacks(
    db: Database,
    category: str,
    page: int,
    idx: int,
    total: int,
    user_id: int,
    *,
    ascending: bool = False,
    order: str = "new",
) -> tuple[Optional[str], Optional[str]]:
    prev_cb: Optional[str] = None
    next_cb: Optional[str] = None

    files_page = await db.list_category(
        category, page=page, page_size=PAGE_SIZE, user_id=user_id, ascending=ascending
    )

    if idx > 0:
        prev_cb = f"file:{files_page[idx - 1].id}:{category}:{page}:{idx - 1}:{order}"
    elif page > 0:
        files_prev = await db.list_category(
            category, page=page - 1, page_size=PAGE_SIZE, user_id=user_id, ascending=ascending
        )
        if files_prev:
            prev_idx = len(files_prev) - 1
            prev_cb = f"file:{files_prev[-1].id}:{category}:{page - 1}:{prev_idx}:{order}"

    if idx < len(files_page) - 1:
        next_cb = f"file:{files_page[idx + 1].id}:{category}:{page}:{idx + 1}:{order}"
    elif (page + 1) * PAGE_SIZE < total:
        files_next = await db.list_category(
            category, page=page + 1, page_size=PAGE_SIZE, user_id=user_id, ascending=ascending
        )
        if files_next:
            next_cb = f"file:{files_next[0].id}:{category}:{page + 1}:0:{order}"

    return prev_cb, next_cb


# =============================================================================
# Shared file-render helper
# =============================================================================

async def _render_file(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    record: FileRecord,
    category: str,
    page: int,
    idx: int,
    user_id: int,
    *,
    prev_cb: Optional[str] = None,
    next_cb: Optional[str] = None,
    in_section_id: Optional[str] = None,
    section_page: int = 0,
    order: str = "new",
) -> None:
    sec_line = ""
    if not in_section_id:
        sec_names = await _db(context).file_sections(record.id, user_id)
        if sec_names:
            names_str = ", ".join(f"_{n}_" for n in sec_names[:3])
            sec_line = f"\nSections: {names_str}"

    fav_star = " \u2b50" if record.is_favorite else ""

    caption = (
        f"\U0001f4c4 *{record.filename}*{fav_star}\n\n"
        f"Size: {human_size(record.size)}\n"
        f"Category: {CATEGORIES[record.category]['label']}\n"
        f"Uploaded: {human_date(record.upload_date)}\n"
        f"Catalog ID: `{record.id}`"
        f"{sec_line}"
    )

    if in_section_id:
        section = await _db(context).get_section(in_section_id, user_id)
        can_edit_section = bool(
            section and (
                not section.is_shared or section.permission == "edit"
            )
        )
        buttons = section_file_detail(
            record.id, in_section_id, section_page, record.category,
            is_favorite=record.is_favorite,
            can_edit=can_edit_section,
        )
    else:
        buttons = file_detail(
            record.id, category, page, idx,
            prev_cb=prev_cb,
            next_cb=next_cb,
            is_favorite=record.is_favorite,
            order=order,
        )

    if record.category == "images":
        await _show_media(query, context, "photo", record.file_id, caption, buttons)
    elif record.category == "videos":
        await _show_media(query, context, "video", record.file_id, caption, buttons)
    else:
        await _show_text(query, context, caption, reply_markup=buttons)


# =============================================================================
# Commands
# =============================================================================

@any_user
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_auth(update, context):
        return
    uid  = _uid(update)
    db   = _db(context)
    counts    = await db.category_counts(uid)
    total, sz = await db.totals(uid)
    fav_count = await db.count_favorites(uid)
    is_owner  = (uid == OWNER_ID)
    text = (
        "\U0001f5c2 *Personal File Locker*\n\n"
        f"{total} files catalogued \u00b7 {human_size(sz)} total\n\n"
        "Forward or send me files and I'll catalogue them automatically.\n"
        "You can paste a message link from a linked Backup Source to import "
        "sequentially from that message ID (in limited batches).\n"
        "Browse your folders below \U0001f447"
    )
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_menu(counts, fav_count, is_owner=is_owner),
    )


@any_user
async def cmd_signup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please /signup in a private chat with me.")
        return
    uid = _uid(update)
    db = _db(context)
    if await db.get_user_by_telegram_id(uid):
        await update.message.reply_text(
            "You already have an account \u2014 use /login instead."
        )
        return
    context.user_data[AWAITING_SIGNUP_PASSWORD] = True
    await update.message.reply_text(
        "\U0001f510 Let's create your account.\n\n"
        f"Reply with a password (at least {MIN_PASSWORD_LENGTH} characters). "
        "I'll delete your message right after reading it, so it doesn't "
        "stay in the chat."
    )


@any_user
async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.message.reply_text("Please /login in a private chat with me.")
        return
    uid = _uid(update)
    db = _db(context)
    if not await db.get_user_by_telegram_id(uid):
        await update.message.reply_text(
            "You don't have an account yet \u2014 use /signup to create one."
        )
        return
    context.user_data[AWAITING_LOGIN_PASSWORD] = True
    await update.message.reply_text(
        "\U0001f511 Reply with your password to log in (I'll delete your "
        "message right after reading it)."
    )


@any_user
async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(AUTH_SESSION_KEY, None)
    await update.message.reply_text("\U0001f44b Logged out. Use /login to come back in.")


@any_user
async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = _uid(update)
    db = _db(context)
    user = await db.get_user_by_telegram_id(uid)
    if not user:
        await update.message.reply_text(
            f"Telegram user ID: `{uid}`\n"
            "Use this ID when someone shares a section with you. "
            "An account is only needed for your own locker.",
            parse_mode="Markdown",
        )
        return
    is_authed = bool(context.user_data.get(AUTH_SESSION_KEY))
    created    = human_date(user["created_at"])
    last_login = human_date(user["last_login"]) if user.get("last_login") else "never"
    session_line = (
        "\u2705 logged in" if is_authed else "\U0001f512 not logged in \u2014 use /login"
    )
    text = (
        "\U0001faaa *Account Info*\n\n"
        f"UUID: `{user['uuid']}`\n"
        f"Telegram ID: `{uid}`\n"
        f"Created: {created}\n"
        f"Last login: {last_login}\n"
        f"Session: {session_line}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@any_user
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _render_stats(update.message.reply_text, context, _uid(update))


async def _render_stats(
    responder: Responder,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> None:
    db = _db(context)
    total, size   = await db.totals(user_id)
    counts        = await db.category_counts(user_id)
    fav_count     = await db.count_favorites(user_id)
    lines = [
        "\U0001f4ca *Storage Overview*\n",
        f"Total: *{total}* files \u00b7 *{human_size(size)}*",
        f"\u2b50 Favourites: {fav_count}\n",
    ]
    for key, meta in CATEGORIES.items():
        n = counts.get(key, 0)
        if n:
            lines.append(f"{meta['label']}: {n}")
    await responder("\n".join(lines), parse_mode="Markdown", reply_markup=back_to_menu())


@any_user
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        query_str = " ".join(context.args)
        context.user_data["last_search"] = query_str
        await _render_search(update.message.reply_text, context, query_str, page=0, user_id=_uid(update))
    else:
        context.user_data[AWAITING_SEARCH] = True
        await update.message.reply_text(
            "\U0001f50e Send the filename (or part of it) you're looking for."
        )


async def _render_search(
    responder: Responder,
    context: ContextTypes.DEFAULT_TYPE,
    query_str: str,
    page: int,
    user_id: int,
) -> None:
    db = _db(context)
    files, total = await db.search(query_str, page=page, page_size=PAGE_SIZE, user_id=user_id)
    if total == 0:
        await responder(
            f"No files found matching '{query_str}'.",
            reply_markup=back_to_menu(),
        )
        return
    text = f"\U0001f50e *Results for '{query_str}'* \u2014 {total} match(es)"
    await responder(
        text,
        parse_mode="Markdown",
        reply_markup=search_results_view(files, page, total, PAGE_SIZE),
    )


@any_user
async def cmd_sections(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid      = _uid(update)
    db       = _db(context)
    sections = await db.list_sections(uid)
    text = (
        f"\U0001f4c2 *Sections* \u2014 {len(sections)} section(s)"
        if sections
        else "\U0001f4c2 *Sections*\n\nNo sections yet. Create one to start organising!"
    )
    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=sections_menu(sections)
    )


@any_user
async def cmd_favourites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid   = _uid(update)
    db    = _db(context)
    total = await db.count_favorites(uid)
    files = await db.list_favorites(page=0, page_size=PAGE_SIZE, user_id=uid)
    text = (
        f"\u2b50 *Favourites* \u2014 {total} file(s)"
        if files
        else "\u2b50 *Favourites*\n\nNo starred files yet. Tap \u2b50 on any file to star it."
    )
    await update.message.reply_text(
        text, parse_mode="Markdown",
        reply_markup=favourites_view(files, 0, total, PAGE_SIZE),
    )


@any_user
async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid   = _uid(update)
    db    = _db(context)
    files = await db.list_recent(uid)
    text = (
        f"\U0001f550 *Recently accessed* \u2014 {len(files)} file(s)"
        if files
        else "\U0001f550 *Recently accessed*\n\nYou haven't opened any files yet."
    )
    await update.message.reply_text(
        text, parse_mode="Markdown",
        reply_markup=recent_view(files),
    )


@owner_only
async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(context)
    sources = await db.list_backup_sources(OWNER_ID)
    text = (
        f"\U0001f4e6 *Backup Sources* \u2014 {len(sources)} linked"
        if sources
        else (
            "\U0001f4e6 *Backup Sources*\n\n"
            "None linked yet. Add this bot as *admin* to any group or "
            "channel and I'll ask if you want it backed up automatically."
        )
    )
    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=backup_sources_menu(sources)
    )


@owner_only
async def cmd_setcommands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await context.bot.set_my_commands(
        [BotCommand(name, desc) for name, desc in BOT_COMMANDS]
    )
    lines = ["\u2705 *Bot commands updated!*\n", "Registered:"]
    for name, desc in BOT_COMMANDS:
        lines.append(f"  /{name} \u2014 {desc}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@owner_only
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: show all users with their file counts."""
    db    = _db(context)
    users = await db.list_users()
    if not users:
        await update.message.reply_text(
            "\U0001f451 *Admin Panel*\n\nNo users yet.", parse_mode="Markdown"
        )
        return
    lines = [f"\U0001f451 *Admin Panel* \u2014 {len(users)} user(s)\n"]
    for stat in users:
        uid_label = f"You" if stat.user_id == OWNER_ID else f"User `{stat.user_id}`"
        lines.append(
            f"{uid_label}: *{stat.file_count}* files \u00b7 {human_size(stat.total_size)}"
        )
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=admin_users_menu(users),
    )


# =============================================================================
# Incoming file handler
# =============================================================================

def _fallback_filename(message, label: str, mime_type: str = "") -> str:
    """Generate a readable name; Telegram photo objects have no original name."""
    sent_at = getattr(message, "date", None)
    stamp = sent_at.strftime("%Y%m%d_%H%M%S") if sent_at else time.strftime("%Y%m%d_%H%M%S")
    message_id = getattr(message, "message_id", None)
    suffix = f"_{message_id}" if message_id is not None else ""
    extension = mimetypes.guess_extension(mime_type or "") or ""
    if extension == ".jpe":
        extension = ".jpg"
    if not extension:
        extension = {
            "photo": ".jpg",
            "video": ".mp4",
            "audio": ".mp3",
            "voice": ".ogg",
        }.get(label, ".bin")
    return f"{label}_{stamp}{suffix}{extension}"


def _extract_incoming_file(message) -> Optional[tuple[str, object, str, str]]:
    if message.document:
        f = message.document
        return "document", f, f.file_name or _fallback_filename(message, "document", f.mime_type or ""), f.mime_type or ""
    if message.video:
        f = message.video
        return "video", f, f.file_name or _fallback_filename(message, "video", f.mime_type or ""), f.mime_type or ""
    if message.audio:
        f = message.audio
        return "audio", f, f.file_name or _fallback_filename(message, "audio", f.mime_type or ""), f.mime_type or ""
    if message.voice:
        f = message.voice
        return "audio", f, _fallback_filename(message, "voice", f.mime_type or ""), f.mime_type or ""
    if message.photo:
        f = message.photo[-1]
        return "photo", f, _fallback_filename(message, "photo", "image/jpeg"), "image/jpeg"
    return None


def _parse_telegram_message_link(text: str) -> Optional[tuple[str | int, int]]:
    """Parse one public or private Telegram message link; never a range."""
    candidate = text.strip().strip("<>").rstrip(".,)")
    if "://" not in candidate:
        candidate = "https://" + candidate
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in {
        "t.me", "www.t.me", "telegram.me", "www.telegram.me",
    }:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    try:
        if len(parts) == 2:
            chat_ref, message_id = "@" + parts[0].lstrip("@"), int(parts[1])
        elif len(parts) == 3 and parts[0] == "s":
            chat_ref, message_id = "@" + parts[1].lstrip("@"), int(parts[2])
        elif len(parts) == 3 and parts[0] == "c":
            internal_id, message_id = int(parts[1]), int(parts[2])
            chat_ref = int(f"-100{internal_id}")
        else:
            return None
    except ValueError:
        return None
    if message_id <= 0:
        return None
    return chat_ref, message_id


async def _import_linked_history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_ref: str | int,
    message_id: int,
) -> None:
    """Import a bounded run of IDs starting at the owner's pasted message link."""
    uid = _uid(update)
    if uid != OWNER_ID:
        await update.message.reply_text("Only the bot owner can import from backup sources.")
        return
    if not await _require_auth(update, context):
        return

    db = _db(context)
    try:
        source_chat = await context.bot.get_chat(chat_ref)
    except TelegramError:
        await update.message.reply_text(
            "I couldn't access that source. Check the link and make sure the bot is still in the chat."
        )
        return
    linked = await db.get_backup_source_by_chat(source_chat.id)
    if source_chat.id not in ALLOWED_CHAT_IDS and linked is None:
        await update.message.reply_text(
            "I can only import from a group/channel already linked in Backup Sources "
            "or explicitly allowed in ALLOWED_CHAT_IDS."
        )
        return

    # Telegram does not provide bots a history-list endpoint. The owner supplies
    # a known ID, and this imports a finite run of IDs from there, skipping gaps.
    # A lock prevents two simultaneous scans from flooding the same owner chat.
    lock = context.bot_data.setdefault("linked_history_import_lock", asyncio.Lock())
    if lock.locked():
        await update.message.reply_text("A backup import is already running. Please wait for it to finish.")
        return

    active_section_id = context.user_data.get(ACTIVE_SECTION_ID)
    active_section = (
        await db.get_section(active_section_id, uid) if active_section_id else None
    )
    if active_section is None:
        active_section_id = None
    source_label = source_chat.title or source_chat.username or "linked source"
    progress = None

    async with lock:
        progress = await update.message.reply_text(
            f"Starting import from {source_label} at message ID {message_id}. "
            f"I'll check up to {LINKED_HISTORY_IMPORT_MAX_IDS} IDs, skipping gaps."
        )
        saved = 0
        duplicates = 0
        missing = 0
        unsupported = 0
        section_added = 0
        categories: Counter = Counter()
        section_name = active_section.name if active_section else None
        stopped_reason: Optional[str] = None
        checked = 0

        for offset in range(LINKED_HISTORY_IMPORT_MAX_IDS):
            current_id = message_id + offset
            retries = 0
            while True:
                try:
                    forwarded = await context.bot.forward_message(
                        chat_id=uid,
                        from_chat_id=source_chat.id,
                        message_id=current_id,
                    )
                    break
                except RetryAfter as exc:
                    retries += 1
                    if retries > 3:
                        stopped_reason = "Telegram kept rate-limiting the import."
                        forwarded = None
                        break
                    await asyncio.sleep(float(exc.retry_after) + 0.2)
                except TelegramError as exc:
                    error_text = str(exc).lower()
                    if any(
                        marker in error_text
                        for marker in (
                            "message to forward not found",
                            "message not found",
                            "message_id_invalid",
                            "message identifier is not specified",
                            "message can't be forwarded",
                            "message cannot be forwarded",
                            "protected content",
                        )
                    ):
                        missing += 1
                        forwarded = None
                        break
                    stopped_reason = (
                        "Telegram returned an unexpected error, so I stopped rather "
                        "than risk skipping a valid message."
                    )
                    logger.info(
                        "Linked history import stopped at chat_id=%s message_id=%s: %s",
                        source_chat.id, current_id, exc,
                    )
                    forwarded = None
                    break

            checked += 1
            if stopped_reason:
                break
            if forwarded is not None:
                result = await _catalog_linked_forward(
                    context,
                    uid=uid,
                    forwarded=forwarded,
                    active_section_id=active_section_id,
                )
                if result is None:
                    unsupported += 1
                else:
                    category, is_new, added_to_section = result
                    if is_new:
                        saved += 1
                        categories[category] += 1
                    else:
                        duplicates += 1
                    if added_to_section:
                        section_added += 1

            if (
                checked % LINKED_HISTORY_IMPORT_PROGRESS_EVERY == 0
                and checked < LINKED_HISTORY_IMPORT_MAX_IDS
            ):
                try:
                    await progress.edit_text(
                        _history_import_status(
                            source_label, checked, saved, duplicates,
                            missing, unsupported, categories,
                        )
                    )
                except TelegramError:
                    pass
            await asyncio.sleep(LINKED_HISTORY_IMPORT_DELAY)

        lines = [
            f"Import from {source_label}",
            f"Checked {checked} message IDs starting at {message_id}.",
            f"Saved: {saved} new file(s).",
        ]
        if categories:
            lines.extend(
                f"  {CATEGORIES[category]['label']}: {count}"
                for category, count in categories.most_common()
            )
        if duplicates:
            lines.append(f"Already in your locker: {duplicates}.")
        if missing:
            lines.append(f"Missing, restricted, or deleted IDs skipped: {missing}.")
        if unsupported:
            lines.append(f"Messages without a supported file skipped: {unsupported}.")
        if section_name and section_added:
            lines.append(
                f"Added {section_added} file(s) to the open section: {section_name}."
            )
        if stopped_reason:
            lines.append(stopped_reason)
            resume_id = message_id + max(0, checked - 1)
            resume_link = _telegram_message_link(source_chat, resume_id)
            lines.append("After fixing access or rate limits, resume from:")
            lines.append(resume_link or f"message ID {resume_id}")
        elif checked == LINKED_HISTORY_IMPORT_MAX_IDS:
            next_id = message_id + checked
            lines.append("Batch limit reached. Paste this next link to continue:")
            resume_link = _telegram_message_link(source_chat, next_id)
            if resume_link:
                lines.append(resume_link)
            else:
                lines.append(f"Resume from message ID {next_id} using a Telegram link.")
        else:
            lines.append("No further IDs were checked.")

        try:
            await progress.edit_text("\n".join(lines))
        except TelegramError:
            await context.bot.send_message(chat_id=uid, text="\n".join(lines))


async def _catalog_linked_forward(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    uid: int,
    forwarded,
    active_section_id: Optional[str],
) -> Optional[tuple[str, bool, bool]]:
    """Catalog a forwarded media message, preserving the active-section rule."""
    extracted = _extract_incoming_file(forwarded)
    if extracted is None:
        try:
            await context.bot.delete_message(uid, forwarded.message_id)
        except TelegramError:
            pass
        return None

    file_type, tg_obj, filename, _mime = extracted
    extension = os.path.splitext(filename)[1].lstrip(".") or "bin"
    category = classify_extension(extension)
    db = _db(context)
    section = (
        await db.get_section(active_section_id, uid) if active_section_id else None
    )
    storage_uid = uid
    if section and section.is_shared and section.permission == "edit":
        storage_uid = section.owner_id
    record_id, is_new = await db.add_file(
        user_id=storage_uid,
        file_id=tg_obj.file_id,
        file_unique_id=getattr(tg_obj, "file_unique_id", None),
        file_type=file_type,
        filename=filename,
        extension=extension,
        size=getattr(tg_obj, "file_size", 0) or 0,
        category=category,
    )
    added_to_section = False
    if section and (not section.is_shared or section.permission == "edit"):
        added_to_section = await db.add_to_section(
            section.id, record_id, user_id=uid
        )
    return category, is_new, added_to_section


def _telegram_message_link(chat, message_id: int) -> Optional[str]:
    """Build a pasteable public/private Telegram message link when possible."""
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{message_id}"
    chat_id = str(chat.id)
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{message_id}"
    return None


def _history_import_status(
    source_label: str,
    checked: int,
    saved: int,
    duplicates: int,
    missing: int,
    unsupported: int,
    categories: Counter,
) -> str:
    lines = [
        f"Importing from {source_label}",
        f"Checked {checked}/{LINKED_HISTORY_IMPORT_MAX_IDS} IDs · saved {saved}.",
    ]
    lines.extend(
        f"  {CATEGORIES[category]['label']}: {count}"
        for category, count in categories.most_common()
    )
    if duplicates:
        lines.append(f"Already saved: {duplicates}.")
    if missing:
        lines.append(f"Missing/restricted IDs skipped: {missing}.")
    if unsupported:
        lines.append(f"Non-file messages skipped: {unsupported}.")
    return "\n".join(lines)


async def _file_id_for_section(
    db: Database, file_id: str, section, user_id: int
) -> Optional[str]:
    """Make a shared-section copy owned by the section owner when needed."""
    record = await db.get_file(file_id, user_id)
    if record is None:
        return None
    if record.user_id == section.owner_id or (
        record.user_id == 0 and section.owner_id == OWNER_ID
    ):
        return record.id
    copied_id, _ = await db.add_file(
        user_id=section.owner_id,
        file_id=record.file_id,
        file_unique_id=record.file_unique_id,
        file_type=record.file_type,
        filename=record.filename,
        extension=record.extension,
        size=record.size,
        category=record.category,
    )
    return copied_id


@any_upload_source
async def handle_incoming_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Validate and enqueue an upload quickly; a single worker writes files."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if _extract_incoming_file(message) is None:
        return

    enqueue_lock = context.bot_data.setdefault("ingest_enqueue_lock", asyncio.Lock())
    queue: asyncio.Queue = context.bot_data.get("incoming_file_queue")
    if queue is None:
        await message.reply_text("The file queue is starting up. Please try again shortly.")
        return

    async with enqueue_lock:
        active_section_id = None
        active_section_name = None
        shared_editor = False
        if chat.type == "private":
            active_section_id = context.user_data.get(ACTIVE_SECTION_ID)
            active_section_name = context.user_data.get(ACTIVE_SECTION_NAME)
            if active_section_id and user is not None:
                active = await _db(context).get_section(active_section_id, user.id)
                shared_editor = bool(
                    active and active.is_shared and active.permission == "edit"
                )
            if not shared_editor and not await _require_auth(update, context):
                return
            dest_chat_id = user.id
        else:
            dest_chat_id = OWNER_ID

        job = {
            "update": update,
            "context": context,
            "dest_chat_id": dest_chat_id,
            "active_section_id": active_section_id,
            "active_section_name": active_section_name,
            "shared_editor": shared_editor,
            "source_label": getattr(chat, "title", None) or "your uploads",
        }
        try:
            queue.put_nowait(job)
        except asyncio.QueueFull:
            notices = context.bot_data.setdefault("ingest_queue_full_notice", {})
            now = time.monotonic()
            if now - notices.get(dest_chat_id, 0.0) >= 30:
                notices[dest_chat_id] = now
                await message.reply_text(
                    f"The file queue is full ({INGEST_QUEUE_MAXSIZE} waiting). "
                    "This file wasn't queued; "
                    "please resend it after the current queue drains."
                )
            return

        state = _register_ingest_file(
            context.bot_data, dest_chat_id, job["source_label"]
        )

    await _sync_ingest_progress(context.bot, dest_chat_id, state)


async def _process_incoming_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    active_section_id: Optional[str],
    active_section_name: Optional[str],
    shared_editor: bool,
) -> Optional[tuple[str, bool, Optional[str]]]:
    message = update.effective_message
    user    = update.effective_user
    chat    = update.effective_chat

    extracted = _extract_incoming_file(message)
    if extracted is None:
        return

    file_type, tg_obj, filename, _mime = extracted
    extension = os.path.splitext(filename)[1].lstrip(".") or "bin"
    category  = classify_extension(extension)
    size      = getattr(tg_obj, "file_size", 0) or 0

    # Files from ALLOWED_CHAT_IDS / linked backup-source groups & channels
    # go into the owner's locker — channel posts have no uploader at all
    # (user is None there), so there's no other sensible owner to pick.
    if chat.type == "private":
        uid = user.id
    else:
        uid = OWNER_ID

    db = _db(context)
    # If an editor is uploading while a shared section is active, the file
    # belongs to the section owner so every current share sees the same item.
    storage_uid = uid
    active_section = None
    if chat.type == "private" and active_section_id:
        active_section = await db.get_section(active_section_id, uid)
    if chat.type == "private":
        if shared_editor:
            if not (
                active_section
                and active_section.is_shared
                and active_section.permission == "edit"
            ):
                return None
        elif not context.user_data.get(AUTH_SESSION_KEY):
            return None
    if chat.type == "private":
        if active_section and active_section.is_shared:
            if active_section.permission == "edit":
                storage_uid = active_section.owner_id
            else:
                active_section = None

    record_id, is_new = await db.add_file(
        user_id       = storage_uid,
        file_id       = tg_obj.file_id,
        file_unique_id= getattr(tg_obj, "file_unique_id", None),
        file_type     = file_type,
        filename      = filename,
        extension     = extension,
        size          = size,
        category      = category,
    )

    # ── Section auto-add ──────────────────────────────────────────────────────
    # Two independent sources for "which section should this land in":
    #   - private DM: the section captured when the upload was queued
    #   - group/channel: whatever section this chat is linked to as a
    #     backup source (a standing link, not tied to any one session)
    #
    # The link is attempted regardless of `is_new`: `record_id` is valid
    # either way (add_file returns the existing record's id for a catalog
    # duplicate too), so a re-forwarded file that just isn't linked to
    # *this* section yet still gets linked. add_to_section itself is the
    # single source of truth for "is this file already in this section" —
    # it's backed by a unique index on (section_id, file_id), so calling
    # it again for something already linked is a safe no-op (returns
    # False) rather than a duplicate row. `added` below reflects that, so
    # the summary only ever claims "added to section" when something
    # actually changed.
    section_name_for_summary: Optional[str] = None
    if chat.type == "private":
        active_sec_id = active_section.id if active_section else active_section_id
        active_sec_name = (
            active_section.name if active_section
            else active_section_name or "section"
        )
        if active_sec_id and active_section:
            added = await db.add_to_section(active_sec_id, record_id, user_id=uid)
            if added:
                section_name_for_summary = active_sec_name
    else:
        source = await db.get_backup_source_by_chat(chat.id)
        if source:
            added = await db.add_to_section(source["section_id"], record_id, user_id=OWNER_ID)
            if added:
                section_name_for_summary = source["chat_title"]

    return category, is_new, section_name_for_summary


# =============================================================================
# Backup sources — detecting admin promotion, linking, auto-ingest
#
# Telegram does not give bots a history-list endpoint. Linking a source
# auto-saves new files; after linking, the owner can also supply a known
# message link for a bounded sequential import of older IDs.
# =============================================================================

async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cm = update.my_chat_member
    if cm is None:
        return
    chat = cm.chat
    if chat.type not in ("group", "supergroup", "channel"):
        return

    new_status = cm.new_chat_member.status
    old_status = cm.old_chat_member.status
    if new_status != "administrator" or old_status == "administrator":
        return  # only react to a fresh promotion, not every permission tweak

    # Only react when the owner themselves did the promoting — avoids a
    # stranger adding the bot to some unrelated group and spamming a link
    # prompt into the owner's DM.
    if cm.from_user is None or cm.from_user.id != OWNER_ID:
        return

    db = _db(context)
    if await db.get_backup_source_by_chat(chat.id) is not None:
        return  # already linked, nothing to ask

    text = (
        f"\U0001f4e6 I was just made admin in *{chat.title}*.\n\n"
        "Want me to back it up? New files posted there from now on will "
        "be saved automatically into a matching section, and I'll let "
        "you know here whenever something new comes in.\n\n"
        "_(I can't list the old chat history automatically. You can paste "
        "a known message link from this source to start a limited sequential import.)_"
    )
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID, text=text, parse_mode="Markdown",
            reply_markup=link_prompt_keyboard(chat.id),
        )
    except TelegramError:
        pass


def _register_ingest_file(bot_data: dict, dest_chat_id: int, source_label: str) -> dict:
    states = bot_data.setdefault("ingest_progress_states", {})
    state = states.get(dest_chat_id)
    if state is None or state["closed"]:
        state = {
            "source_label": source_label,
            "total": 0,
            "pending": 0,
            "processed": 0,
            "saved": 0,
            "duplicates": 0,
            "failed": 0,
            "active": False,
            "categories": Counter(),
            "sections": Counter(),
            "message_id": None,
            "last_edit_time": 0.0,
            "resource_status": None,
            "revision": 0,
            "closed": False,
            "edit_lock": asyncio.Lock(),
        }
        states[dest_chat_id] = state
    state["total"] += 1
    state["pending"] += 1
    state["revision"] += 1
    return state


def _ingest_progress_text(state: dict, *, final: bool = False) -> str:
    total = max(1, state["total"])
    processed = state["processed"]
    percent = 100 if final else min(100, int(processed * 100 / total))
    filled = min(10, percent // 10)
    bar = "\u2588" * filled + "\u2591" * (10 - filled)
    waiting = max(0, state["pending"] - int(state["active"]))
    title = "\u2705 File queue complete" if final else f"\U0001f4e5 Saving files from {state['source_label']}"
    lines = [
        title,
        f"{bar} {percent}% \u00b7 {processed}/{state['total']} processed",
        f"Active: {'yes' if state['active'] else 'no'} \u00b7 waiting: {waiting}",
        (
            f"Saved: {state['saved']} \u00b7 duplicates: {state['duplicates']} "
            f"\u00b7 errors: {state['failed']}"
        ),
    ]
    for category, count in state["categories"].most_common():
        lines.append(f"  {CATEGORIES[category]['label']}: {count}")
    for section_name, count in state["sections"].most_common():
        lines.append(f"  Added to {section_name}: {count}")
    if state["resource_status"]:
        lines.append(state["resource_status"])
    elif not final and state["pending"] == 0:
        lines.append("Queue idle; waiting briefly for more files.")
    return "\n".join(lines)


async def _sync_ingest_progress(
    bot, dest_chat_id: int, state: dict, *, force: bool = False, final: bool = False
) -> None:
    async with state["edit_lock"]:
        try:
            if state["message_id"] is None:
                message = await bot.send_message(
                    chat_id=dest_chat_id,
                    text=_ingest_progress_text(state, final=final),
                )
                state["message_id"] = message.message_id
                state["last_edit_time"] = time.monotonic()
                return
            now = time.monotonic()
            if not force and now - state["last_edit_time"] < INGEST_PROGRESS_EDIT_INTERVAL:
                return
            await bot.edit_message_text(
                chat_id=dest_chat_id,
                message_id=state["message_id"],
                text=_ingest_progress_text(state, final=final),
            )
            state["last_edit_time"] = now
        except Exception:
            pass  # Progress is helpful, but must never block file cataloguing.


async def _finalize_ingest_progress(
    bot, dest_chat_id: int, state: dict, revision: int
) -> None:
    await asyncio.sleep(INGEST_BATCH_IDLE_SECONDS)
    if state["pending"] != 0 or state["revision"] != revision:
        return
    state["closed"] = True
    state["resource_status"] = None
    await _sync_ingest_progress(
        bot, dest_chat_id, state, force=True, final=True
    )


def _cgroup_memory_ratio() -> Optional[float]:
    candidates = (
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
        (
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        ),
    )
    for current_path, limit_path in candidates:
        try:
            with open(current_path, encoding="utf-8") as current_file:
                current = int(current_file.read().strip())
            with open(limit_path, encoding="utf-8") as limit_file:
                limit_text = limit_file.read().strip()
            if limit_text == "max":
                continue
            limit = int(limit_text)
            if 0 < limit < (1 << 60):
                return min(1.0, current / limit)
        except (OSError, ValueError):
            continue
    try:
        values = {}
        with open("/proc/meminfo", encoding="utf-8") as meminfo:
            for line in meminfo:
                name, _, value = line.partition(":")
                if name in ("MemTotal", "MemAvailable"):
                    values[name] = int(value.split()[0]) * 1024
        if values.get("MemTotal", 0) > 0:
            ratio = 1 - values["MemAvailable"] / values["MemTotal"]
            return min(1.0, max(0.0, ratio))
    except (OSError, ValueError, KeyError):
        pass
    return None


def _cgroup_cpu_throttle_ratio(bot_data: dict) -> Optional[float]:
    for path in ("/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/cpu/cpu.stat"):
        try:
            with open(path, encoding="utf-8") as stat_file:
                values = {
                    key: int(value)
                    for line in stat_file
                    if len(parts := line.split()) == 2
                    for key, value in [parts]
                }
            periods = values["nr_periods"]
            throttled = values["nr_throttled"]
            now = time.monotonic()
            previous = bot_data.get("ingest_cpu_sample")
            bot_data["ingest_cpu_sample"] = (now, periods, throttled)
            if previous is None:
                return 0.0
            elapsed = now - previous[0]
            period_delta = periods - previous[1]
            throttled_delta = throttled - previous[2]
            if elapsed >= 0.35 and period_delta > 0:
                ratio = min(1.0, max(0.0, throttled_delta / period_delta))
                bot_data["ingest_cpu_pressure"] = ratio
            return bot_data.get("ingest_cpu_pressure", 0.0)
        except (OSError, ValueError, KeyError):
            continue
    return None


def _ingest_resource_delay(bot_data: dict) -> tuple[float, Optional[str]]:
    memory_ratio = _cgroup_memory_ratio()
    cpu_ratio = _cgroup_cpu_throttle_ratio(bot_data)
    delay = INGEST_BASE_DELAY
    reasons = []
    if memory_ratio is not None:
        memory_percent = int(memory_ratio * 100)
        if memory_ratio >= INGEST_MEMORY_CRITICAL_RATIO:
            delay = max(delay, INGEST_HIGH_PRESSURE_DELAY)
            reasons.append(f"High RAM use ({memory_percent}%) \u2014 slowing the queue.")
        elif memory_ratio >= INGEST_MEMORY_WARNING_RATIO:
            delay = max(delay, INGEST_PRESSURE_DELAY)
            reasons.append(f"RAM use is elevated ({memory_percent}%) \u2014 slowing the queue.")
    if cpu_ratio is not None and cpu_ratio >= INGEST_CPU_THROTTLE_WARNING_RATIO:
        delay = max(delay, INGEST_PRESSURE_DELAY)
        reasons.append("CPU quota is throttling the worker \u2014 slowing the queue.")
    return delay, " ".join(reasons) if reasons else None


async def run_ingest_worker(application) -> None:
    """Process all incoming file jobs sequentially with memory/CPU backpressure."""
    queue: asyncio.Queue = application.bot_data["incoming_file_queue"]
    states = application.bot_data.setdefault("ingest_progress_states", {})
    while True:
        job = await queue.get()
        if job is None:
            queue.task_done()
            return

        dest_chat_id = job["dest_chat_id"]
        state = states.get(dest_chat_id)
        if state is None:
            queue.task_done()
            continue

        state["active"] = True
        try:
            delay, resource_status = _ingest_resource_delay(application.bot_data)
            state["resource_status"] = resource_status
            await _sync_ingest_progress(application.bot, dest_chat_id, state)
            if delay:
                await asyncio.sleep(delay)
            result = await _process_incoming_file(
                job["update"],
                job["context"],
                active_section_id=job["active_section_id"],
                active_section_name=job["active_section_name"],
                shared_editor=job["shared_editor"],
            )
            if result is None:
                state["failed"] += 1
            else:
                category, is_new, section_name = result
                if is_new:
                    state["saved"] += 1
                    state["categories"][category] += 1
                    if section_name:
                        state["sections"][section_name] += 1
                else:
                    state["duplicates"] += 1
        except Exception:
            state["failed"] += 1
            logger.exception(
                "Queued file ingest failed for chat_id=%s message_id=%s",
                dest_chat_id,
                getattr(job["update"].effective_message, "message_id", None),
            )
        finally:
            state["processed"] += 1
            state["pending"] = max(0, state["pending"] - 1)
            state["active"] = False
            try:
                await _sync_ingest_progress(
                    application.bot,
                    dest_chat_id,
                    state,
                    force=state["pending"] == 0,
                )
            finally:
                if state["pending"] == 0:
                    asyncio.create_task(
                        _finalize_ingest_progress(
                            application.bot, dest_chat_id, state, state["revision"]
                        ),
                        name=f"finalize-ingest-progress-{dest_chat_id}",
                    )
                queue.task_done()


# =============================================================================
# Free-text handler — "awaiting X" state machine
# =============================================================================

@any_user
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = _uid(update)
    text = update.message.text.strip()

    # ── Signup / login password capture ─────────────────────────────────────
    # Checked first, and unconditionally: a password must never fall
    # through into rename/search/etc. Covers both flows up front, then
    # deletes the user's message in either case so the plaintext password
    # doesn't linger in the chat history.
    if context.user_data.get(AWAITING_SIGNUP_PASSWORD):
        context.user_data.pop(AWAITING_SIGNUP_PASSWORD, None)
        password = text
        try:
            await update.message.delete()
        except TelegramError:
            pass
        if len(password) < MIN_PASSWORD_LENGTH:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    f"That password is too short (minimum {MIN_PASSWORD_LENGTH} "
                    "characters). Send /signup to try again."
                ),
            )
            return
        db = _db(context)
        if await db.get_user_by_telegram_id(uid):
            # Raced with another /signup for the same account — not an error.
            await context.bot.send_message(chat_id=uid, text="You already have an account.")
            return
        password_hash = await asyncio.to_thread(pwd_context.hash, password)
        account_uuid = await db.create_user(uid, password_hash)
        context.user_data[AUTH_SESSION_KEY] = True
        await db.set_user_last_login(uid, time.time())
        await context.bot.send_message(
            chat_id=uid,
            text=(
                "\u2705 *Account created and you're logged in.*\n\n"
                f"Your account UUID: `{account_uuid}`\n"
                "(This id never changes \u2014 keep it if you ever need to "
                "reference your account.)\n\nSend /start to open your locker."
            ),
            parse_mode="Markdown",
        )
        return

    if context.user_data.get(AWAITING_LOGIN_PASSWORD):
        context.user_data.pop(AWAITING_LOGIN_PASSWORD, None)
        password = text
        try:
            await update.message.delete()
        except TelegramError:
            pass
        db = _db(context)
        user = await db.get_user_by_telegram_id(uid)
        if not user:
            await context.bot.send_message(
                chat_id=uid, text="No account found. Use /signup to create one."
            )
            return
        ok = await asyncio.to_thread(pwd_context.verify, password, user["password_hash"])
        if not ok:
            await context.bot.send_message(
                chat_id=uid, text="\u274c Wrong password. Try /login again."
            )
            return
        context.user_data[AUTH_SESSION_KEY] = True
        await db.set_user_last_login(uid, time.time())
        await context.bot.send_message(
            chat_id=uid,
            text="\u2705 Logged in. Send /start to open your locker.",
        )
        return

    # ── File rename ───────────────────────────────────────────────────────────
    rename_id = context.user_data.get(AWAITING_RENAME)
    if rename_id is not None:
        context.user_data.pop(AWAITING_RENAME, None)
        ok = await _db(context).rename_file(rename_id, text, uid)
        if ok:
            await update.message.reply_text(
                f"\u270f\ufe0f Renamed to *{text}*.", parse_mode="Markdown"
            )
        else:
            await update.message.reply_text("That file no longer exists.")
        return

    # ── New section name ──────────────────────────────────────────────────────
    if context.user_data.get(AWAITING_SECTION_NAME):
        context.user_data.pop(AWAITING_SECTION_NAME, None)
        name = text[:50]
        if not name:
            await update.message.reply_text("Section name can't be empty. Try again.")
            return
        db = _db(context)
        await db.create_section(name, uid)
        sections = await db.list_sections(uid)
        await update.message.reply_text(
            f"\u2705 Section *{name}* created!",
            parse_mode="Markdown",
            reply_markup=sections_menu(sections),
        )
        return

    share_section_id = context.user_data.get(AWAITING_SECTION_SHARE)
    if share_section_id is not None:
        context.user_data.pop(AWAITING_SECTION_SHARE, None)
        try:
            recipient_id = int(text)
            if recipient_id <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Send a valid numeric Telegram user ID. You can ask them to use /whoami."
            )
            return
        db = _db(context)
        if recipient_id == uid:
            await update.message.reply_text("You already own this section.")
            return
        granted = await db.grant_section_access(
            share_section_id, uid, recipient_id, permission="view"
        )
        if not granted:
            await update.message.reply_text("Section not found or sharing is unavailable.")
            return
        await update.message.reply_text(
            f"\U0001f517 Viewer access granted to `{recipient_id}`. "
            "You can change them to an editor or revoke access below.",
            parse_mode="Markdown",
            reply_markup=section_share_user_view(
                share_section_id, recipient_id, "view"
            ),
        )
        return

    # ── Rename existing section ───────────────────────────────────────────────
    rename_sec_id = context.user_data.get(AWAITING_SECTION_RENAME)
    if rename_sec_id is not None:
        context.user_data.pop(AWAITING_SECTION_RENAME, None)
        name = text[:50]
        if not name:
            await update.message.reply_text("Section name can't be empty.")
            return
        db = _db(context)
        ok = await db.rename_section(rename_sec_id, name, uid)
        if ok:
            section = await db.get_section(rename_sec_id, uid)
            total   = await db.count_section_files(rename_sec_id)
            files   = await db.list_section_files(rename_sec_id, 0, PAGE_SIZE, uid)
            await update.message.reply_text(
                f"\u270f\ufe0f Section renamed to *{name}*.",
                parse_mode="Markdown",
                reply_markup=section_view(section, files, 0, total, PAGE_SIZE),
            )
        else:
            await update.message.reply_text("Section not found.")
        return

    # ── Search ────────────────────────────────────────────────────────────────
    if context.user_data.get(AWAITING_SEARCH):
        context.user_data.pop(AWAITING_SEARCH, None)
        context.user_data["last_search"] = text
        await _render_search(
            update.message.reply_text, context, text, page=0, user_id=uid
        )
        return

    # A Telegram link from a linked/allowed source starts a bounded sequential
    # import at that message ID; the owner can continue with the next link.
    if update.effective_chat and update.effective_chat.type == "private":
        link = _parse_telegram_message_link(text)
        if link:
            await _import_linked_history(update, context, link[0], link[1])
            return


# =============================================================================
# Callback handler — all inline keyboard taps
# =============================================================================

@any_user
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data  = query.data
    db    = _db(context)
    uid   = _uid(update)

    # ── Fixed single-word actions ─────────────────────────────────────────────

    if data == "menu":
        _clear_section_ctx(context.user_data)
        counts    = await db.category_counts(uid)
        total, sz = await db.totals(uid)
        fav_count = await db.count_favorites(uid)
        is_owner  = (uid == OWNER_ID)
        text = (
            "\U0001f5c2 *Personal File Locker*\n\n"
            f"{total} files catalogued \u00b7 {human_size(sz)} total\n\n"
            "Browse your folders below \U0001f447"
        )
        await _show_text(
            query, context, text,
            reply_markup=main_menu(counts, fav_count, is_owner=is_owner),
        )
        return

    if data == "stats":
        await _render_stats(_callback_responder(query, context), context, uid)
        return

    if data == "search":
        _clear_section_ctx(context.user_data)
        context.user_data[AWAITING_SEARCH] = True
        await _show_text(
            query, context,
            "\U0001f50e Send the filename (or part of it) you're looking for.",
            reply_markup=back_to_menu(),
        )
        return

    if data == "noop":
        return

    if data == "sections":
        # Navigating to sections list — clear any section upload context
        _clear_section_ctx(context.user_data)
        sections = await db.list_sections(uid)
        text = (
            f"\U0001f4c2 *Sections* \u2014 {len(sections)} section(s)"
            if sections
            else "\U0001f4c2 *Sections*\n\nNo sections yet. Create one to start organising!"
        )
        await _show_text(query, context, text, reply_markup=sections_menu(sections))
        return

    if data == "backup":
        if uid != OWNER_ID:
            await query.answer("Owner only.", show_alert=True)
            return
        sources = await db.list_backup_sources(OWNER_ID)
        text = (
            f"\U0001f4e6 *Backup Sources* \u2014 {len(sources)} linked"
            if sources
            else (
                "\U0001f4e6 *Backup Sources*\n\n"
                "None linked yet. Add this bot as *admin* to any group or "
                "channel and I'll ask if you want it backed up automatically."
            )
        )
        await _show_text(query, context, text, reply_markup=backup_sources_menu(sources))
        return

    if data == "newsec":
        context.user_data[AWAITING_SECTION_NAME] = True
        await _show_text(
            query, context,
            "\U0001f4c2 Send a name for the new section.",
            reply_markup=back_to_menu(),
        )
        return

    if data == "recent":
        files = await db.list_recent(uid)
        text = (
            f"\U0001f550 *Recently accessed* \u2014 {len(files)} file(s)"
            if files
            else "\U0001f550 *Recently accessed*\n\nYou haven't opened any files yet."
        )
        await _show_text(query, context, text, reply_markup=recent_view(files))
        return

    if data == "bulksrch":
        query_str = context.user_data.get("last_search", "")
        if not query_str:
            await query.answer("No active search to bulk-download.", show_alert=True)
            return
        files = await db.search_all(query_str, uid)
        await _start_bulk_delivery(
            query, context, uid, files, f"\U0001f50e Search: '{query_str}'"
        )
        return

    if data == "bulkfav":
        files = await db.list_favorites_all(uid)
        await _start_bulk_delivery(query, context, uid, files, "\u2b50 Favourites")
        return

    # ── Prefixed actions ──────────────────────────────────────────────────────

    parts  = data.split(":")
    action = parts[0]

    if action == "sharesec":
        section_id = parts[1]
        section = await db.get_section(section_id, uid)
        if not section or section.is_shared:
            await query.answer("Only the section owner can manage sharing.", show_alert=True)
            return
        shares = await db.list_section_shares(section_id, uid)
        text = (
            f"\U0001f517 *Sharing: {section.name}*\n\n"
            "Viewer: can browse and download.\n"
            "Editor: can browse, download, add, and remove files.\n"
            "Only you can rename/delete the section or manage access. "
            "New files added later appear for everyone automatically.\n\n"
            "Share with a Telegram user ID; ask them to send /whoami to the bot."
        )
        await _show_text(
            query, context, text,
            reply_markup=section_shares_view(section_id, shares),
        )
        return

    if action == "shareadd":
        section_id = parts[1]
        section = await db.get_section(section_id, uid)
        if not section or section.is_shared:
            await query.answer("Only the section owner can share this section.", show_alert=True)
            return
        context.user_data[AWAITING_SECTION_SHARE] = section_id
        await _show_text(
            query, context,
            "Send the recipient's numeric Telegram user ID. They can get it with /whoami.",
            reply_markup=back_to_menu(),
        )
        return

    if action == "shareuser":
        section_id, recipient_id = parts[1], int(parts[2])
        section = await db.get_section(section_id, uid)
        shares = await db.list_section_shares(section_id, uid) if section and not section.is_shared else []
        share = next(
            (item for item in shares if int(item["user_id"]) == recipient_id), None
        )
        if share is None:
            await _show_text(query, context, "That share no longer exists.", reply_markup=back_to_menu())
            return
        permission = share.get("permission", "view")
        label = "Editor" if permission == "edit" else "Viewer"
        await _show_text(
            query, context,
            f"\U0001f517 User `{recipient_id}` has *{label}* access.",
            parse_mode="Markdown",
            reply_markup=section_share_user_view(section_id, recipient_id, permission),
        )
        return

    if action == "shareperm":
        section_id, recipient_id, permission = parts[1], int(parts[2]), parts[3]
        if permission not in ("view", "edit") or not await db.set_section_access(
            section_id, uid, recipient_id, permission
        ):
            await query.answer("Could not update that share.", show_alert=True)
            return
        label = "Editor" if permission == "edit" else "Viewer"
        await _show_text(
            query, context,
            f"\u2705 User `{recipient_id}` is now a *{label}*.",
            parse_mode="Markdown",
            reply_markup=section_share_user_view(section_id, recipient_id, permission),
        )
        return

    if action == "sharerevoke":
        section_id, recipient_id = parts[1], int(parts[2])
        if not await db.revoke_section_access(section_id, uid, recipient_id):
            await query.answer("That share was already removed.", show_alert=False)
            return
        shares = await db.list_section_shares(section_id, uid)
        await _show_text(
            query, context,
            f"\U0001f6ab Access revoked for `{recipient_id}`.",
            parse_mode="Markdown",
            reply_markup=section_shares_view(section_id, shares),
        )
        return

    if action == "cat":
        _clear_section_ctx(context.user_data)
        category, page = parts[1], int(parts[2])
        order = parts[3] if len(parts) > 3 else "new"
        ascending = order == "old"
        total = await db.count_category(category, uid)
        files = await db.list_category(
            category, page=page, page_size=PAGE_SIZE, user_id=uid, ascending=ascending
        )
        label = CATEGORIES[category]["label"]
        sort_note = "Oldest first" if ascending else "Newest first"
        text = (
            f"{label} \u2014 {total} file(s)\nSorted: {sort_note}"
            if files else f"{label}\n\nThis folder is empty."
        )
        await _show_text(
            query, context, text,
            reply_markup=folder_view(category, files, page, total, PAGE_SIZE, order=order),
        )
        return

    if action == "bulkcat":
        category = parts[1]
        order = parts[2] if len(parts) > 2 else "new"
        files = await db.list_category_all(category, uid, ascending=(order == "old"))
        label = CATEGORIES[category]["label"]
        await _start_bulk_delivery(query, context, uid, files, label)
        return

    if action == "srch":
        _clear_section_ctx(context.user_data)
        page       = int(parts[1])
        last_query = context.user_data.get("last_search", "")
        await _render_search(
            _callback_responder(query, context), context, last_query, page, uid
        )
        return

    if action == "favs":
        page  = int(parts[1])
        total = await db.count_favorites(uid)
        files = await db.list_favorites(page=page, page_size=PAGE_SIZE, user_id=uid)
        text  = (
            f"\u2b50 *Favourites* \u2014 {total} file(s)"
            if files
            else "\u2b50 *Favourites*\n\nNo starred files yet."
        )
        await _show_text(
            query, context, text,
            reply_markup=favourites_view(files, page, total, PAGE_SIZE),
        )
        return

    if action == "fav":
        fid        = parts[1]
        now_starred = await db.toggle_favorite(fid, uid)
        await query.answer(
            "\u2b50 Added to Favourites!" if now_starred else "Removed from Favourites.",
            show_alert=False,
        )
        record = await db.get_file(fid, uid)
        if record:
            await _render_file(query, context, record, record.category, 0, 0, uid)
        return

    if action == "file":
        fid      = parts[1]
        category = parts[2]
        page     = int(parts[3])
        idx      = int(parts[4]) if len(parts) > 4 else 0
        order    = parts[5] if len(parts) > 5 else "new"

        record = await db.get_file(fid, uid, mark_recent=True)
        if not record:
            await _show_text(query, context, "File no longer exists.", reply_markup=back_to_menu())
            return

        total = await db.count_category(category, uid)
        prev_cb, next_cb = await _build_nav_callbacks(
            db, category, page, idx, total, uid, ascending=(order == "old"), order=order
        )
        await _render_file(
            query, context, record, category, page, idx, uid,
            prev_cb=prev_cb, next_cb=next_cb, order=order,
        )
        return

    if action == "get":
        fid    = parts[1]
        record = await db.get_file(fid, uid, mark_recent=True)
        if not record:
            await query.answer("File no longer exists.", show_alert=True)
            return
        await _deliver_file(context, update.effective_chat.id, record)
        return

    if action == "ren":
        fid = parts[1]
        context.user_data[AWAITING_RENAME] = fid
        await _show_text(
            query, context,
            "\u270f\ufe0f Send the new filename for this file.",
            reply_markup=back_to_menu(),
        )
        return

    if action == "del":
        fid, category, page = parts[1], parts[2], int(parts[3])
        await db.delete_file(fid, uid)
        total = await db.count_category(category, uid)
        files = await db.list_category(category, page=page, page_size=PAGE_SIZE, user_id=uid)
        label = CATEGORIES[category]["label"]
        text  = f"\U0001f5d1 Deleted.\n\n{label} \u2014 {total} file(s) remaining."
        await _show_text(
            query, context, text,
            reply_markup=folder_view(category, files, page, total, PAGE_SIZE),
        )
        return

    if action == "recat":
        fid    = parts[1]
        record = await db.get_file(fid, uid)
        if not record:
            await _show_text(query, context, "File no longer exists.", reply_markup=back_to_menu())
            return
        await _show_text(
            query, context,
            f"\U0001f500 *{record.filename}*\n\nChoose the correct category:",
            reply_markup=recategorize_picker(fid, record.category),
        )
        return

    if action == "docat":
        fid, new_category = parts[1], parts[2]
        if new_category not in CATEGORIES:
            await query.answer("Unknown category.", show_alert=True)
            return
        ok = await db.recategorize_file(fid, new_category, uid)
        if ok:
            label = CATEGORIES[new_category]["label"]
            await query.answer(f"Moved to {label}!", show_alert=False)
            record = await db.get_file(fid, uid)
            if record:
                await _render_file(query, context, record, new_category, 0, 0, uid)
        else:
            await _show_text(query, context, "File no longer exists.", reply_markup=back_to_menu())
        return

    if action == "sec":
        section_id   = parts[1]
        page         = int(parts[2])
        section      = await db.get_section(section_id, uid)
        if not section:
            await _show_text(query, context, "Section not found.", reply_markup=back_to_menu())
            return

        # ── Set section upload context ────────────────────────────────────────
        can_edit = not section.is_shared or section.permission == "edit"
        if can_edit:
            context.user_data[ACTIVE_SECTION_ID]   = section_id
            context.user_data[ACTIVE_SECTION_NAME] = section.name
        else:
            _clear_section_ctx(context.user_data)

        total = await db.count_section_files(section_id)
        files = await db.list_section_files(section_id, page=page, page_size=PAGE_SIZE, user_id=uid)
        if can_edit:
            sec_ctx = "\n\n_\U0001f4ce Upload files now to add them here automatically._"
        else:
            sec_ctx = "\n\n_You have view-only access._"
        text = (
            f"\U0001f4c1 *{section.name}*\n\n{total} file(s){sec_ctx}"
            if files
            else f"\U0001f4c1 *{section.name}*\n\nNo files yet.{sec_ctx}"
        )
        await _show_text(
            query, context, text,
            reply_markup=section_view(section, files, page, total, PAGE_SIZE),
        )
        return

    if action == "bulksec":
        section_id = parts[1]
        section = await db.get_section(section_id, uid)
        if not section:
            await query.answer("Section not found.", show_alert=True)
            return
        files = await db.list_section_files_all(section_id, uid)
        await _start_bulk_delivery(query, context, uid, files, f"\U0001f4c2 {section.name}")
        return

    if action == "linkbackup":
        if uid != OWNER_ID:
            await query.answer("Owner only.", show_alert=True)
            return
        chat_id = int(parts[1])
        existing = await db.get_backup_source_by_chat(chat_id)
        if existing:
            await query.answer("Already linked.", show_alert=False)
            return
        try:
            chat_info = await context.bot.get_chat(chat_id)
        except TelegramError:
            await _show_text(
                query, context,
                "\u26a0\ufe0f Couldn't reach that chat \u2014 am I still admin there?",
                reply_markup=back_to_menu(),
            )
            return
        title = chat_info.title or f"Chat {chat_id}"
        section_id = await db.create_section(title, OWNER_ID)
        await db.link_backup_source(chat_id, title, chat_info.type, OWNER_ID, section_id)
        await _show_text(
            query, context,
            f"\u2705 *Linked!*\n\n"
            f"New files posted in *{title}* will be saved into the "
            f"matching section from now on, and I'll notify you here.",
            reply_markup=backup_source_detail(chat_id, section_id),
        )
        return

    if action == "dismissbackup":
        await _show_text(
            query, context, "Okay, skipped \u2014 you can link it anytime from \U0001f4e6 Backup Sources.",
            reply_markup=back_to_menu(),
        )
        return

    if action == "backupsrc":
        if uid != OWNER_ID:
            await query.answer("Owner only.", show_alert=True)
            return
        chat_id = int(parts[1])
        source = await db.get_backup_source_by_chat(chat_id)
        if not source:
            await _show_text(query, context, "This backup link no longer exists.", reply_markup=back_to_menu())
            return
        section_id = source["section_id"]
        total  = await db.count_section_files(section_id)
        counts = await db.section_category_counts(section_id)
        lines = [f"\U0001f4e6 *{source['chat_title']}*\n", f"Total: *{total}* file(s)\n"]
        for key, meta in CATEGORIES.items():
            n = counts.get(key, 0)
            if n:
                lines.append(f"{meta['label']}: {n}")
        await _show_text(
            query, context, "\n".join(lines),
            reply_markup=backup_source_detail(chat_id, section_id),
        )
        return

    if action == "unlinkbackup":
        if uid != OWNER_ID:
            await query.answer("Owner only.", show_alert=True)
            return
        chat_id = int(parts[1])
        source = await db.get_backup_source_by_chat(chat_id)
        name = source["chat_title"] if source else "that chat"
        await db.unlink_backup_source(chat_id, OWNER_ID)
        sources = await db.list_backup_sources(OWNER_ID)
        text = (
            f"\U0001f6ab Unlinked *{name}*.\n\n"
            "New files posted there won't be saved anymore \u2014 the "
            "section and everything already in it are untouched.\n\n"
        )
        text += (
            f"\U0001f4e6 *Backup Sources* \u2014 {len(sources)} linked"
            if sources else "No backup sources linked."
        )
        await _show_text(query, context, text, reply_markup=backup_sources_menu(sources))
        return

    if action == "secf":
        fid, section_id, section_page = parts[1], parts[2], int(parts[3])
        record = await db.get_file(fid, uid, mark_recent=True)
        if not record:
            await _show_text(query, context, "File no longer exists.", reply_markup=back_to_menu())
            return
        await _render_file(
            query, context, record,
            category=record.category, page=0, idx=0, user_id=uid,
            in_section_id=section_id, section_page=section_page,
        )
        return

    if action == "secadd":
        fid, category, page = parts[1], parts[2], int(parts[3])
        sections = await db.list_sections(uid)
        text = (
            "\U0001f4c2 Choose a section to add this file to:"
            if sections
            else "\U0001f4c2 You have no sections yet. Create one first!"
        )
        await _show_text(
            query, context, text,
            reply_markup=pick_section_menu(sections, fid, category, page),
        )
        return

    if action == "s2s":
        section_id, fid = parts[1], parts[2]
        section = await db.get_section(section_id, uid)
        if not section:
            await query.answer("Section not found.", show_alert=True)
            return
        if section.is_shared and section.permission != "edit":
            await query.answer("This section is view-only.", show_alert=True)
            return
        target_file_id = await _file_id_for_section(db, fid, section, uid)
        if target_file_id is None:
            await query.answer("File is no longer available.", show_alert=True)
            return
        added = await db.add_to_section(
            section_id, target_file_id, user_id=uid
        )
        await query.answer(
            f"\u2705 Added to '{section.name}'!" if added else f"Already in '{section.name}'.",
            show_alert=False,
        )
        return

    if action == "secrm":
        section_id, fid, section_page = parts[1], parts[2], int(parts[3])
        section = await db.get_section(section_id, uid)
        if not section:
            await _show_text(query, context, "Section was deleted.", reply_markup=back_to_menu())
            return
        if section.is_shared and section.permission != "edit":
            await query.answer("This section is view-only.", show_alert=True)
            return
        await db.remove_from_section(section_id, fid, user_id=uid)
        total = await db.count_section_files(section_id)
        files = await db.list_section_files(section_id, page=section_page, page_size=PAGE_SIZE, user_id=uid)
        text  = (
            f"\u2796 Removed.\n\n\U0001f4c1 *{section.name}* \u2014 {total} file(s) remaining."
            if files
            else f"\u2796 Removed.\n\n\U0001f4c1 *{section.name}* is now empty."
        )
        await _show_text(
            query, context, text,
            reply_markup=section_view(section, files, section_page, total, PAGE_SIZE),
        )
        return

    if action == "rensec":
        section_id = parts[1]
        context.user_data[AWAITING_SECTION_RENAME] = section_id
        await _show_text(
            query, context,
            "\u270f\ufe0f Send the new name for this section.",
            reply_markup=back_to_menu(),
        )
        return

    if action == "delsec":
        section_id = parts[1]
        section    = await db.get_section(section_id, uid)
        name       = section.name if section else "Section"
        await db.delete_section(section_id, uid)
        _clear_section_ctx(context.user_data)
        sections = await db.list_sections(uid)
        text  = f"\U0001f5d1 *{name}* deleted.\n\n"
        text += (
            f"\U0001f4c2 *My Sections* \u2014 {len(sections)} section(s) remaining."
            if sections
            else "No sections left."
        )
        await _show_text(query, context, text, reply_markup=sections_menu(sections))
        return

    if action == "bulkpause":
        job_id = parts[1]
        job = context.bot_data.get(BULK_JOBS_KEY, {}).get(job_id)
        if job is None:
            await query.answer("This delivery already finished.", show_alert=False)
            return
        job["paused"] = True
        await query.answer("\u23f8 Pausing after the current batch\u2026", show_alert=False)
        return

    if action == "bulkresume":
        job_id = parts[1]
        job = context.bot_data.get(BULK_JOBS_KEY, {}).get(job_id)
        if job is None:
            await query.answer("This delivery already finished.", show_alert=False)
            return
        job["paused"] = False
        await query.answer("\u25b6\ufe0f Resuming\u2026", show_alert=False)
        return

    if action == "bulkcancel":
        job_id = parts[1]
        job = context.bot_data.get(BULK_JOBS_KEY, {}).get(job_id)
        if job is None:
            await query.answer("This delivery already finished.", show_alert=False)
            return
        job["cancelled"] = True
        await query.answer("Cancelling after the current batch\u2026", show_alert=False)
        return


# =============================================================================
# File delivery
# =============================================================================

async def _deliver_file(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, record: FileRecord
) -> None:
    senders = {
        "document": context.bot.send_document,
        "video":    context.bot.send_video,
        "audio":    context.bot.send_audio,
        "photo":    context.bot.send_photo,
    }
    send  = senders.get(record.file_type, context.bot.send_document)
    kwarg = {"photo": "photo", "video": "video", "audio": "audio"}.get(
        record.file_type, "document"
    )
    try:
        await send(chat_id=chat_id, **{kwarg: record.file_id}, caption=record.filename)
    except TelegramError as exc:
        logger.warning("Failed to deliver file id=%s: %s", record.id, exc)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "\u26a0\ufe0f Telegram couldn't retrieve that file "
                "(it may have expired or been removed)."
            ),
        )


# =============================================================================
# Bulk delivery — "📥 Get All": send every file in a folder/section/search/
# favourites list in one go, without blocking the bot for anyone else.
#
# Design
# ------
#   * Runs as a tracked background task (Application.create_task) so a
#     4,000-file delivery doesn't freeze the bot for other users/commands.
#   * Photos/videos are grouped into media-group "albums" of up to
#     BULK_CHUNK_SIZE (Telegram's own limit is 10) — this is what turns
#     e.g. 4,000 photos into ~400 messages instead of 4,000, both far
#     faster and much less likely to trip Telegram's flood control.
#     Documents/audio (which can't usefully share an album with photos)
#     are sent individually via the existing _deliver_file.
#   * A short pause between every send plus automatic RetryAfter handling
#     (Telegram's own "please slow down" signal) keeps this safe even for
#     very large catalogs.
#   * One job per user at a time — a second tap while one is running is
#     rejected with a clear message rather than silently queuing.
#   * Cancellable mid-flight: the progress message carries a Cancel
#     button; the job checks a shared flag before every batch.
# =============================================================================

BULK_JOBS_KEY = "bulk_jobs"      # bot_data[BULK_JOBS_KEY][job_id] -> {"cancelled": bool}
BULK_ACTIVE_KEY = "bulk_active"  # bot_data[BULK_ACTIVE_KEY][user_id] -> job_id


def _new_bulk_job(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> Optional[str]:
    """Reserve a bulk-delivery slot for this user. Returns a job_id, or
    None if this user already has one in flight."""
    active = context.bot_data.setdefault(BULK_ACTIVE_KEY, {})
    if user_id in active:
        return None
    job_id = uuid.uuid4().hex[:10]
    context.bot_data.setdefault(BULK_JOBS_KEY, {})[job_id] = {
        "cancelled": False,
        "paused": False,
    }
    active[user_id] = job_id
    return job_id


def _finish_bulk_job(context: ContextTypes.DEFAULT_TYPE, user_id: int, job_id: str) -> None:
    context.bot_data.get(BULK_JOBS_KEY, {}).pop(job_id, None)
    context.bot_data.get(BULK_ACTIVE_KEY, {}).pop(user_id, None)


def _bulk_cancelled(context: ContextTypes.DEFAULT_TYPE, job_id: str) -> bool:
    job = context.bot_data.get(BULK_JOBS_KEY, {}).get(job_id)
    return bool(job and job["cancelled"])


def _bulk_paused(context: ContextTypes.DEFAULT_TYPE, job_id: str) -> bool:
    job = context.bot_data.get(BULK_JOBS_KEY, {}).get(job_id)
    return bool(job and job["paused"])


async def _start_bulk_delivery(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    uid: int,
    files: list[FileRecord],
    label: str,
) -> None:
    """Acknowledge a "Get All" tap and kick off the background job."""
    if not files:
        await query.answer("Nothing to send \u2014 this list is empty.", show_alert=True)
        return

    job_id = _new_bulk_job(context, uid)
    if job_id is None:
        await query.answer(
            "You already have a bulk delivery running. Cancel it first, "
            "or wait for it to finish.",
            show_alert=True,
        )
        return

    await query.answer(f"\U0001f680 Starting bulk delivery of {len(files)} file(s)\u2026")
    chat_id = query.message.chat_id
    progress_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"\U0001f4e5 *Bulk delivery started*\n\n{label}\n0/{len(files)} sent\u2026",
        parse_mode="Markdown",
        reply_markup=bulk_progress_keyboard(job_id),
    )
    context.application.create_task(
        _run_bulk_delivery(
            context, chat_id, uid, job_id, files, label, progress_msg.message_id
        ),
        update=None,
    )


async def _send_bulk_batch(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    batch: list[FileRecord],
    retries: int = 0,
) -> int:
    """Send one batch (a media-group album, or a single file). Returns how
    many files were actually handed to Telegram."""
    try:
        if len(batch) > 1:
            media_cls = InputMediaPhoto if batch[0].file_type == "photo" else InputMediaVideo
            media = [
                media_cls(media=f.file_id, caption=f.filename if k == 0 else None)
                for k, f in enumerate(batch)
            ]
            await context.bot.send_media_group(chat_id=chat_id, media=media)
        else:
            await _deliver_file(context, chat_id, batch[0])
        return len(batch)
    except RetryAfter as exc:
        if retries >= BULK_MAX_RETRIES:
            logger.warning("Bulk send gave up after repeated flood-control waits.")
            return 0
        await asyncio.sleep(exc.retry_after + 1)
        return await _send_bulk_batch(context, chat_id, batch, retries + 1)
    except TelegramError as exc:
        logger.warning("Bulk send failed for %d file(s): %s", len(batch), exc)
        return 0


async def _run_bulk_delivery(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    uid: int,
    job_id: str,
    files: list[FileRecord],
    label: str,
    progress_message_id: int,
) -> None:
    total = len(files)
    sent = 0
    cancelled = False
    chunks_since_update = 0

    async def _update_progress(
        final_text: Optional[str] = None, *, paused: bool = False
    ) -> None:
        markup = None if final_text else bulk_progress_keyboard(job_id, paused=paused)
        if final_text:
            text = final_text
        elif paused:
            text = (
                f"\u23f8 *Bulk delivery paused*\n\n{label}\n{sent}/{total} sent so far.\n"
                "Tap Resume whenever you're ready."
            )
        else:
            text = f"\U0001f4e5 *Bulk delivery in progress*\n\n{label}\n{sent}/{total} sent\u2026"
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=progress_message_id,
                text=text, parse_mode="Markdown", reply_markup=markup,
            )
        except TelegramError:
            pass  # message unchanged / deleted by the user — never fatal

    try:
        i = 0
        while i < total:
            if _bulk_cancelled(context, job_id):
                cancelled = True
                break

            if _bulk_paused(context, job_id):
                await _update_progress(paused=True)
                while _bulk_paused(context, job_id) and not _bulk_cancelled(context, job_id):
                    await asyncio.sleep(1.5)
                if _bulk_cancelled(context, job_id):
                    cancelled = True
                    break
                await _update_progress()  # back to "in progress" view on resume

            record = files[i]
            batch = [record]
            # Group consecutive same-type photos/videos into one album —
            # this is what makes thousands of photos practical to deliver.
            if record.file_type in ("photo", "video"):
                j = i + 1
                while (
                    j < total
                    and len(batch) < BULK_CHUNK_SIZE
                    and files[j].file_type == record.file_type
                ):
                    batch.append(files[j])
                    j += 1

            sent += await _send_bulk_batch(context, chat_id, batch)
            i += len(batch)
            chunks_since_update += 1

            if chunks_since_update >= BULK_PROGRESS_EVERY:
                chunks_since_update = 0
                await _update_progress()

            await asyncio.sleep(BULK_SEND_DELAY)
    finally:
        if cancelled:
            await _update_progress(
                f"\u23f9 *Bulk delivery cancelled*\n\n{label}\n"
                f"Sent {sent}/{total} file(s) before stopping."
            )
        else:
            await _update_progress(
                f"\u2705 *Bulk delivery complete*\n\n{label}\nSent {sent}/{total} file(s)."
            )
        _finish_bulk_job(context, uid, job_id)
