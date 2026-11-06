"""Telegram → Bluesky auto-publisher.

Main entry point. Runs two concurrent loops:
  1. Telegram listener (long polling) -> ingests posts into SQLite queue.
  2. Worker -> claims new posts and publishes them to Bluesky as threads.

Also periodically re-queues failed posts for retry.
"""
import logging
import threading
import time

import db
from publisher_queue import run_pending_once, retry_failed, recover_stale

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")


def worker_loop() -> None:
    log.info("worker loop started")
    while True:
        try:
            retry_failed()
            result = run_pending_once()
            if result["processed"] or result["failed"]:
                log.info("worker tick: %s", result)
        except Exception:
            log.exception("worker tick error")
        time.sleep(2)


def main() -> None:
    db.init_db()
    stale = recover_stale()
    if stale:
        log.info("recovered %s stale 'processing' post(s) -> 'new'", stale)

    worker = threading.Thread(target=worker_loop, daemon=True)
    worker.start()

    # Import here to avoid heavy import ordering issues.
    from telegram_listener import poll_forever
    poll_forever()


if __name__ == "__main__":
    main()
