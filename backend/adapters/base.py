"""
NovelBridge — Site Adapter base class and data containers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


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
    source_language: str = "Unknown"
    scraping_method: str = "Unknown"

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
        for adapter in cls._adapters.values():
            if adapter.can_handle(url):
                return adapter
        return None

    @classmethod
    def list_all(cls) -> List[dict]:
        return [{"site_id": sid, "class": type(a).__name__} for sid, a in cls._adapters.items()]
