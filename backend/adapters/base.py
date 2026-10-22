"""
NovelBridge — Site Adapter base class and data containers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
import logging
from typing import List, Optional
from urllib.parse import urlparse

from backend.adapters.preferences import get_preferences

logger = logging.getLogger("novelbridge.adapters")


def _rejection_reason(url: str) -> Optional[str]:
    from backend.config import resolve_path

    path = resolve_path("config/_rejected.json")
    if not path.exists():
        return None
    try:
        rejected = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.exception("Could not read rejected-source registry at %s", path)
        raise RuntimeError(f"Could not read rejected-source registry: {path}") from exc
    if not isinstance(rejected, dict):
        raise ValueError(f"{path} must contain a JSON object")
    host = (urlparse(url).hostname or "").lower()
    for rejected_url, reason in rejected.items():
        if (urlparse(rejected_url).hostname or "").lower() == host:
            return str(reason)
    return None


def host_matches(url: str, domains: tuple[str, ...]) -> bool:
    """Match an HTTP(S) URL against exact domains and their subdomains."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


@dataclass
class NovelMeta:
    """Scraped metadata for a novel."""
    title:       str
    author:      Optional[str]  = None
    cover_url:   Optional[str]  = None
    description: Optional[str]  = None
    source_url:  str            = ""
    source_site: str            = ""


@dataclass
class ChapterRef:
    """A reference to a chapter (URL + display info)."""
    index:     int
    title:     str
    source_url: str
    volume_serial: Optional[int] = None
    volume_title: Optional[str] = None


@dataclass
class SearchResult:
    title: str
    url: str
    source_site: str
    cover_url: Optional[str] = None


class SiteAdapter(ABC):
    """
    Abstract base class for web novel site adapters.
    Adding a new site = creating one new file implementing this interface.
    """

    # Unique identifier for the site, e.g. "novelfire" or "wtrlab"
    site_id: str = ""

    # Whether this source is already in Arabic (no translation needed)
    is_native_arabic: bool = False

    # Optional registry metadata for custom adapters.
    source_language: str = "English"
    category: str = ""
    scraping_method: str = "Unknown"
    can_search: bool = False

    @abstractmethod
    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        """Fetch and return novel metadata (title, author, cover, description)."""
        ...

    @abstractmethod
    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        """Fetch and return an ordered list of chapter references."""
        ...

    @abstractmethod
    async def get_chapter_text(self, chapter_url: str) -> str:
        """Fetch and return the plain text body of a single chapter."""
        ...

    async def get_chapter_images(self, chapter_url: str) -> list[dict[str, str]]:
        """Return chapter image URL and alt-text records when supported."""
        return []

    async def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        """Search this source when ``can_search`` is enabled."""
        raise NotImplementedError(f"{type(self).__name__} does not support search")

    def can_handle(self, url: str) -> bool:
        """Return True if this adapter recognises the given URL."""
        return False


class AdapterRegistry:
    """Simple registry mapping site_id → SiteAdapter instance."""

    _adapters: dict[str, SiteAdapter] = {}

    @classmethod
    def register(cls, adapter: SiteAdapter) -> None:
        cls._adapters[adapter.site_id] = adapter

    @classmethod
    def find(cls, url: str) -> Optional[SiteAdapter]:
        enabled_adapters = get_preferences()["enabled_adapters"]
        for adapter in cls._adapters.values():
            if enabled_adapters.get(adapter.site_id, True) is not False and adapter.can_handle(url):
                reason = _rejection_reason(url)
                if reason:
                    logger.warning(
                        "Source %s is marked rejected (%s) but remains enabled for diagnosis",
                        adapter.site_id,
                        reason,
                    )
                return adapter
        return None

    @classmethod
    def list_all(cls) -> List[dict]:
        preferences = get_preferences()
        return [
            {
                "site_id": sid,
                "class": type(adapter).__name__,
                "source_language": getattr(adapter, "source_language", "Unknown"),
                "category": getattr(
                    adapter,
                    "category",
                    "",
                ) or ("arabic" if getattr(adapter, "is_native_arabic", False) else "webnovel"),
                "enabled": preferences["enabled_adapters"].get(sid, True) is not False,
                "is_native_arabic": bool(
                    getattr(adapter, "is_native_arabic", False)
                ),
                "can_search": bool(getattr(adapter, "can_search", False)),
                "base_urls": list(getattr(adapter, "_base_urls", [])),
                "supports_chapter_images": (
                    type(adapter).get_chapter_images is not SiteAdapter.get_chapter_images
                ),
            }
            for sid, adapter in cls._adapters.items()
        ]
