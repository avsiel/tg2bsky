"""Bluesky (AT Protocol) publisher.

Uses only the public, free bsky.social XRPC endpoints:
  - com.atproto.server.createSession
  - com.atproto.repo.uploadBlob
  - com.atproto.repo.createRecord (app.bsky.feed.post)

Text is split into <=300-grapheme threads; media chunks are attached
to the relevant posts in the thread.
"""
import json
import logging
import mimetypes
import time
from typing import Any

import requests

import config

log = logging.getLogger("bluesky")

PDS = "https://bsky.social"

_session: dict | None = None
_session_expiry = 0.0


def _ensure_session() -> str:
    """Return a valid access JWT, refreshing if needed."""
    global _session, _session_expiry
    if _session and time.time() < _session_expiry - 30:
        return _session["accessJwt"]

    r = requests.post(
        f"{PDS}/xrpc/com.atproto.server.createSession",
        json={"identifier": config.BSKY_HANDLE, "password": config.BSKY_APP_PASSWORD},
        timeout=30,
    )
    r.raise_for_status()
    _session = r.json()
    _session_expiry = time.time() + 7200  # access JWT ~2h; refresh if needed
    return _session["accessJwt"]


def _refresh_session() -> str:
    global _session, _session_expiry
    if _session and _session.get("refreshJwt"):
        r = requests.post(
            f"{PDS}/xrpc/com.atproto.server.refreshSession",
            headers={"Authorization": f"Bearer {_session['refreshJwt']}"},
            timeout=30,
        )
        if r.status_code == 200:
            _session = r.json()
            _session_expiry = time.time() + 7200
            return _session["accessJwt"]
    return _ensure_session()


def _auth_headers() -> dict:
    token = _ensure_session()
    return {"Authorization": f"Bearer {token}"}


def _upload_blob(local_path: str, mime: str) -> dict:
    token = _ensure_session()
    with open(local_path, "rb") as f:
        data = f.read()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": mime,
    }
    r = requests.post(
        f"{PDS}/xrpc/com.atproto.repo.uploadBlob",
        headers=headers,
        data=data,
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"uploadBlob failed ({r.status_code}): {r.text[:500]}")
    return r.json()["blob"]


def _split_text(text: str, limit: int = 300) -> list[str]:
    """Split text into chunks of at most `limit` graphemes, preferring to
    break on paragraph/sentence/word boundaries without splitting words."""
    text = (text or "").strip()
    if not text:
        return [""]
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    for para in text.split("\n"):
        para = para.rstrip()
        # Split an over-long paragraph into word-aligned chunks.
        while len(para) > limit:
            cut = _word_break(para, limit)
            chunk = para[:cut].rstrip()
            if chunk:
                chunks.append(chunk)
            para = para[cut:].lstrip()
        if para:
            # Try to merge with the previous chunk to reduce fragmentation.
            if chunks and len(chunks[-1]) + 1 + len(para) <= limit:
                chunks[-1] = chunks[-1] + " " + para
            else:
                chunks.append(para)
    return chunks or [""]


def _word_break(text: str, limit: int) -> int:
    """Return an index to cut text at, not breaking a word when possible."""
    cut = limit
    # prefer last space within limit
    space = text.rfind(" ", 0, limit)
    if space > limit // 2:
        cut = space + 1
    return cut


def _facet_text(text: str) -> str:
    return text


def publish_post(
    text: str,
    images: list[dict] | None = None,
    reply_to: dict | None = None,
) -> dict:
    """Create a single Bluesky post. Returns the record's strongRef dict.

    images: list of {"local_path", "mime", "alt", "kind", "width", "height"}
    reply_to: {"root": {...}, "parent": {...}}
    """
    images = images or []
    embed = None
    if images:
        # Videos and images cannot be mixed in one post (handled upstream);
        # a single video becomes app.bsky.embed.video, everything else images.
        has_video = any(im.get("kind") in ("video", "animation") for im in images)
        if has_video:
            v = images[0]
            blob = _upload_blob(v["local_path"], v.get("mime", "video/mp4"))
            embed = {
                "$type": "app.bsky.embed.video",
                "video": blob,
            }
            if v.get("width") and v.get("height"):
                embed["aspectRatio"] = {"width": v["width"], "height": v["height"]}
            if v.get("alt"):
                embed["alt"] = v["alt"][:1000]
        else:
            images_blobs = []
            for img in images:
                blob = _upload_blob(img["local_path"], img.get("mime", "image/jpeg"))
                images_blobs.append({
                    "alt": (img.get("alt") or "")[:1000],
                    "image": blob,
                })
            embed = {"$type": "app.bsky.embed.images", "images": images_blobs}

    record: dict[str, Any] = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
    }
    if embed:
        record["embed"] = embed
    if reply_to:
        record["reply"] = reply_to

    token = _ensure_session()
    r = requests.post(
        f"{PDS}/xrpc/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "repo": config.BSKY_HANDLE,
            "collection": "app.bsky.feed.post",
            "record": record,
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"createRecord failed ({r.status_code}): {r.text[:500]}")
    body = r.json()
    return {"uri": body["uri"], "cid": body["cid"]}


def _resolve_strongref(uri: str) -> dict:
    """Resolve a post URI to a strongRef (uri + cid) via the API."""
    token = _ensure_session()
    # uri format: at://<did>/app.bsky.feed.post/<rkey>
    parts = uri.split("/")
    if len(parts) < 5:
        raise RuntimeError(f"unexpected uri format: {uri}")
    did = parts[2]
    rkey = parts[-1]
    r = requests.get(
        f"{PDS}/xrpc/com.atproto.repo.getRecord",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "repo": did,
            "collection": "app.bsky.feed.post",
            "rkey": rkey,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"getRecord failed ({r.status_code}): {r.text[:300]}")
    body = r.json()
    return {"uri": body["uri"], "cid": body["cid"]}


def publish_thread(
    text: str,
    media_chunks: list[list[dict]],
    reply_to: tuple[str, str] | None = None,
) -> list[str]:
    """Publish text (and media chunks) as a thread, return list of post URIs.

    reply_to: optional (root_uri, parent_uri) to attach this whole thread as a
    reply under an existing Bluesky post (root=thread root, parent=post to
    reply directly to).
    """
    text_parts = _split_text(text, config.BSKY_MAX_GRAPHEMES)
    n_parts = len(text_parts)
    n_media = len(media_chunks)
    n = max(n_parts, n_media)

    uris: list[str] = []
    root: dict | None = None
    parent: dict | None = None

    # If replying to an existing post, initialize the chain anchors.
    if reply_to:
        root_uri, parent_uri = reply_to
        root = _resolve_strongref(root_uri)
        parent = _resolve_strongref(parent_uri) if parent_uri != root_uri else root

    for i in range(n):
        part_text = text_parts[i] if i < n_parts else ""
        media = media_chunks[i] if i < n_media else []
        reply = None
        if parent and root:
            reply = {"root": root, "parent": parent}
        strong = publish_post(part_text, images=media, reply_to=reply)
        uris.append(strong["uri"])
        if root is None:
            root = strong
        parent = strong
        time.sleep(0.5)

    return uris
