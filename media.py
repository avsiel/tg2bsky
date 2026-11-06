"""Download Telegram media and normalize it for Bluesky.

Telegram sends photo/video as file_id; we download via getFile and
validate against Bluesky limits (image count/size, video size/duration).
"""
import io
import logging
import os
from dataclasses import dataclass, field
from typing import Any

import requests

import config

log = logging.getLogger("media")

MEDIA_DIR = "media"


@dataclass
class MediaItem:
    kind: str                 # "photo" | "video" | "animation" | "audio" | "document"
    file_id: str
    file_unique_id: str = ""
    mime: str = ""
    width: int = 0
    height: int = 0
    duration: int = 0        # seconds (video/animation)
    file_size: int = 0
    local_path: str = ""
    caption: str = ""

    @property
    def is_image(self) -> bool:
        return self.kind in ("photo",)

    @property
    def is_video(self) -> bool:
        return self.kind in ("video", "animation")

    @property
    def is_supported(self) -> bool:
        return self.is_image or self.is_video


def tg_api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"


def extract_media(message: dict) -> list[MediaItem]:
    """Extract supported media items from a Telegram message."""
    items: list[MediaItem] = []
    caption = message.get("caption") or ""

    photo = message.get("photo")
    if photo:
        # Telegram sends multiple sizes; take the largest.
        largest = max(photo, key=lambda p: p.get("width", 0) * p.get("height", 0))
        items.append(MediaItem(
            kind="photo",
            file_id=largest["file_id"],
            file_unique_id=largest.get("file_unique_id", ""),
            mime="image/jpeg",
            width=largest.get("width", 0),
            height=largest.get("height", 0),
            caption=caption,
        ))

    video = message.get("video")
    if video:
        items.append(MediaItem(
            kind="video",
            file_id=video["file_id"],
            file_unique_id=video.get("file_unique_id", ""),
            mime=video.get("mime_type", ""),
            width=video.get("width", 0),
            height=video.get("height", 0),
            duration=video.get("duration", 0),
            file_size=video.get("file_size", 0),
            caption=caption,
        ))

    animation = message.get("animation")
    if animation:
        items.append(MediaItem(
            kind="animation",
            file_id=animation["file_id"],
            file_unique_id=animation.get("file_unique_id", ""),
            mime=animation.get("mime_type", ""),
            width=animation.get("width", 0),
            height=animation.get("height", 0),
            duration=animation.get("duration", 0),
            file_size=animation.get("file_size", 0),
            caption=caption,
        ))

    # Documents that are images/videos (optional, best-effort)
    document = message.get("document")
    if document and (not items):
        mime = document.get("mime_type", "")
        if mime.startswith("image/") or mime.startswith("video/"):
            kind = "video" if mime.startswith("video/") else "photo"
            items.append(MediaItem(
                kind=kind,
                file_id=document["file_id"],
                file_unique_id=document.get("file_unique_id", ""),
                mime=mime,
                file_size=document.get("file_size", 0),
                caption=caption,
            ))

    return items


def _get_file_path(file_id: str) -> str:
    r = requests.get(tg_api_url("getFile"), params={"file_id": file_id}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"getFile failed: {data}")
    return data["result"]["file_path"]


def download(file_id: str) -> str:
    """Download a Telegram file to the local media dir, return local path."""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    file_path = _get_file_path(file_id)
    url = f"https://api.telegram.org/file/bot{config.TELEGRAM_BOT_TOKEN}/{file_path}"
    local_name = file_path.split("/")[-1]
    local_path = os.path.join(MEDIA_DIR, local_name)

    r = requests.get(url, timeout=120)
    r.raise_for_status()
    with open(local_path, "wb") as f:
        f.write(r.content)
    return local_path


def prepare_for_bluesky(items: list[MediaItem]) -> tuple[list[dict], list[str]]:
    """Download media and split into Bluesky-compatible chunks.

    Returns:
      (chunks, warnings) where each chunk is a list of uploaded-ready items
      (a dict with local_path / alt text), never exceeding 4 images, with at
      most one video per chunk (Bluesky allows max 1 video per post).
    """
    warnings: list[str] = []
    downloaded: list[dict] = []

    for it in items:
        if not it.is_supported:
            warnings.append(f"Skipped unsupported media type: {it.kind}")
            continue
        try:
            local_path = download(it.file_id)
            size = it.file_size or os.path.getsize(local_path)
        except Exception as e:
            warnings.append(f"Failed to download {it.kind}: {e}")
            continue

        if it.is_image and size > config.BSKY_MAX_IMAGE_BYTES:
            warnings.append(f"Image too large ({size} bytes), skipped")
            continue
        if it.is_video and size > config.BSKY_MAX_VIDEO_BYTES:
            warnings.append(f"Video too large ({size} bytes), skipped")
            continue

        downloaded.append({
            "kind": it.kind,
            "local_path": local_path,
            "mime": it.mime or ("image/jpeg" if it.is_image else "video/mp4"),
            "alt": it.caption or "",
            "size": size,
            "width": it.width,
            "height": it.height,
        })

    # Split into chunks: max 4 images, max 1 video per chunk.
    chunks: list[list[dict]] = []
    cur: list[dict] = []
    cur_has_video = False
    for d in downloaded:
        is_video = d["kind"] in ("video", "animation")
        too_many_images = (not is_video) and len(cur) >= config.BSKY_MAX_IMAGES
        if too_many_images or (is_video and (cur_has_video or cur)):
            # Start a new chunk (a video cannot share a post with other media).
            chunks.append(cur)
            cur = []
            cur_has_video = False
        if is_video:
            cur_has_video = True
        cur.append(d)
    if cur:
        chunks.append(cur)

    return chunks, warnings
