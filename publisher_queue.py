"""Processing pipeline: turn a stored post into a Bluesky thread.

Steps:
  1. Rebuild media items from the DB row.
  2. Download + normalize media into Bluesky chunks.
  3. Publish as a thread.
  4. Update DB status (posted / failed with retry).
"""
import logging

import db
import media
from bluesky_publisher import publish_thread
from db import post_row_to_dict

log = logging.getLogger("queue")


def process_post(conn, row) -> tuple[list[str], str | None]:
    """Process a single claimed post. Returns (list of Bluesky URIs, root uri).

    Raises ReplyNotReady if the post is a reply whose target isn't published yet.
    """
    d = post_row_to_dict(row)
    text = d["text"]

    media_items = [
        media.MediaItem(
            kind=m.get("kind", "photo"),
            file_id=m.get("file_id", ""),
            file_unique_id=m.get("file_unique_id", ""),
            mime=m.get("mime", ""),
            width=m.get("width", 0),
            height=m.get("height", 0),
            duration=m.get("duration", 0),
            file_size=m.get("file_size", 0),
            caption=m.get("caption", ""),
        )
        for m in d["media"]
    ]

    chunks, warnings = media.prepare_for_bluesky(media_items)
    for w in warnings:
        log.warning("media warning (post %s): %s", d["id"], w)

    # Resolve reply target if this message is a reply to another channel post.
    reply_to = None
    reply_root_uri = None
    if d.get("reply_to_tg_msg_id"):
        target_row = db.get_by_msg_id(conn, d["chat_id"], d["reply_to_tg_msg_id"])
        if target_row is None:
            # Replied-to message was never ingested (e.g. a sticker) -> publish
            # as a standalone post instead of a reply.
            log.warning("reply target tg msg %s missing; publishing standalone",
                        d["reply_to_tg_msg_id"])
        else:
            target = db.resolve_reply_target(conn, d["chat_id"], d["reply_to_tg_msg_id"])
            if target is None:
                # Target exists but is not posted yet -> wait and retry later.
                raise ReplyNotReady(
                    f"reply target (tg msg {d['reply_to_tg_msg_id']}) not posted yet"
                )
            reply_to = target
            reply_root_uri = target[0]

    uris = publish_thread(text, chunks, reply_to=reply_to)
    return uris, reply_root_uri


class ReplyNotReady(Exception):
    """Raised when a reply's target post is not published yet."""


def run_pending_once() -> dict:
    """Claim and process pending posts. Returns a summary dict."""
    conn = db.connect()
    processed = 0
    failed = 0
    try:
        rows = db.claim_new(conn, limit=1)
        for row in rows:
            try:
                uris, reply_root_uri = process_post(conn, row)
                db.mark_posted(conn, row["id"], uris, reply_root_uri=reply_root_uri)
                conn.commit()
                processed += 1
                log.info("posted %s -> %s", row["id"], uris)
            except ReplyNotReady as e:
                # Target not ready yet: put back to 'new' and retry later.
                log.info("post %s not ready: %s", row["id"], e)
                conn.execute(
                    "UPDATE posts SET status = ? WHERE id = ?",
                    (db._STATUS_NEW, row["id"]),
                )
                conn.commit()
            except Exception as e:
                log.exception("failed to process post %s", row["id"])
                db.mark_failed(conn, row["id"], str(e))
                conn.commit()
                failed += 1
    finally:
        conn.close()
    return {"processed": processed, "failed": failed}


def retry_failed() -> int:
    """Re-queue failed posts that are due for retry."""
    conn = db.connect()
    retried = 0
    try:
        rows = db.get_retryable(conn, limit=10)
        for row in rows:
            db.mark_processing(conn, row["id"])
            conn.commit()
            retried += 1
    finally:
        conn.close()
    return retried


def recover_stale() -> int:
    """Reset posts left in 'processing' after a crash/restart back to 'new',
    so they are picked up again."""
    conn = db.connect()
    n = 0
    try:
        cur = conn.execute(
            "UPDATE posts SET status = ? WHERE status = ?",
            (db._STATUS_NEW, db._STATUS_PROCESSING),
        )
        conn.commit()
        n = cur.rowcount
    finally:
        conn.close()
    return n
