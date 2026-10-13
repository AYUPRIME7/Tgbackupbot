"""Small formatting helpers shared across the bot."""

from __future__ import annotations

from datetime import datetime


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def human_date(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%d %b %Y, %H:%M")


def truncate(text: str, length: int = 45) -> str:
    return text if len(text) <= length else text[: length - 1] + "…"
