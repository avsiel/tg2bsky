"""Management commands exposed through the Telegram bot.

Available (admin users only):
  /start   - who am I / help
  /status  - queue summary
  /queue   - pending posts
  /errors  - recent failures
  /retry   - re-queue due failed posts
  /recent  - latest published posts
"""
import logging
from typing import Callable

import requests

import config
import db
from media import tg_api_url
from publisher_queue import retry_failed

log = logging.getLogger("commands")


def _is_admin(user_id: int) -> bool:
    if not config.ADMIN_USER_IDS:
        return True  # if unset, allow anyone (dev); set in .env for prod
    return user_id in config.ADMIN_USER_IDS


def _send(chat_id: int, text: str) -> None:
    requests.post(
        tg_api_url("sendMessage"),
        json={"chat_id": chat_id, "text": text},
        timeout=20,
    )


def _fmt_time(ts) -> str:
    if not ts:
        return "-"
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _status_text() -> str:
    conn = db.connect()
    try:
        s = db.stats(conn)
    finally:
        conn.close()
    lines = [f"Queue: {s['total']} total"]
    for st in ("new", "processing", "posted", "failed", "skipped"):
        n = s["by_status"].get(st, 0)
        if n:
            lines.append(f"  {st}: {n}")
    return "\n".join(lines)


def _queue_text() -> str:
    conn = db.connect()
    try:
        rows = db.recent(conn, limit=15)
        pending = [r for r in rows if r["status"] in ("new", "processing")]
    finally:
        conn.close()
    if not pending:
        return "No pending posts."
    lines = []
    for r in pending:
        lines.append(f"#{r['id']} [{r['status']}] {r['text'][:60]}")
    return "\n".join(lines)


def _errors_text() -> str:
    conn = db.connect()
    try:
        rows = db.recent_errors(conn, limit=10)
    finally:
        conn.close()
    if not rows:
        return "No errors."
    lines = []
    for r in rows:
        lines.append(f"#{r['id']} attempts={r['attempts']}: {r['error'][:100]}")
    return "\n".join(lines)


def _recent_text() -> str:
    conn = db.connect()
    try:
        rows = db.recent(conn, limit=10)
        posted = [r for r in rows if r["status"] == "posted"]
    finally:
        conn.close()
    if not posted:
        return "Nothing published yet."
    lines = []
    for r in posted:
        uris = db._deserialize_list(r["bsky_uris"])
        lines.append(f"#{r['id']} @ {_fmt_time(r['published_at'])} -> {len(uris)} post(s)")
    return "\n".join(lines)


def _help_text() -> str:
    return (
        "Commands:\n"
        "/status - queue summary\n"
        "/queue - pending posts\n"
        "/errors - recent failures\n"
        "/retry - re-queue failed posts\n"
        "/recent - latest published"
    )


def handle_command(message: dict) -> None:
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    text = (message.get("text") or "").strip()

    if not chat_id or not user_id:
        return
    if not _is_admin(user_id):
        _send(chat_id, "Access denied.")
        return

    cmd = text.split()[0].split("@")[0] if text else ""
    handlers: dict[str, Callable[[], str]] = {
        "/start": _help_text,
        "/help": _help_text,
        "/status": _status_text,
        "/queue": _queue_text,
        "/errors": _errors_text,
        "/recent": _recent_text,
    }
    if cmd in handlers:
        _send(chat_id, handlers[cmd]())
    elif cmd == "/retry":
        n = retry_failed()
        _send(chat_id, f"Re-queued {n} failed post(s) for retry.")
    else:
        _send(chat_id, _help_text())
