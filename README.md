# NoobAyuPrime Backup Bot

## New behavior

- **Sequential Telegram import:** the owner can paste a `t.me/<channel>/<message_id>` or `t.me/c/<internal_id>/<message_id>` link in a private chat. The source must already be linked in Backup Sources or present in `ALLOWED_CHAT_IDS`, and the bot must be able to access it. It attempts IDs in order, skips missing/restricted IDs and unsupported messages, and catalogs supported media in the currently open section or the matching type folder. Each run is capped at 500 IDs; if the cap is reached, paste the suggested next link to continue. This is a bounded sequence of forward requests, not a Telegram history-list operation, so no total-history count is available.
- **Small-server ingestion:** incoming files enter a bounded FIFO queue (up to 500 waiting) and one worker catalogs them at a time. A live progress bar shows processed, waiting, saved, duplicate, and error counts. On Linux, the worker slows down when cgroup RAM usage is high or CPU quota throttling is detected. It stores Telegram file IDs and metadata rather than downloading file bytes into RAM. The queue is in memory, so jobs still waiting when the process is forcibly restarted are not persisted.
- **Section sharing:** open a section and choose **Manage Sharing**. Add a recipient's numeric Telegram ID (they can get it with `/whoami`). Viewer access allows browsing/downloading; Editor access also allows adding/removing files. The owner can change access or revoke it at any time. New section entries show when the recipient refreshes or reopens the section.
- **Readable fallback names:** media without a Telegram-provided filename uses a timestamp and message ID instead of Telegram's opaque file identifier.
- **Duplicate handling:** the same file can be linked to a custom section even if it already appears in a default category. Adding it twice to the same section remains a harmless “already in section” result.

Sharing metadata is stored in the `section_shares` MongoDB collection, created automatically at startup. Existing collections and file records are unchanged.

## Start

Set `BOT_TOKEN`, `OWNER_ID`, and `MONGO_URI` in the runtime environment (or in a local `.env` that is not committed), then run:

```bash
python main.py
```
