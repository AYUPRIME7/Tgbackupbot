"""
MongoDB data layer for the File Locker Bot (via Motor, the async driver).

Why MongoDB instead of local SQLite
-------------------------------------
On a constrained box (~500 MB disk, ~450 MB RAM) a free-tier MongoDB Atlas
cluster keeps the catalog completely off-box.  Moving to a new server = just
point the new box at the same MONGO_URI.

Multi-user design
-----------------
Every document that belongs to a user carries a ``user_id`` (Telegram user
integer ID) field.  Queries always include a user filter:
  * regular users → {"user_id": uid}
  * owner         → {"$or": [{"user_id": owner_id},
                              {"user_id": {"$exists": False}}]}
  The second clause lets the owner see legacy documents created before the
  multi-user upgrade (those docs have no user_id field).

Collections
-----------
  files          — one doc per catalogued file
  sections       — one doc per user-created section
  section_files  — junction: {section_id, file_id, added_at}
  favorites      — one doc per starred file: {user_id, file_id, starred_at}
  recent_files   — ring buffer: {user_id, file_id, accessed_at}
  backup_sources — one doc per linked group/channel: {chat_id, chat_title,
                   chat_type, owner_id, section_id, created_at}. Links a
                   Telegram chat (where the bot is admin) to a section —
                   every new file posted there from then on is auto-saved
                   into that section. This can only cover files posted
                   *after* linking: Telegram's Bot API has no method for a
                   bot to read a chat's message history from before it
                   joined, so there is no way to retroactively pull in
                   content that already existed there.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

RECENT_LIMIT = 20  # max files kept in the recent ring-buffer


# ---------------------------------------------------------------------------
# Data-transfer objects
# ---------------------------------------------------------------------------

@dataclass
class FileRecord:
    id: str
    file_id: str
    file_unique_id: Optional[str]
    file_type: str
    filename: str
    extension: str
    size: int
    category: str
    upload_date: float
    user_id: int = 0
    is_favorite: bool = field(default=False)

    @classmethod
    def from_doc(cls, doc: dict, *, is_favorite: bool = False) -> "FileRecord":
        return cls(
            id=str(doc["_id"]),
            file_id=doc["file_id"],
            file_unique_id=doc.get("file_unique_id"),
            file_type=doc["file_type"],
            filename=doc["filename"],
            extension=doc["extension"],
            size=doc.get("size", 0),
            category=doc["category"],
            upload_date=doc["upload_date"],
            user_id=doc.get("user_id", 0),
            is_favorite=is_favorite,
        )


@dataclass
class SectionRecord:
    id: str
    name: str
    created_at: float
    count: int = 0


@dataclass
class UserStat:
    user_id: int
    file_count: int
    total_size: int


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class Database:
    """Thin async wrapper around the MongoDB collections."""

    def __init__(self, mongo_uri: str, db_name: str, owner_id: int = 0) -> None:
        self._uri = mongo_uri
        self._db_name = db_name
        self._owner_id = owner_id
        self._client: Optional[AsyncIOMotorClient] = None
        self._files: Optional[AsyncIOMotorCollection] = None
        self._sections: Optional[AsyncIOMotorCollection] = None
        self._section_files: Optional[AsyncIOMotorCollection] = None
        self._favorites: Optional[AsyncIOMotorCollection] = None
        self._recent: Optional[AsyncIOMotorCollection] = None
        self._backup_sources: Optional[AsyncIOMotorCollection] = None

    async def connect(self) -> None:
        self._client = AsyncIOMotorClient(self._uri, maxPoolSize=10, minPoolSize=0)
        db = self._client[self._db_name]

        self._files = db["files"]
        await self._files.create_index("user_id")
        await self._files.create_index([("user_id", 1), ("category", 1)])
        await self._files.create_index("filename")
        await self._files.create_index("upload_date")
        await self._files.create_index("file_unique_id")

        self._sections = db["sections"]
        await self._sections.create_index([("user_id", 1), ("name", 1)])

        self._section_files = db["section_files"]
        await self._section_files.create_index(
            [("section_id", 1), ("file_id", 1)], unique=True
        )
        await self._section_files.create_index("section_id")

        self._favorites = db["favorites"]
        await self._favorites.create_index([("user_id", 1), ("file_id", 1)], unique=True)
        await self._favorites.create_index([("user_id", 1), ("starred_at", -1)])

        self._backup_sources = db["backup_sources"]
        await self._backup_sources.create_index("chat_id", unique=True)
        await self._backup_sources.create_index("owner_id")

        self._recent = db["recent_files"]
        await self._recent.create_index([("user_id", 1), ("file_id", 1)], unique=True)
        await self._recent.create_index([("user_id", 1), ("accessed_at", -1)])

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _oid(file_id: str) -> Optional[ObjectId]:
        try:
            return ObjectId(file_id)
        except (InvalidId, TypeError):
            return None

    def _user_filter(self, user_id: int) -> dict:
        """Return a MongoDB filter that scopes to a specific user.
        Owner also sees legacy docs that pre-date the multi-user upgrade
        (those have no user_id field)."""
        if self._owner_id and user_id == self._owner_id:
            return {
                "$or": [
                    {"user_id": user_id},
                    {"user_id": {"$exists": False}},
                ]
            }
        return {"user_id": user_id}

    # -----------------------------------------------------------------------
    # Files — core CRUD
    # -----------------------------------------------------------------------

    async def add_file(
        self,
        user_id: int,
        file_id: str,
        file_unique_id: Optional[str],
        file_type: str,
        filename: str,
        extension: str,
        size: int,
        category: str,
    ) -> tuple[str, bool]:
        """Insert a new catalog entry. Returns (record_id, is_new)."""
        uf = self._user_filter(user_id)

        existing = None
        if file_unique_id:
            existing = await self._files.find_one({**uf, "file_unique_id": file_unique_id})
        if existing is None:
            existing = await self._files.find_one({**uf, "filename": filename, "size": size})

        if existing is not None:
            return str(existing["_id"]), False

        result = await self._files.insert_one(
            {
                "user_id": user_id,
                "file_id": file_id,
                "file_unique_id": file_unique_id,
                "file_type": file_type,
                "filename": filename,
                "extension": extension,
                "size": size,
                "category": category,
                "upload_date": time.time(),
            }
        )
        return str(result.inserted_id), True

    async def get_file(
        self, file_id: str, user_id: int, *, mark_recent: bool = False
    ) -> Optional[FileRecord]:
        oid = self._oid(file_id)
        if oid is None:
            return None
        doc = await self._files.find_one({"_id": oid})
        if not doc:
            return None
        is_fav = await self._favorites.find_one(
            {"user_id": user_id, "file_id": file_id}
        ) is not None
        if mark_recent:
            await self._touch_recent(file_id, user_id)
        return FileRecord.from_doc(doc, is_favorite=is_fav)

    async def rename_file(self, file_id: str, new_name: str, user_id: int) -> bool:
        oid = self._oid(file_id)
        if oid is None:
            return False
        uf = self._user_filter(user_id)
        result = await self._files.update_one(
            {"_id": oid, **uf}, {"$set": {"filename": new_name}}
        )
        return result.matched_count > 0

    async def delete_file(self, file_id: str, user_id: int) -> bool:
        oid = self._oid(file_id)
        if oid is None:
            return False
        uf = self._user_filter(user_id)
        result = await self._files.delete_one({"_id": oid, **uf})
        if result.deleted_count > 0:
            await self._section_files.delete_many({"file_id": file_id})
            await self._favorites.delete_many({"file_id": file_id})
            await self._recent.delete_many({"file_id": file_id})
        return result.deleted_count > 0

    async def recategorize_file(
        self, file_id: str, new_category: str, user_id: int
    ) -> bool:
        oid = self._oid(file_id)
        if oid is None:
            return False
        uf = self._user_filter(user_id)
        result = await self._files.update_one(
            {"_id": oid, **uf}, {"$set": {"category": new_category}}
        )
        return result.matched_count > 0

    async def list_category(
        self,
        category: str,
        page: int,
        page_size: int,
        user_id: int,
        ascending: bool = False,
    ) -> list[FileRecord]:
        uf = self._user_filter(user_id)
        cursor = (
            self._files.find({**uf, "category": category})
            .sort("upload_date", 1 if ascending else -1)
            .skip(page * page_size)
            .limit(page_size)
        )
        return [FileRecord.from_doc(doc) async for doc in cursor]

    async def list_category_all(
        self, category: str, user_id: int, ascending: bool = False
    ) -> list[FileRecord]:
        """Every file in this category, unpaginated — used for bulk delivery,
        where the whole set is needed rather than one page at a time."""
        uf = self._user_filter(user_id)
        cursor = (
            self._files.find({**uf, "category": category})
            .sort("upload_date", 1 if ascending else -1)
        )
        return [FileRecord.from_doc(doc) async for doc in cursor]

    async def count_category(self, category: str, user_id: int) -> int:
        uf = self._user_filter(user_id)
        return await self._files.count_documents({**uf, "category": category})

    async def category_counts(self, user_id: int) -> dict[str, int]:
        uf = self._user_filter(user_id)
        pipeline = [
            {"$match": uf},
            {"$group": {"_id": "$category", "count": {"$sum": 1}}},
        ]
        counts: dict[str, int] = {}
        async for doc in self._files.aggregate(pipeline):
            counts[doc["_id"]] = doc["count"]
        return counts

    async def search(
        self, query: str, page: int, page_size: int, user_id: int
    ) -> tuple[list[FileRecord], int]:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        uf = self._user_filter(user_id)
        mongo_filter = {**uf, "filename": pattern}
        total = await self._files.count_documents(mongo_filter)
        cursor = (
            self._files.find(mongo_filter)
            .sort("upload_date", -1)
            .skip(page * page_size)
            .limit(page_size)
        )
        return [FileRecord.from_doc(doc) async for doc in cursor], total

    async def search_all(self, query: str, user_id: int) -> list[FileRecord]:
        """Every filename match, unpaginated — used for bulk delivery."""
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        uf = self._user_filter(user_id)
        cursor = self._files.find({**uf, "filename": pattern}).sort("upload_date", -1)
        return [FileRecord.from_doc(doc) async for doc in cursor]

    async def totals(self, user_id: int) -> tuple[int, int]:
        """Return (total_files, total_bytes) for this user."""
        uf = self._user_filter(user_id)
        pipeline = [
            {"$match": uf},
            {"$group": {"_id": None, "count": {"$sum": 1}, "size": {"$sum": "$size"}}},
        ]
        async for doc in self._files.aggregate(pipeline):
            return doc["count"], doc["size"]
        return 0, 0

    # -----------------------------------------------------------------------
    # Favorites
    # -----------------------------------------------------------------------

    async def toggle_favorite(self, file_id: str, user_id: int) -> bool:
        """Star if not starred, unstar if starred. Returns True = now starred."""
        existing = await self._favorites.find_one(
            {"user_id": user_id, "file_id": file_id}
        )
        if existing:
            await self._favorites.delete_one({"user_id": user_id, "file_id": file_id})
            return False
        await self._favorites.insert_one(
            {"user_id": user_id, "file_id": file_id, "starred_at": time.time()}
        )
        return True

    async def list_favorites(
        self, page: int, page_size: int, user_id: int
    ) -> list[FileRecord]:
        cursor = (
            self._favorites.find({"user_id": user_id})
            .sort("starred_at", -1)
            .skip(page * page_size)
            .limit(page_size)
        )
        records: list[FileRecord] = []
        async for link in cursor:
            rec = await self.get_file(link["file_id"], user_id)
            if rec is not None:
                rec.is_favorite = True
                records.append(rec)
        return records

    async def list_favorites_all(self, user_id: int) -> list[FileRecord]:
        """Every starred file, unpaginated — used for bulk delivery."""
        cursor = self._favorites.find({"user_id": user_id}).sort("starred_at", -1)
        records: list[FileRecord] = []
        async for link in cursor:
            rec = await self.get_file(link["file_id"], user_id)
            if rec is not None:
                rec.is_favorite = True
                records.append(rec)
        return records

    async def count_favorites(self, user_id: int) -> int:
        return await self._favorites.count_documents({"user_id": user_id})

    # -----------------------------------------------------------------------
    # Recent files
    # -----------------------------------------------------------------------

    async def _touch_recent(self, file_id: str, user_id: int) -> None:
        """Upsert the file into this user's recent ring-buffer."""
        await self._recent.update_one(
            {"user_id": user_id, "file_id": file_id},
            {"$set": {"user_id": user_id, "file_id": file_id, "accessed_at": time.time()}},
            upsert=True,
        )
        count = await self._recent.count_documents({"user_id": user_id})
        if count > RECENT_LIMIT:
            oldest = (
                await self._recent.find({"user_id": user_id})
                .sort("accessed_at", 1)
                .limit(count - RECENT_LIMIT)
                .to_list(length=count - RECENT_LIMIT)
            )
            ids = [d["_id"] for d in oldest]
            await self._recent.delete_many({"_id": {"$in": ids}})

    async def list_recent(self, user_id: int) -> list[FileRecord]:
        """Return up to RECENT_LIMIT files, most-recently accessed first."""
        cursor = (
            self._recent.find({"user_id": user_id})
            .sort("accessed_at", -1)
            .limit(RECENT_LIMIT)
        )
        records: list[FileRecord] = []
        async for link in cursor:
            rec = await self.get_file(link["file_id"], user_id)
            if rec is not None:
                records.append(rec)
        return records

    # -----------------------------------------------------------------------
    # Sections
    # -----------------------------------------------------------------------

    async def create_section(self, name: str, user_id: int) -> str:
        result = await self._sections.insert_one(
            {"user_id": user_id, "name": name.strip(), "created_at": time.time()}
        )
        return str(result.inserted_id)

    async def get_section(
        self, section_id: str, user_id: Optional[int] = None
    ) -> Optional[SectionRecord]:
        oid = self._oid(section_id)
        if oid is None:
            return None
        query: dict = {"_id": oid}
        if user_id is not None:
            uf = self._user_filter(user_id)
            query.update(uf)
        doc = await self._sections.find_one(query)
        if not doc:
            return None
        count = await self._section_files.count_documents({"section_id": section_id})
        return SectionRecord(
            id=str(doc["_id"]),
            name=doc["name"],
            created_at=doc.get("created_at", 0.0),
            count=count,
        )

    async def rename_section(
        self, section_id: str, new_name: str, user_id: int
    ) -> bool:
        oid = self._oid(section_id)
        if oid is None:
            return False
        uf = self._user_filter(user_id)
        result = await self._sections.update_one(
            {"_id": oid, **uf}, {"$set": {"name": new_name.strip()}}
        )
        return result.matched_count > 0

    async def list_sections(self, user_id: int) -> list[SectionRecord]:
        uf = self._user_filter(user_id)
        sections: list[SectionRecord] = []
        async for doc in self._sections.find(uf).sort("created_at", 1):
            sid = str(doc["_id"])
            count = await self._section_files.count_documents({"section_id": sid})
            sections.append(
                SectionRecord(
                    id=sid,
                    name=doc["name"],
                    created_at=doc.get("created_at", 0.0),
                    count=count,
                )
            )
        return sections

    async def delete_section(self, section_id: str, user_id: int) -> bool:
        oid = self._oid(section_id)
        if oid is None:
            return False
        uf = self._user_filter(user_id)
        result = await self._sections.delete_one({"_id": oid, **uf})
        if result.deleted_count > 0:
            await self._section_files.delete_many({"section_id": section_id})
        return result.deleted_count > 0

    async def add_to_section(self, section_id: str, file_id: str) -> bool:
        """Returns True if newly added, False if already present."""
        try:
            await self._section_files.insert_one(
                {"section_id": section_id, "file_id": file_id, "added_at": time.time()}
            )
            return True
        except Exception:
            return False

    async def remove_from_section(self, section_id: str, file_id: str) -> bool:
        result = await self._section_files.delete_one(
            {"section_id": section_id, "file_id": file_id}
        )
        return result.deleted_count > 0

    async def list_section_files(
        self, section_id: str, page: int, page_size: int, user_id: int
    ) -> list[FileRecord]:
        cursor = (
            self._section_files.find({"section_id": section_id})
            .sort("added_at", -1)
            .skip(page * page_size)
            .limit(page_size)
        )
        records: list[FileRecord] = []
        async for link in cursor:
            rec = await self.get_file(link["file_id"], user_id)
            if rec is not None:
                records.append(rec)
        return records

    async def list_section_files_all(self, section_id: str, user_id: int) -> list[FileRecord]:
        """Every file in this section, unpaginated — used for bulk delivery."""
        cursor = self._section_files.find({"section_id": section_id}).sort("added_at", -1)
        records: list[FileRecord] = []
        async for link in cursor:
            rec = await self.get_file(link["file_id"], user_id)
            if rec is not None:
                records.append(rec)
        return records

    async def count_section_files(self, section_id: str) -> int:
        return await self._section_files.count_documents({"section_id": section_id})

    async def file_sections(self, file_id: str, user_id: int) -> list[str]:
        """Return names of sections (owned by user) that contain this file."""
        names: list[str] = []
        uf = self._user_filter(user_id)
        async for link in self._section_files.find({"file_id": file_id}):
            sec = await self._sections.find_one(
                {"_id": self._oid(link["section_id"]), **uf}
            )
            if sec:
                names.append(sec["name"])
        return names

    async def section_category_counts(self, section_id: str) -> dict[str, int]:
        """Breakdown (photos/videos/archives/...) of one section's files —
        used on the backup-source detail screen."""
        file_ids = []
        async for link in self._section_files.find({"section_id": section_id}, {"file_id": 1}):
            oid = self._oid(link["file_id"])
            if oid is not None:
                file_ids.append(oid)
        if not file_ids:
            return {}
        pipeline = [
            {"$match": {"_id": {"$in": file_ids}}},
            {"$group": {"_id": "$category", "count": {"$sum": 1}}},
        ]
        counts: dict[str, int] = {}
        async for doc in self._files.aggregate(pipeline):
            counts[doc["_id"]] = doc["count"]
        return counts

    # -----------------------------------------------------------------------
    # Backup sources — link a group/channel (bot is admin there) to a
    # section so new files posted there are auto-saved going forward.
    # -----------------------------------------------------------------------

    async def link_backup_source(
        self, chat_id: int, chat_title: str, chat_type: str, owner_id: int, section_id: str
    ) -> None:
        await self._backup_sources.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "chat_title": chat_title,
                    "chat_type": chat_type,
                    "owner_id": owner_id,
                    "section_id": section_id,
                },
                "$setOnInsert": {"created_at": time.time()},
            },
            upsert=True,
        )

    async def get_backup_source_by_chat(self, chat_id: int) -> Optional[dict]:
        return await self._backup_sources.find_one({"chat_id": chat_id})

    async def list_backup_sources(self, owner_id: int) -> list[dict]:
        cursor = self._backup_sources.find({"owner_id": owner_id}).sort("created_at", -1)
        return [doc async for doc in cursor]

    async def unlink_backup_source(self, chat_id: int, owner_id: int) -> bool:
        """Stops future auto-backup for this chat. The section and the
        files already saved from it are left untouched."""
        result = await self._backup_sources.delete_one(
            {"chat_id": chat_id, "owner_id": owner_id}
        )
        return result.deleted_count > 0

    # -----------------------------------------------------------------------
    # Admin — owner-only helpers
    # -----------------------------------------------------------------------

    async def list_users(self) -> list[UserStat]:
        """Return one UserStat per distinct user_id in the files collection.
        Used by the owner admin panel."""
        pipeline = [
            {
                "$group": {
                    "_id": "$user_id",
                    "file_count": {"$sum": 1},
                    "total_size": {"$sum": "$size"},
                }
            },
            {"$sort": {"file_count": -1}},
        ]
        result: list[UserStat] = []
        async for doc in self._files.aggregate(pipeline):
            uid = doc["_id"]
            if uid is None:
                uid = self._owner_id  # legacy docs without user_id
            result.append(
                UserStat(
                    user_id=uid,
                    file_count=doc["file_count"],
                    total_size=doc["total_size"],
                )
            )
        return result
