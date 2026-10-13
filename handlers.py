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
import os
import time
import uuid
from collections import Counter
from typing import Awaitable, Callable, Optional

from passlib.context import CryptContext
from telegram import BotCommand, InputMediaPhoto, InputMediaVideo, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import ContextTypes

from config import (
    ALLOWED_CHAT_IDS,
    BATCH_SUMMARY_DELAY,
    BOT_COMMANDS,
    BULK_CHUNK_SIZE,
    BULK_MAX_RETRIES,
    BULK_PROGRESS_EVERY,
    BULK_SEND_DELAY,
    CATEGORIES,
    INGEST_PROGRESS_EDIT_INTERVAL,
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
        buttons = section_file_detail(
            record.id, in_section_id, section_page, record.category,
            is_favorite=record.is_favorite,
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
            "You don't have an account yet \u2014 use /signup to create one."
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
        f"\U0001f4c2 *My Sections* \u2014 {len(sections)} section(s)"
        if sections
        else "\U0001f4c2 *My Sections*\n\nNo sections yet. Create one to start organising!"
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

def _extract_incoming_file(message) -> Optional[tuple[str, object, str, str]]:
    if message.document:
        f = message.document
        return "document", f, f.file_name or f"document_{f.file_unique_id}", f.mime_type or ""
    if message.video:
        f = message.video
        return "video", f, f.file_name or f"video_{f.file_unique_id}.mp4", f.mime_type or ""
    if message.audio:
        f = message.audio
        return "audio", f, f.file_name or f"audio_{f.file_unique_id}.mp3", f.mime_type or ""
    if message.voice:
        f = message.voice
        return "audio", f, f"voice_{f.file_unique_id}.ogg", f.mime_type or ""
    if message.photo:
        f = message.photo[-1]
        return "photo", f, f"photo_{f.file_unique_id}.jpg", "image/jpeg"
    return None


@any_upload_source
async def handle_incoming_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user    = update.effective_user
    chat    = update.effective_chat

    extracted = _extract_incoming_file(message)
    if extracted is None:
        return

    if chat.type == "private" and not await _require_auth(update, context):
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
    record_id, is_new = await db.add_file(
        user_id       = uid,
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
    #   - private DM: whatever section the owner currently has open
    #     (context.user_data — only meaningful for an interactive user)
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
        active_sec_id   = context.user_data.get(ACTIVE_SECTION_ID)
        active_sec_name = context.user_data.get(ACTIVE_SECTION_NAME, "section")
        if active_sec_id:
            added = await db.add_to_section(active_sec_id, record_id)
            if added:
                section_name_for_summary = active_sec_name
    else:
        source = await db.get_backup_source_by_chat(chat.id)
        if source:
            added = await db.add_to_section(source["section_id"], record_id)
            if added:
                section_name_for_summary = source["chat_title"]

    source_label = getattr(chat, "title", None) or "your DM"
    await _queue_batch_summary(
        context,
        dest_chat_id  = uid,           # send summary to the uploader
        source_label  = source_label,
        category      = category,
        is_new        = is_new,
        section_name  = section_name_for_summary,
    )


# =============================================================================
# Backup sources — detecting admin promotion, linking, auto-ingest
#
# Telegram cannot hand a bot a chat's history from before it joined, so
# "backup" here only ever means "starting now" — see db.py's docstring on
# backup_sources for the full explanation. What this DOES give the owner:
# add the bot as admin anywhere → get asked once → confirm → every new
# file posted there from then on is saved and reported automatically.
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
        "_(I can only see files posted from now on — Telegram doesn't "
        "let bots read a chat's history from before they joined, so "
        "anything already in there can't be pulled in retroactively.)_"
    )
    try:
        await context.bot.send_message(
            chat_id=OWNER_ID, text=text, parse_mode="Markdown",
            reply_markup=link_prompt_keyboard(chat.id),
        )
    except TelegramError:
        pass


def _batch_progress_text(state: dict) -> str:
    """The live 'still catching files...' text shown while a burst is
    ongoing — same shape as the final summary, just phrased as in-progress."""
    label = state["source_label"]
    lines = [f"\U0001f4e5 *Catching files from {label}\u2026*\n"]
    lines.append(f"{state['new']} saved so far")
    for cat, n in state["categories"].most_common():
        lines.append(f"   {CATEGORIES[cat]['label']}: +{n}")
    if state["duplicates"]:
        lines.append(f"\u267b\ufe0f {state['duplicates']} duplicate(s) skipped")
    return "\n".join(lines)


async def _queue_batch_summary(
    context: ContextTypes.DEFAULT_TYPE,
    dest_chat_id: int,
    source_label: str,
    category: str,
    is_new: bool,
    section_name: Optional[str] = None,
) -> None:
    batches: dict[int, dict] = context.bot_data.setdefault("pending_batches", {})
    is_new_burst = dest_chat_id not in batches
    state = batches.setdefault(
        dest_chat_id,
        {
            "new": 0,
            "duplicates": 0,
            "categories": Counter(),
            "source_label": source_label,
            "section_name": None,
            "progress_message_id": None,
            "last_edit_time": 0.0,
        },
    )
    if is_new:
        state["new"] += 1
        state["categories"][category] += 1
        if section_name:
            state["section_name"] = section_name  # last active section wins
    else:
        state["duplicates"] += 1

    # Live progress: post a message on the first file of a burst, then
    # keep it updated (throttled) as more files arrive — rather than
    # staying silent until everything's done.
    if is_new_burst:
        try:
            msg = await context.bot.send_message(
                chat_id=dest_chat_id,
                text=_batch_progress_text(state),
                parse_mode="Markdown",
            )
            state["progress_message_id"] = msg.message_id
            state["last_edit_time"] = time.time()
        except TelegramError:
            pass  # progress display is a nicety, never block ingestion on it
    elif (
        state["progress_message_id"] is not None
        and time.time() - state["last_edit_time"] >= INGEST_PROGRESS_EDIT_INTERVAL
    ):
        try:
            await context.bot.edit_message_text(
                chat_id=dest_chat_id,
                message_id=state["progress_message_id"],
                text=_batch_progress_text(state),
                parse_mode="Markdown",
            )
            state["last_edit_time"] = time.time()
        except TelegramError:
            pass  # e.g. "message not modified" — harmless, ignore

    job_name = f"batch_summary:{dest_chat_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    context.job_queue.run_once(
        _send_batch_summary,
        when=BATCH_SUMMARY_DELAY,
        data=dest_chat_id,
        name=job_name,
    )


async def _send_batch_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    dest_chat_id: int = context.job.data
    batches: dict[int, dict] = context.bot_data.get("pending_batches", {})
    state = batches.pop(dest_chat_id, None)
    if not state or (state["new"] == 0 and state["duplicates"] == 0):
        return

    total = state["new"] + state["duplicates"]
    label = state["source_label"]
    sec   = state.get("section_name")
    progress_message_id = state.get("progress_message_id")

    if total == 1:
        if state["new"]:
            msg = f"\u2705 Saved from {label}."
            if sec:
                msg += f" Added to section \U0001f4c1 *{sec}*."
        else:
            msg = f"\u267b\ufe0f Duplicate from {label} skipped."
        if progress_message_id is not None:
            try:
                await context.bot.edit_message_text(
                    chat_id=dest_chat_id, message_id=progress_message_id,
                    text=msg, parse_mode="Markdown",
                )
                return
            except TelegramError:
                pass  # fall through to sending fresh below
        await context.bot.send_message(
            chat_id=dest_chat_id, text=msg, parse_mode="Markdown"
        )
        return

    lines = [f"\u2705 *From {label}:*\n"]
    if state["new"]:
        lines.append(f"{state['new']} file(s) saved")
        for cat, n in state["categories"].most_common():
            lines.append(f"   {CATEGORIES[cat]['label']}: +{n}")
        if sec:
            lines.append(f"\U0001f4c1 Auto-added to section: *{sec}*")
    if state["duplicates"]:
        lines.append(f"\u267b\ufe0f {state['duplicates']} duplicate(s) skipped")

    # Finalize in place (turn the live progress message into the final
    # summary) rather than leaving it stuck at "...catching files" and
    # sending a second, separate message.
    if progress_message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=dest_chat_id, message_id=progress_message_id,
                text="\n".join(lines), parse_mode="Markdown",
            )
            return
        except TelegramError:
            pass  # fall through to sending fresh below

    await context.bot.send_message(
        chat_id=dest_chat_id,
        text="\n".join(lines),
        parse_mode="Markdown",
    )


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
            f"\U0001f4c2 *My Sections* \u2014 {len(sections)} section(s)"
            if sections
            else "\U0001f4c2 *My Sections*\n\nNo sections yet. Create one to start organising!"
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
        context.user_data[ACTIVE_SECTION_ID]   = section_id
        context.user_data[ACTIVE_SECTION_NAME] = section.name

        total = await db.count_section_files(section_id)
        files = await db.list_section_files(section_id, page=page, page_size=PAGE_SIZE, user_id=uid)
        sec_ctx = f"\n\n_\U0001f4ce Upload files now to add them here automatically._"
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
        added = await db.add_to_section(section_id, fid)
        await query.answer(
            f"\u2705 Added to '{section.name}'!" if added else f"Already in '{section.name}'.",
            show_alert=False,
        )
        return

    if action == "secrm":
        section_id, fid, section_page = parts[1], parts[2], int(parts[3])
        await db.remove_from_section(section_id, fid)
        section = await db.get_section(section_id, uid)
        if not section:
            await _show_text(query, context, "Section was deleted.", reply_markup=back_to_menu())
            return
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
