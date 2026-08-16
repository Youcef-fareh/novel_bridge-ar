"""
NovelBridge — WTR-Lab.com site adapter.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List
from urllib.parse import urljoin, urlparse

try:
    from selectolax.parser import HTMLParser
    _SELECTOLAX_AVAILABLE = True
except ImportError:
    _SELECTOLAX_AVAILABLE = False
    from bs4 import BeautifulSoup

from backend.adapters.base import ChapterRef, NovelMeta, SiteAdapter
from backend.adapters.fetcher import (
    fetch_html, parse_attr, parse_chapter_body, parse_links, parse_text
)

_SITE_ID = "wtrlab"
_BASE    = "https://wtr-lab.com"

_cfg_path = Path("config/sites.json")
_SEL: dict = {}
if _cfg_path.exists():
    _SEL = json.loads(_cfg_path.read_text(encoding="utf-8")).get(_SITE_ID, {}).get("selectors", {})


def _split_selectors(value: str) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()]


class WTRLabAdapter(SiteAdapter):
    site_id = _SITE_ID

    def can_handle(self, url: str) -> bool:
        return "wtr-lab.com" in url

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        html = await fetch_html(novel_url, _SITE_ID)

        title = parse_text(html, _split_selectors(_SEL.get("novel_title", "h1, h1.story-title, .story-name"))) or "Unknown Title"
        
        # Author extraction from table/grid or selectors
        author = parse_text(html, _split_selectors(_SEL.get("novel_author", ".story-author a, .author")))
        if not author and _SELECTOLAX_AVAILABLE:
            tree = HTMLParser(html)
            for row in tree.css("div.grid"):
                t = row.text(strip=True)
                if t.startswith("Author"):
                    author = t[6:].strip()
                    break

        cover_url = parse_attr(html, _split_selectors(_SEL.get("novel_cover", ".story-cover img, .story-image img, img.relative")), "src")
        if not cover_url and _SELECTOLAX_AVAILABLE:
            tree = HTMLParser(html)
            for img in tree.css("img"):
                src = img.attributes.get("src") or ""
                if "cdn/series" in src or "series/" in src:
                    cover_url = src
                    break

        description = parse_text(html, _split_selectors(_SEL.get("novel_description", ".leading-relaxed, .story-desc, .description, .synopsis")))
        if not description and _SELECTOLAX_AVAILABLE:
            tree = HTMLParser(html)
            for div in tree.css("div"):
                text = div.text(strip=True)
                if text.startswith("Novel Summary"):
                    description = text[13:].strip()
                    if "DetailsEditHistory" in description:
                        description = description.split("DetailsEditHistory")[0].strip()
                    break

        return NovelMeta(
            title=title,
            author=author,
            cover_url=cover_url,
            description=description,
            source_url=novel_url,
            source_site=_SITE_ID,
        )

    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        html = await fetch_html(novel_url, _SITE_ID)
        selectors = _split_selectors(_SEL.get("chapter_list", ".chapter-list a, .chapter-item a, a[href*='/chapter-']"))
        links = parse_links(html, selectors, base_url=_BASE)

        refs: list[ChapterRef] = []
        seen_urls = set()
        for text, href in links:
            if href and "/chapter-" in href and href not in seen_urls:
                seen_urls.add(href)
                refs.append(ChapterRef(index=len(refs), title=text or f"Chapter {len(refs)+1}", source_url=href))
        return refs

    async def get_chapter_text(self, chapter_url: str) -> str:
        html = await fetch_html(chapter_url, _SITE_ID)
        selectors = _split_selectors(_SEL.get("chapter_text", ".chapter-content, .story-part, #chapter-container, article, main"))
        text = parse_chapter_body(html, selectors)
        if not text:
            text = parse_text(html, [".chapter-content", ".story-part", "article", "main"])
        return text or ""
