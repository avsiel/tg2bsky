"""Telegram listener (long polling) that ingests channel posts into the queue.

Albums arrive as multiple messages sharing a media_group_id; we group them
into a single DB entry (led by the first message of the album) so the whole
album is published as one Bluesky thread. Later messages of the same album
update the leader's media/text.
"""
import logging
import time

import requests

import config
import db
import media
from media import tg_api_url

log = logging.getLogger("telegram")

# In-memory map: (chat_id, media_group_id) -> leader telegram_msg_id
_album_leaders: dict[tuple[int, str], int] = {}


def _media_to_dicts(items: list[media.MediaItem]) -> list[dict]:
    return [
        {
            "kind": it.kind,
            "file_id": it.file_id,
            "file_unique_id": it.file_unique_id,
            "mime": it.mime,
            "width": it.width,
            "height": it.height,
            "duration": it.duration,
            "file_size": it.file_size,
            "caption": it.caption,
        }
        for it in items
    ]


def handle_message(message: dict) -> bool:
    """Ingest one Telegram message. Returns True if a new post was created."""
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if not chat_id:
        return False

    # Only ingest from the configured source channel.
    if str(chat_id) != str(config.TELEGRAM_SOURCE_CHAT_ID):
        return False

    msg_id = message.get("message_id")

    # Ignore stickers entirely: nothing should be published to Bluesky.
    if message.get("sticker"):
        log.info("ignoring sticker message %s", msg_id)
        return False

    text = (message.get("text") or message.get("caption") or "").strip()
    media_group_id = message.get("media_group_id")
    media_items = media.extract_media(message)
    media_dicts = _media_to_dicts(media_items)

    # Detect a reply: replying to our own channel post threads it in Bluesky.
    reply_to_tg_msg_id = None
    reply = message.get("reply_to_message")
    if reply and reply.get("message_id"):
        reply_to_tg_msg_id = reply["message_id"]

    conn = db.connect()
    created = False
    try:
        if media_group_id:
            key = (chat_id, media_group_id)
            leader = _album_leaders.get(key)
            if leader is None:
                # First message of the album -> create the leader entry.
                created = db.upsert_incoming(
                    conn, chat_id, msg_id, media_group_id, text, media_dicts,
                    reply_to_tg_msg_id=reply_to_tg_msg_id,
                )
                if created:
                    _album_leaders[key] = msg_id
            else:
                # Fold this message into the existing leader.
                row = db.get_by_msg_id(conn, chat_id, leader)
                if row:
                    merged = _media_to_dicts(_merge_media(row, media_items))
                    merged_text = _merge_text(row["text"], text)
                    db.update_message(conn, chat_id, leader, merged_text, merged)
                    conn.commit()
        else:
            created = db.upsert_incoming(
                conn, chat_id, msg_id, None, text, media_dicts,
                reply_to_tg_msg_id=reply_to_tg_msg_id,
            )
        conn.commit()
    finally:
        conn.close()
    return created


def _merge_media(row, new_items: list[media.MediaItem]) -> list[media.MediaItem]:
    existing = [
        media.MediaItem(**m) for m in db.post_row_to_dict(row)["media"]
    ]
    seen = {m.file_unique_id for m in existing}
    for it in new_items:
        if it.file_unique_id not in seen:
            existing.append(it)
            seen.add(it.file_unique_id)
    return existing


def _merge_text(existing: str, new: str) -> str:
    if not existing:
        return new
    if new and new != existing:
        return f"{existing}\n{new}"
    return existing


def poll_forever() -> None:
    """Run the long-polling loop, feeding a processing loop."""
    offset = 0
    log.info("Telegram listener started for chat %s", config.TELEGRAM_SOURCE_CHAT_ID)
    while True:
        try:
            r = requests.get(
                tg_api_url("getUpdates"),
                params={"offset": offset, "timeout": config.TG_POLL_TIMEOUT},
                timeout=config.TG_POLL_TIMEOUT + 10,
            )
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                log.error("getUpdates error: %s", data)
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = max(offset, upd["update_id"] + 1)
                msg = upd.get("message") or upd.get("channel_post")
                if msg:
                    try:
                        if "message" in upd and (msg.get("text") or "").startswith("/"):
                            # Direct message to the bot -> management command.
                            from bot_commands import handle_command
                            handle_command(msg)
                        else:
                            handle_message(msg)
                    except Exception:
                        log.exception("failed to ingest message")
        except requests.RequestException as e:
            log.error("network error in poll loop: %s", e)
            time.sleep(5)
        except Exception:
            log.exception("unexpected error in poll loop")
            time.sleep(5)
