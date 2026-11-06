"""SQLite storage for the publishing queue.

Stores the minimum necessary to reliably publish without duplicates:
  - Telegram message ids (and album grouping)
  - extracted text and media references
  - processing status and error info
  - resulting Bluesky post URIs

No personal user data beyond what is needed to publish is kept.
"""
import json
import sqlite3
import time
from typing import Any

import config

_STATUS_NEW = "new"
_STATUS_PROCESSING = "processing"
_STATUS_POSTED = "posted"
_STATUS_FAILED = "failed"
_STATUS_SKIPPED = "skipped"


def _now() -> float:
    return time.time()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_msg_id INTEGER NOT NULL,
                media_group_id  TEXT,
                chat_id         INTEGER NOT NULL,
                text            TEXT NOT NULL DEFAULT '',
                media           TEXT NOT NULL DEFAULT '[]',
                status          TEXT NOT NULL DEFAULT 'new',
                attempts        INTEGER NOT NULL DEFAULT 0,
                next_retry_at   REAL,
                bsky_uris       TEXT NOT NULL DEFAULT '[]',
                reply_to_tg_msg_id INTEGER,
                reply_root_uri  TEXT,
                received_at     REAL NOT NULL,
                published_at    REAL,
                error           TEXT,
                UNIQUE(chat_id, telegram_msg_id)
            );

            CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
            CREATE INDEX IF NOT EXISTS idx_posts_media_group
                ON posts(chat_id, media_group_id);
            """
        )
        # Migration: add reply_to_tg_msg_id column if this DB predates it.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(posts)")}
        if "reply_to_tg_msg_id" not in cols:
            conn.execute(
                "ALTER TABLE posts ADD COLUMN reply_to_tg_msg_id INTEGER"
            )
        if "reply_root_uri" not in cols:
            conn.execute(
                "ALTER TABLE posts ADD COLUMN reply_root_uri TEXT"
            )


def _serialize_media(media: list[dict]) -> str:
    return json.dumps(media, ensure_ascii=False)


def _deserialize_media(raw: str) -> list[dict]:
    try:
        return json.loads(raw)
    except Exception:
        return []


def _deserialize_list(raw: str) -> list[str]:
    try:
        return json.loads(raw)
    except Exception:
        return []


def upsert_incoming(
    conn: sqlite3.Connection,
    chat_id: int,
    msg_id: int,
    media_group_id: str | None,
    text: str,
    media: list[dict],
    reply_to_tg_msg_id: int | None = None,
) -> bool:
    """Insert a new incoming message. Returns True if it was newly added
    (False if a message with this id was already seen -> duplicate)."""
    cur = conn.execute(
        "SELECT id FROM posts WHERE chat_id = ? AND telegram_msg_id = ?",
        (chat_id, msg_id),
    )
    if cur.fetchone():
        return False
    conn.execute(
        """
        INSERT INTO posts
            (telegram_msg_id, media_group_id, chat_id, text, media,
             status, attempts, reply_to_tg_msg_id, received_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (msg_id, media_group_id, chat_id, text, _serialize_media(media),
         _STATUS_NEW, reply_to_tg_msg_id, _now()),
    )
    return True


def update_message(
    conn: sqlite3.Connection,
    chat_id: int,
    msg_id: int,
    text: str,
    media: list[dict],
) -> None:
    conn.execute(
        "UPDATE posts SET text = ?, media = ? WHERE chat_id = ? AND telegram_msg_id = ?",
        (text, _serialize_media(media), chat_id, msg_id),
    )


def get_by_msg_id(conn: sqlite3.Connection, chat_id: int, msg_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM posts WHERE chat_id = ? AND telegram_msg_id = ?",
        (chat_id, msg_id),
    ).fetchone()


def resolve_reply_target(conn: sqlite3.Connection, chat_id: int, msg_id: int) -> tuple[str, str] | None:
    """Resolve (root_uri, parent_uri) for replying to Telegram message `msg_id`.

    Returns None if the target message is not yet posted (caller should retry).
    """
    row = get_by_msg_id(conn, chat_id, msg_id)
    if not row:
        return None
    if row["status"] != _STATUS_POSTED:
        return None
    uris = _deserialize_list(row["bsky_uris"])
    if not uris:
        return None
    parent = uris[-1]
    # Root: if the target itself was a reply, use its stored root; otherwise
    # it is a top-level post and its first URI is the root.
    root = row["reply_root_uri"] or uris[0]
    return (root, parent)


def get_album_rows(conn: sqlite3.Connection, chat_id: int, media_group_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM posts
        WHERE chat_id = ? AND media_group_id = ?
        ORDER BY telegram_msg_id ASC
        """,
        (chat_id, media_group_id),
    ).fetchall()


def claim_new(conn: sqlite3.Connection, limit: int = 1) -> list[sqlite3.Row]:
    """Atomically claim the oldest 'new' post(s) and mark them 'processing'."""
    rows = conn.execute(
        """
        SELECT id FROM posts
        WHERE status = ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (_STATUS_NEW, limit),
    ).fetchall()
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    conn.executemany(
        "UPDATE posts SET status = ?, attempts = attempts + 1 WHERE id = ?",
        [(_STATUS_PROCESSING, i) for i in ids],
    )
    conn.commit()
    placeholders = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT * FROM posts WHERE id IN ({placeholders}) ORDER BY id ASC",
        ids,
    ).fetchall()


def get_retryable(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    now = _now()
    return conn.execute(
        """
        SELECT * FROM posts
        WHERE status = ? AND attempts < ? AND next_retry_at <= ?
        ORDER BY next_retry_at ASC
        LIMIT ?
        """,
        (_STATUS_FAILED, config.MAX_RETRIES, now, limit),
    ).fetchall()


def mark_failed(conn: sqlite3.Connection, post_id: int, error: str) -> None:
    delay = config.RETRY_BASE_DELAY * (2 ** _attempt_count(conn, post_id))
    conn.execute(
        """
        UPDATE posts SET status = ?, error = ?, next_retry_at = ?
        WHERE id = ?
        """,
        (_STATUS_FAILED, error[:2000], _now() + delay, post_id),
    )


def _attempt_count(conn: sqlite3.Connection, post_id: int) -> int:
    row = conn.execute("SELECT attempts FROM posts WHERE id = ?", (post_id,)).fetchone()
    return row["attempts"] if row else 0


def mark_posted(
    conn: sqlite3.Connection,
    post_id: int,
    bsky_uris: list[str],
    reply_root_uri: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE posts SET status = ?, bsky_uris = ?, reply_root_uri = ?,
               published_at = ?, error = NULL
        WHERE id = ?
        """,
        (_STATUS_POSTED, json.dumps(bsky_uris, ensure_ascii=False),
         reply_root_uri, _now(), post_id),
    )


def mark_processing(conn: sqlite3.Connection, post_id: int) -> None:
    conn.execute(
        "UPDATE posts SET status = ?, attempts = attempts + 1 WHERE id = ?",
        (_STATUS_PROCESSING, post_id),
    )


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT status, COUNT(*) AS n FROM posts GROUP BY status
        """
    ).fetchall()
    by_status = {r["status"]: r["n"] for r in row}
    total = sum(by_status.values())
    return {"total": total, "by_status": by_status}


def recent(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def recent_errors(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT * FROM posts
        WHERE status = ? AND error IS NOT NULL
        ORDER BY id DESC LIMIT ?
        """,
        (_STATUS_FAILED, limit),
    ).fetchall()


def post_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["media"] = _deserialize_media(d.get("media", "[]"))
    d["bsky_uris"] = _deserialize_list(d.get("bsky_uris", "[]"))
    return d
