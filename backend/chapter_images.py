"""Bounded, public-network-only chapter image downloads."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from backend.config import get_config
from backend.database import get_chapter_images, replace_chapter_images
from backend.models import ChapterImage

logger = logging.getLogger("novelbridge.chapter_images")

MAX_IMAGES_PER_CHAPTER = 8
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
_IMAGE_TYPES = {
    "image/jpeg": (".jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
    "image/png": (".png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/gif": (".gif", lambda data: data.startswith((b"GIF87a", b"GIF89a"))),
    "image/webp": (".webp", lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP"),
}


def _assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Chapter image URL must be an absolute HTTP(S) URL.")
    try:
        addresses = {
            result[4][0]
            for result in socket.getaddrinfo(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
            )
        }
    except OSError as exc:
        raise ValueError(f"Could not resolve chapter image host {parsed.hostname}.") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Chapter image host must resolve only to public IP addresses.")


async def _download_image(client: httpx.AsyncClient, url: str) -> tuple[bytes, str]:
    current_url = url
    for redirect_count in range(MAX_REDIRECTS + 1):
        await asyncio.to_thread(_assert_public_url, current_url)
        async with client.stream("GET", current_url, follow_redirects=False) as response:
            if response.is_redirect:
                if redirect_count == MAX_REDIRECTS:
                    raise ValueError("Chapter image exceeded the redirect limit.")
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Chapter image redirect did not include a location.")
                current_url = urljoin(current_url, location)
                continue
            response.raise_for_status()
            media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if media_type not in _IMAGE_TYPES:
                raise ValueError(f"Unsupported chapter image media type: {media_type or 'missing'}.")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise ValueError("Chapter image exceeds the 2 MiB size limit.")
                chunks.append(chunk)
        image = b"".join(chunks)
        if not _IMAGE_TYPES[media_type][1](image):
            raise ValueError("Chapter image content does not match its media type.")
        return image, media_type
    raise ValueError("Chapter image redirect handling failed.")


async def store_chapter_images(
    novel_id: int,
    chapter_id: int,
    images: list[dict[str, str]],
) -> list[ChapterImage]:
    """Fetch, validate, and atomically replace a chapter's bounded image set."""
    bounded = images[:MAX_IMAGES_PER_CHAPTER]
    root = (get_config().paths.data_dir / "chapter-images").resolve()
    if not bounded:
        previous = get_chapter_images(chapter_id)
        replace_chapter_images(chapter_id, [])
        for stored in previous:
            old_path = Path(stored.file_path)
            if old_path.resolve().is_relative_to(root):
                old_path.unlink(missing_ok=True)
        return []

    records: list[dict] = []
    seen_digests: set[str] = set()
    total_bytes = 0
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        for image in bounded:
            try:
                data, media_type = await _download_image(client, image["url"])
                if total_bytes + len(data) > 12 * 1024 * 1024:
                    logger.warning(
                        "Skipping chapter image because chapter %s exceeds its 12 MiB image budget",
                        chapter_id,
                    )
                    break
                total_bytes += len(data)
                digest = hashlib.sha256(data).hexdigest()
                if digest in seen_digests:
                    continue
                seen_digests.add(digest)
                extension = _IMAGE_TYPES[media_type][0]
                destination = root / str(novel_id) / str(chapter_id) / f"{digest}{extension}"
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.resolve().is_relative_to(root):
                    raise ValueError("Chapter image path escapes the image store.")
                temporary = destination.with_suffix(destination.suffix + ".tmp")
                temporary.write_bytes(data)
                os.replace(temporary, destination)
                records.append({
                    "image_id": digest[:24],
                    "file_path": str(destination),
                    "media_type": media_type,
                    "alt_text": image.get("alt_text"),
                    "size_bytes": len(data),
                })
            except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
                logger.warning(
                    "Could not store image for chapter %s from %r: %s",
                    chapter_id,
                    image.get("url"),
                    exc,
                )

    previous = get_chapter_images(chapter_id)
    if not records and previous:
        logger.warning(
            "Keeping %d previously stored chapter images after all current image downloads failed",
            len(previous),
        )
        return previous
    stored = replace_chapter_images(chapter_id, records)
    retained = {record["file_path"] for record in records}
    for old_image in previous:
        old_path = Path(old_image.file_path)
        if str(old_path) not in retained and old_path.resolve().is_relative_to(root):
            old_path.unlink(missing_ok=True)
    return stored
