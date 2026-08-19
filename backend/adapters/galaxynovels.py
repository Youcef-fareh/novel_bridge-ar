"""
NovelBridge — GalaxyNovels.com (Arabic) site adapter.
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
    fetch_html, fetch_html_playwright, parse_attr, parse_chapter_body,
    parse_links, parse_text,
)

_SITE_ID = "galaxynovels"
_BASE    = "https://galaxynovels.com"

# Load selectors from config
_cfg_path = Path("config/sites.json")
_SEL: dict = {}
if _cfg_path.exists():
    _SEL = json.loads(_cfg_path.read_text(encoding="utf-8")).get(_SITE_ID, {}).get("selectors", {})


def _split_selectors(value: str) -> list[str]:
    """Split a comma-separated CSS selector string."""
    return [s.strip() for s in value.split(",") if s.strip()]


async def _fetch_galaxy_html(url: str, wait_for: str = "") -> str:
    """Use HTTP first, then Playwright when Galaxy blocks the request."""
    try:
        return await fetch_html(url, _SITE_ID)
    except Exception:
        return await fetch_html_playwright(url, wait_for)


class GalaxyNovelsAdapter(SiteAdapter):
    site_id = _SITE_ID
    is_native_arabic = True

    def can_handle(self, url: str) -> bool:
        return "galaxynovels.com" in url or "galaxynovel" in url

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        html = await _fetch_galaxy_html(novel_url, "h1")

        title = parse_text(html, _split_selectors(_SEL.get("novel_title", "h1, .wor-single-title"))) or "Unknown Title"
        author = parse_text(html, _split_selectors(_SEL.get("novel_author", ".author, .wor-author a")))
        
        # If author not found in standard selectors, search for author metadata in meta tags or descriptions
        if not author and _SELECTOLAX_AVAILABLE:
            tree = HTMLParser(html)
            desc_meta = tree.css_first("meta[name=description]")
            if desc_meta:
                m_text = desc_meta.attributes.get("content", "")
                match = re.search(r"المؤلف\s+([^على]+?)\s+على", m_text)
                if match:
                    author = match.group(1).strip()

        cover_url = parse_attr(html, _split_selectors(_SEL.get("novel_cover", ".wor-single-thumb img, .wor-single-cover img, .wp-post-image")), "src")
        if not cover_url:
            cover_url = parse_attr(html, _split_selectors(_SEL.get("novel_cover", "img")), "data-src")
        if cover_url and cover_url.startswith("/"):
            cover_url = urljoin(_BASE, cover_url)

        description = parse_text(html, _split_selectors(_SEL.get("novel_description", ".wor-single-summary__text, .description")))

        if not title or title == "Unknown Title" or not description:
            html = await fetch_html_playwright(novel_url, "h1")
            title = parse_text(html, _split_selectors(_SEL.get("novel_title", "h1, .wor-single-title"))) or title
            author = parse_text(html, _split_selectors(_SEL.get("novel_author", ".author, .wor-author a"))) or author
            description = parse_text(html, _split_selectors(_SEL.get("novel_description", ".wor-single-summary__text, .description"))) or description

        return NovelMeta(
            title=title,
            author=author,
            cover_url=cover_url,
            description=description,
            source_url=novel_url,
            source_site=_SITE_ID,
        )

    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        html = await _fetch_galaxy_html(novel_url, ".wor-novel-chapters-list, [data-index-url]")
        
        refs: list[ChapterRef] = []
        seen_urls = set()

        # Method 1: Check for data-index-url (GalaxyNovels full JSON chapter manifest with all 1000+ chapters)
        if _SELECTOLAX_AVAILABLE:
            tree = HTMLParser(html)
            container = tree.css_first(".wor-novel-chapters-wrap, [data-index-url]")
            if container and container.attributes.get("data-index-url"):
                index_url = container.attributes.get("data-index-url")
                try:
                    json_str = await fetch_html(index_url, _SITE_ID)
                    data = json.loads(json_str)
                    raw_chapters = data.get("chapters", [])
                    for item in raw_chapters:
                        ch_url = item.get("url")
                        if ch_url and ch_url not in seen_urls:
                            seen_urls.add(ch_url)
                            label = item.get("label", "")
                            title_extra = item.get("title", "")
                            title = f"{label} {title_extra}".strip() if title_extra else label
                            refs.append(ChapterRef(
                                index=len(refs),
                                title=title or f"Chapter {len(refs)+1}",
                                source_url=ch_url
                            ))
                    if refs:
                        return refs
                except Exception:
                    pass  # Fall back to HTML parsing if JSON index fetch fails

        # Method 2: Fallback to DOM HTML parsing
        selectors = _split_selectors(_SEL.get("chapter_list", ".wor-novel-chapters-list a, .wor-novel-chapter-item a"))
        if _SELECTOLAX_AVAILABLE:
            tree = HTMLParser(html)
            items = tree.css(".wor-novel-chapters-list a, .wor-novel-chapter-item a")
            for a in items:
                href = a.attributes.get("href", "")
                if not href or "/chapter" not in href or href in seen_urls:
                    continue
                seen_urls.add(href)
                if href.startswith("/"):
                    href = _BASE + href
                
                title = a.text(strip=True)
                refs.append(ChapterRef(index=len(refs), title=title or f"Chapter {len(refs)+1}", source_url=href))
        else:
            links = parse_links(html, selectors, base_url=_BASE)
            for text, href in links:
                if href and "/chapter" in href and href not in seen_urls:
                    seen_urls.add(href)
                    refs.append(ChapterRef(index=len(refs), title=text.strip() or f"Chapter {len(refs)+1}", source_url=href))

        # GalaxyNovels HTML lists chapters descending (newest to oldest); reverse if needed
        if refs and len(refs) > 1:
            first_title = refs[0].title
            last_title = refs[-1].title
            nums_first = re.findall(r"\d+", first_title)
            nums_last = re.findall(r"\d+", last_title)
            if nums_first and nums_last and int(nums_first[0]) > int(nums_last[0]):
                refs.reverse()
                for i, r in enumerate(refs):
                    r.index = i

        if not refs:
            html = await fetch_html_playwright(novel_url, ".wor-novel-chapters-list, [data-index-url]")
            tree = HTMLParser(html) if _SELECTOLAX_AVAILABLE else None
            if tree:
                container = tree.css_first(".wor-novel-chapters-wrap, [data-index-url]")
                index_url = container.attributes.get("data-index-url") if container else ""
                if index_url:
                    index_html = await fetch_html_playwright(index_url)
                    try:
                        data = json.loads(index_html)
                        raw_chapters = data.get("chapters", [])
                        for item in raw_chapters:
                            ch_url = item.get("url")
                            if ch_url and ch_url not in seen_urls:
                                label = item.get("label", "")
                                title_extra = item.get("title", "")
                                refs.append(ChapterRef(
                                    index=len(refs),
                                    title=f"{label} {title_extra}".strip() or f"Chapter {len(refs) + 1}",
                                    source_url=ch_url,
                                ))
                    except (TypeError, ValueError):
                        pass
                if not refs:
                    selectors = _split_selectors(_SEL.get("chapter_list", ".wor-novel-chapters-list a, .wor-novel-chapter-item a"))
                    links = parse_links(html, selectors, base_url=_BASE)
                    for title, href in links:
                        if "/chapter" in href and href not in seen_urls:
                            seen_urls.add(href)
                            refs.append(ChapterRef(index=len(refs), title=title or f"Chapter {len(refs) + 1}", source_url=href))
        return refs

    async def get_chapter_text(self, chapter_url: str) -> str:
        html = await _fetch_galaxy_html(chapter_url, ".wor-chapter-body, .chapter-content, article")
        selectors = _split_selectors(_SEL.get("chapter_text", ".wor-chapter-body, .chapter-content, #chapter-container, article"))
        text = parse_chapter_body(html, selectors)
        if not text:
            text = parse_text(html, [".wor-chapter-body", ".chapter-content", "article", "main"])
        if not text:
            html = await fetch_html_playwright(chapter_url, ".wor-chapter-body, .chapter-content, article")
            text = parse_chapter_body(html, selectors) or parse_text(html, [".wor-chapter-body", ".chapter-content", "article", "main"])
        return text or ""
