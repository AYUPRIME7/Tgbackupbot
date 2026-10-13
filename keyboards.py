"""
Inline keyboard builders.

Callback data schema (kept short — Telegram caps it at 64 bytes):
    menu                              main folder dashboard
    cat:<category>:<page>:<order>     open a folder at a given page (order: new|old, default new)
    file:<id>:<category>:<page>:<i>:<order>  open a file's detail view (i = index in page)
    get:<id>                          forward the file back to the user
    ren:<id>:<category>:<page>        start a rename flow
    del:<id>:<category>:<page>        delete a file (from catalog)
    srch:<page>                       paginate the last search
    search                            prompt for a new search query
    stats                             show storage stats
    noop                              inert button (page indicator)

    fav:<file_id>                     toggle star/unstar a file
    favs:<page>                       browse starred files
    recent                            browse recently accessed files

    recat:<file_id>                   start recategorize flow
    docat:<file_id>:<new_cat>         confirm category change

    sections                          list all custom sections
    newsec                            prompt to create a new section
    rensec:<section_id>               prompt to rename a section
    sec:<section_id>:<page>           browse files inside a section
    secf:<file_id>:<sec_id>:<page>    view a file from inside a section
    secadd:<file_id>:<cat>:<page>     choose which section to add a file to
    s2s:<sec_id>:<file_id>            confirm add-file-to-section
    secrm:<sec_id>:<file_id>:<page>   remove file from section
    delsec:<sec_id>                   delete an entire section

    bulkcat:<category>:<order>        bulk-deliver every file in a category
    bulksec:<sec_id>                  bulk-deliver every file in a section
    bulksrch                          bulk-deliver the last search's results
    bulkfav                           bulk-deliver every favourited file
    bulkpause:<job_id>                pause a running bulk delivery
    bulkresume:<job_id>               resume a paused bulk delivery
    bulkcancel:<job_id>               cancel a running bulk delivery

    backup                            list linked backup sources (owner only)
    linkbackup:<chat_id>              confirm linking a chat as a backup source
    dismissbackup:<chat_id>           decline the link prompt
    backupsrc:<chat_id>               view one backup source's detail/stats
    unlinkbackup:<chat_id>            stop auto-backup for a chat (keeps the section)
"""

from __future__ import annotations

from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import CATEGORIES, CATEGORY_ORDER
from db import FileRecord, SectionRecord, UserStat
from utils import human_size, truncate

_CAT_LABELS = {
    "documents": "\U0001f4c4 Documents",
    "archives":  "\U0001f5c2 Archives",
    "videos":    "\U0001f3ac Videos",
    "images":    "\U0001f5bc Images",
    "others":    "\U0001f4e6 Others",
}


# =============================================================================
# Main menu
# =============================================================================

def main_menu(
    counts: dict[str, int],
    fav_count: int = 0,
    *,
    is_owner: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    for key in CATEGORY_ORDER:
        label = CATEGORIES[key]["label"]
        rows.append(
            [InlineKeyboardButton(
                f"{label}  ({counts.get(key, 0)})",
                callback_data=f"cat:{key}:0",
            )]
        )
    rows.append([
        InlineKeyboardButton("\U0001f50e Search", callback_data="search"),
        InlineKeyboardButton("\U0001f4ca Stats",  callback_data="stats"),
    ])
    fav_label = f"\u2b50 Favourites  ({fav_count})" if fav_count else "\u2b50 Favourites"
    rows.append([
        InlineKeyboardButton(fav_label,         callback_data="favs:0"),
        InlineKeyboardButton("\U0001f550 Recent", callback_data="recent"),
    ])
    rows.append([InlineKeyboardButton("\U0001f4c2 My Sections", callback_data="sections")])
    if is_owner:
        rows.append([InlineKeyboardButton(
            "\U0001f4e6 Backup Sources", callback_data="backup"
        )])
        rows.append([InlineKeyboardButton("\U0001f451 Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


# =============================================================================
# Pagination helper
# =============================================================================

def _pagination_row(
    page: int, total: int, page_size: int, make_cb
) -> list[list[InlineKeyboardButton]]:
    total_pages = max(1, -(-total // page_size))
    rows: list[list[InlineKeyboardButton]] = []

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("\u2b05\ufe0f", callback_data=make_cb(page - 1)))
    nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton("\u27a1\ufe0f", callback_data=make_cb(page + 1)))
    rows.append(nav)

    if total_pages > 5:
        jump = []
        if page > 0:
            jump.append(InlineKeyboardButton("\u23ee First", callback_data=make_cb(0)))
        if page - 10 > 0:
            jump.append(InlineKeyboardButton("-10", callback_data=make_cb(page - 10)))
        if page + 10 < total_pages - 1:
            jump.append(InlineKeyboardButton("+10", callback_data=make_cb(page + 10)))
        if page < total_pages - 1:
            jump.append(InlineKeyboardButton("Last \u23ed", callback_data=make_cb(total_pages - 1)))
        if jump:
            rows.append(jump)

    return rows


# =============================================================================
# Folder / search / recent / favourites views
# =============================================================================

def folder_view(
    category: str,
    files: list[FileRecord],
    page: int,
    total: int,
    page_size: int,
    order: str = "new",
) -> InlineKeyboardMarkup:
    """order: "new" = newest-uploaded first (default), "old" = oldest first."""
    rows = []
    for i, f in enumerate(files):
        title = f"{truncate(f.filename)}  \u00b7  {human_size(f.size)}"
        rows.append([InlineKeyboardButton(
            title, callback_data=f"file:{f.id}:{category}:{page}:{i}:{order}"
        )])

    if total > page_size:
        rows.extend(_pagination_row(
            page, total, page_size, lambda p: f"cat:{category}:{p}:{order}"
        ))

    next_order = "old" if order == "new" else "new"
    sort_label = "\U0001f553 Sort: Oldest First" if order == "new" else "\U0001f553 Sort: Newest First"
    rows.append([InlineKeyboardButton(
        sort_label, callback_data=f"cat:{category}:0:{next_order}"
    )])

    if total > 0:
        rows.append([InlineKeyboardButton(
            f"\U0001f4e5 Get All ({total})", callback_data=f"bulkcat:{category}:{order}"
        )])

    rows.append([InlineKeyboardButton("\u2b06\ufe0f Drive Home", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def search_results_view(
    files: list[FileRecord],
    page: int,
    total: int,
    page_size: int,
) -> InlineKeyboardMarkup:
    rows = []
    for f in files:
        label = CATEGORIES[f.category]["label"].split()[0]
        title = f"{label} {truncate(f.filename, 38)}  \u00b7  {human_size(f.size)}"
        rows.append([InlineKeyboardButton(
            title, callback_data=f"file:{f.id}:{f.category}:{page}:0"
        )])

    if total > page_size:
        rows.extend(_pagination_row(page, total, page_size, lambda p: f"srch:{p}"))

    if total > 0:
        rows.append([InlineKeyboardButton(
            f"\U0001f4e5 Get All ({total})", callback_data="bulksrch"
        )])

    rows.append([InlineKeyboardButton("\u2b06\ufe0f Drive Home", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def favourites_view(
    files: list[FileRecord],
    page: int,
    total: int,
    page_size: int,
) -> InlineKeyboardMarkup:
    rows = []
    for f in files:
        label = CATEGORIES[f.category]["label"].split()[0]
        title = f"{label} {truncate(f.filename, 38)}  \u00b7  {human_size(f.size)}"
        rows.append([InlineKeyboardButton(
            title, callback_data=f"file:{f.id}:{f.category}:{page}:0"
        )])

    if total > page_size:
        rows.extend(_pagination_row(page, total, page_size, lambda p: f"favs:{p}"))

    if total > 0:
        rows.append([InlineKeyboardButton(
            f"\U0001f4e5 Get All ({total})", callback_data="bulkfav"
        )])

    rows.append([InlineKeyboardButton("\u2b06\ufe0f Drive Home", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def recent_view(files: list[FileRecord]) -> InlineKeyboardMarkup:
    rows = []
    for f in files:
        label = CATEGORIES[f.category]["label"].split()[0]
        title = f"{label} {truncate(f.filename, 38)}  \u00b7  {human_size(f.size)}"
        rows.append([InlineKeyboardButton(
            title, callback_data=f"file:{f.id}:{f.category}:0:0"
        )])
    rows.append([InlineKeyboardButton("\u2b06\ufe0f Drive Home", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


# =============================================================================
# File detail
# =============================================================================

def file_detail(
    file_id: str,
    category: str,
    page: int,
    idx: int,
    *,
    prev_cb: Optional[str] = None,
    next_cb: Optional[str] = None,
    is_favorite: bool = False,
    order: str = "new",
) -> InlineKeyboardMarkup:
    """
    Rows:
      1. ⬅️ Prev  |  ➡️ Next     (only if applicable)
      2. 📥 Get File
      3. ✏️ Rename  |  🗑 Delete
      4. ⭐ Favourite  |  📂 Add to Section
      5. 🔀 Change Category
      6. 🔙 Back
    """
    rows = []

    nav = []
    if prev_cb:
        nav.append(InlineKeyboardButton("\u2b05\ufe0f Prev", callback_data=prev_cb))
    if next_cb:
        nav.append(InlineKeyboardButton("\u27a1\ufe0f Next", callback_data=next_cb))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("\U0001f4e5 Get File", callback_data=f"get:{file_id}")])

    rows.append([
        InlineKeyboardButton("\u270f\ufe0f Rename", callback_data=f"ren:{file_id}:{category}:{page}"),
        InlineKeyboardButton("\U0001f5d1 Delete",  callback_data=f"del:{file_id}:{category}:{page}"),
    ])

    fav_label = "\u2605 Unfavourite" if is_favorite else "\u2b50 Favourite"
    rows.append([
        InlineKeyboardButton(fav_label, callback_data=f"fav:{file_id}"),
        InlineKeyboardButton("\U0001f4c2 Add to Section", callback_data=f"secadd:{file_id}:{category}:{page}"),
    ])

    rows.append([InlineKeyboardButton("\U0001f500 Change Category", callback_data=f"recat:{file_id}")])
    rows.append([InlineKeyboardButton("\U0001f519 Back", callback_data=f"cat:{category}:{page}:{order}")])

    return InlineKeyboardMarkup(rows)


# =============================================================================
# Recategorize
# =============================================================================

def recategorize_picker(
    file_id: str, current_category: str
) -> InlineKeyboardMarkup:
    rows = []
    for key in CATEGORY_ORDER:
        if key == current_category:
            continue
        label = _CAT_LABELS.get(key, key)
        rows.append([InlineKeyboardButton(label, callback_data=f"docat:{file_id}:{key}")])
    rows.append([InlineKeyboardButton("\U0001f519 Cancel", callback_data=f"cat:{current_category}:0")])
    return InlineKeyboardMarkup(rows)


# =============================================================================
# Sections
# =============================================================================

def sections_menu(sections: list[SectionRecord]) -> InlineKeyboardMarkup:
    rows = []
    for s in sections:
        label = f"\U0001f4c1 {truncate(s.name, 30)}  ({s.count})"
        rows.append([InlineKeyboardButton(label, callback_data=f"sec:{s.id}:0")])
    rows.append([InlineKeyboardButton("\u2795 New Section", callback_data="newsec")])
    rows.append([InlineKeyboardButton("\u2b06\ufe0f Drive Home", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def section_view(
    section: SectionRecord,
    files: list[FileRecord],
    page: int,
    total: int,
    page_size: int,
) -> InlineKeyboardMarkup:
    rows = []
    for f in files:
        title = f"{truncate(f.filename, 38)}  \u00b7  {human_size(f.size)}"
        rows.append([InlineKeyboardButton(
            title, callback_data=f"secf:{f.id}:{section.id}:{page}"
        )])

    if total > page_size:
        rows.extend(_pagination_row(
            page, total, page_size, lambda p: f"sec:{section.id}:{p}"
        ))

    if total > 0:
        rows.append([InlineKeyboardButton(
            f"\U0001f4e5 Get All ({total})", callback_data=f"bulksec:{section.id}"
        )])

    rows.append([
        InlineKeyboardButton("\u270f\ufe0f Rename", callback_data=f"rensec:{section.id}"),
        InlineKeyboardButton("\U0001f5d1 Delete Section", callback_data=f"delsec:{section.id}"),
    ])
    rows.append([
        InlineKeyboardButton("\U0001f4c2 All Sections", callback_data="sections"),
        InlineKeyboardButton("\u2b06\ufe0f Drive Home", callback_data="menu"),
    ])
    return InlineKeyboardMarkup(rows)


def section_file_detail(
    file_id: str,
    section_id: str,
    section_page: int,
    category: str,
    *,
    is_favorite: bool = False,
) -> InlineKeyboardMarkup:
    fav_label = "\u2605 Unfavourite" if is_favorite else "\u2b50 Favourite"
    rows = [
        [InlineKeyboardButton("\U0001f4e5 Get File", callback_data=f"get:{file_id}")],
        [
            InlineKeyboardButton(fav_label, callback_data=f"fav:{file_id}"),
            InlineKeyboardButton(
                "\u2796 Remove from Section",
                callback_data=f"secrm:{section_id}:{file_id}:{section_page}",
            ),
        ],
        [
            InlineKeyboardButton(
                "\U0001f4c2 Add to Another Section",
                callback_data=f"secadd:{file_id}:{category}:0",
            )
        ],
        [
            InlineKeyboardButton(
                "\U0001f519 Back to Section",
                callback_data=f"sec:{section_id}:{section_page}",
            )
        ],
    ]
    return InlineKeyboardMarkup(rows)


def pick_section_menu(
    sections: list[SectionRecord],
    file_id: str,
    category: str,
    page: int,
) -> InlineKeyboardMarkup:
    rows = []
    for s in sections:
        label = f"\U0001f4c1 {truncate(s.name, 35)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"s2s:{s.id}:{file_id}")])
    rows.append([InlineKeyboardButton("\u2795 Create New Section", callback_data="newsec")])
    rows.append([
        InlineKeyboardButton(
            "\U0001f519 Back to File",
            callback_data=f"file:{file_id}:{category}:{page}:0",
        )
    ])
    return InlineKeyboardMarkup(rows)


# =============================================================================
# Backup sources — groups/channels the bot admins, auto-saving into a section
# =============================================================================

def link_prompt_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Sent to the owner right after the bot is promoted to admin somewhere."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2705 Yes, back it up", callback_data=f"linkbackup:{chat_id}"),
        InlineKeyboardButton("\u274c No thanks", callback_data=f"dismissbackup:{chat_id}"),
    ]])


def backup_sources_menu(sources: list[dict]) -> InlineKeyboardMarkup:
    """sources: dicts from Database.list_backup_sources (chat_id, chat_title, ...)."""
    rows = []
    for s in sources:
        label = f"\U0001f4e6 {truncate(s['chat_title'], 32)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"backupsrc:{s['chat_id']}")])
    rows.append([InlineKeyboardButton("\u2b06\ufe0f Drive Home", callback_data="menu")])
    return InlineKeyboardMarkup(rows)


def backup_source_detail(chat_id: int, section_id: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("\U0001f4c1 Open Section", callback_data=f"sec:{section_id}:0")],
        [InlineKeyboardButton("\U0001f504 Refresh Stats", callback_data=f"backupsrc:{chat_id}")],
        [InlineKeyboardButton("\U0001f6ab Unlink Backup", callback_data=f"unlinkbackup:{chat_id}")],
        [InlineKeyboardButton("\u2b06\ufe0f Backup Sources", callback_data="backup")],
    ]
    return InlineKeyboardMarkup(rows)


# =============================================================================
# Admin
# =============================================================================

def admin_users_menu(users: list[UserStat]) -> InlineKeyboardMarkup:
    """Simple admin panel — shows user list with a Home button."""
    rows = [[InlineKeyboardButton("\u2b06\ufe0f Drive Home", callback_data="menu")]]
    return InlineKeyboardMarkup(rows)


# =============================================================================
# Bulk delivery
# =============================================================================

def bulk_progress_keyboard(job_id: str, paused: bool = False) -> InlineKeyboardMarkup:
    """Shown on the progress message while a bulk delivery is running."""
    toggle = (
        InlineKeyboardButton("\u25b6\ufe0f Resume", callback_data=f"bulkresume:{job_id}")
        if paused
        else InlineKeyboardButton("\u23f8 Pause", callback_data=f"bulkpause:{job_id}")
    )
    return InlineKeyboardMarkup(
        [[toggle, InlineKeyboardButton("\u274c Cancel", callback_data=f"bulkcancel:{job_id}")]]
    )


# =============================================================================
# Utility
# =============================================================================

def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("\u2b06\ufe0f Drive Home", callback_data="menu")]]
    )
