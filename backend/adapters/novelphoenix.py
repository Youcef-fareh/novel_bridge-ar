"""
NovelBridge — NovelPhoenix.com site adapter.
"""
from __future__ import annotations

import json
import re
from typing import List

try:
    from selectolax.parser import HTMLParser
    _SELECTOLAX_AVAILABLE = True
except ImportError:
    _SELECTOLAX_AVAILABLE = False

from backend.adapters.base import ChapterRef, NovelMeta, SiteAdapter, host_matches
from backend.adapters.fetcher import (
    fetch_html, parse_chapter_body, parse_cover_url, parse_links, parse_text
)
from backend.config import resolve_path

_SITE_ID = "novelphoenix"
_BASE    = "https://novelphoenix.com"

# Load selectors from config
_cfg_path = resolve_path("config/sites.json")
_SEL: dict = {}
if _cfg_path.exists():
    _SEL = json.loads(_cfg_path.read_text(encoding="utf-8")).get(_SITE_ID, {}).get("selectors", {})


def _split_selectors(value: str) -> list[str]:
    """Split a comma-separated CSS selector string."""
    return [s.strip() for s in value.split(",") if s.strip()]


# Pagination/TOC/list links whose path merely CONTAINS "/chapter" (e.g.
# "/novel/x/chapters?page=2" — the endswith("/chapters") check misses it
# because of the query string) used to be collected as chapters.
_NON_CHAPTER_HREF_RE = re.compile(
    r"[?&]page|/chapters(?:[/?#]|$)|chapter-list|/toc(?:[/?#]|$)|/table-of-contents",
    re.IGNORECASE,
)


def _is_chapter_href(href: str) -> bool:
    """True only for canonical chapter URLs, excluding pagination/TOC links."""
    if not href or "/chapter" not in href:
        return False
    return _NON_CHAPTER_HREF_RE.search(href) is None


class NovelPhoenixAdapter(SiteAdapter):
    site_id = _SITE_ID

    def can_handle(self, url: str) -> bool:
        return host_matches(url, ("novelphoenix.com", "novelphoenix.net"))

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        html = await fetch_html(novel_url, _SITE_ID)

        title = parse_text(html, _split_selectors(_SEL.get("novel_title", "h1"))) or "Unknown Title"
        author = parse_text(html, _split_selectors(_SEL.get("novel_author", ".author")))
        if author and author.lower().startswith("author:"):
            author = author[7:].strip()

        cover_url = parse_cover_url(
            html,
            _split_selectors(_SEL.get("novel_cover", ".cover img, .novel-info img")),
            novel_url,
        )

        description = parse_text(html, _split_selectors(_SEL.get("novel_description", ".summary, .description")))
        if description and description.lower().startswith("summary"):
            description = description[7:].strip()

        return NovelMeta(
            title=title,
            author=author,
            cover_url=cover_url,
            description=description,
            source_url=novel_url,
            source_site=_SITE_ID,
        )

    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        # Chapter lists in NovelPhoenix are usually at /chapters endpoint or on the novel page
        clean_url = novel_url.rstrip("/")
        if not clean_url.endswith("/chapters"):
            base_chapters_url = f"{clean_url}/chapters"
        else:
            base_chapters_url = clean_url

        refs: list[ChapterRef] = []
        seen_urls = set()
        page = 1
        max_pages = 500  # Safety limit

        while page <= max_pages:
            page_url = f"{base_chapters_url}?page={page}" if page > 1 else base_chapters_url
            try:
                html = await fetch_html(page_url, _SITE_ID)
            except Exception:
                if page == 1:
                    # Fallback to main page if /chapters fails
                    try:
                        html = await fetch_html(novel_url, _SITE_ID)
                    except Exception:
                        break
                else:
                    break

            selectors = _split_selectors(_SEL.get("chapter_list", ".chapter-list li a, .chapter-list a, .chapter-item a"))
            new_on_page = 0

            if _SELECTOLAX_AVAILABLE:
                tree = HTMLParser(html)
                items = tree.css(".chapter-list li, .chapter-list a, .chapter-item a")
                for it in items:
                    a_tag = it if it.tag == "a" else it.css_first("a")
                    if not a_tag:
                        continue
                    href = a_tag.attributes.get("href", "")
                    if not _is_chapter_href(href) or href in seen_urls:
                        continue
                    seen_urls.add(href)
                    if href.startswith("/"):
                        href = _BASE + href

                    title_node = it.css_first(".chapter-title, strong.chapter-title")
                    if title_node and title_node.text(strip=True):
                        title = title_node.text(strip=True)
                    elif a_tag.attributes.get("title"):
                        title = a_tag.attributes.get("title", "").strip()
                    else:
                        raw = a_tag.text(strip=True)
                        raw = re.sub(r"^\d+\s*(?=Chapter\b)", "", raw, flags=re.IGNORECASE)
                        raw = re.sub(r"\s*\d+\s*(?:year|month|week|day|hour|minute)s?\s*ago\s*$", "", raw, flags=re.IGNORECASE)
                        title = raw.strip()

                    refs.append(ChapterRef(index=len(refs), title=title or f"Chapter {len(refs)+1}", source_url=href))
                    new_on_page += 1
            else:
                links = parse_links(html, selectors, base_url=_BASE)
                for text, href in links:
                    if _is_chapter_href(href) and href not in seen_urls:
                        seen_urls.add(href)
                        refs.append(ChapterRef(index=len(refs), title=text.strip() or f"Chapter {len(refs)+1}", source_url=href))
                        new_on_page += 1

            if new_on_page == 0:
                # No more new chapters found on this page, stop paginating
                break
            page += 1

        return refs

    async def get_chapter_text(self, chapter_url: str) -> str:
        html = await fetch_html(chapter_url, _SITE_ID)
        selectors = _split_selectors(_SEL.get("chapter_text", "#chapter-container, .chapter-content"))
        text = parse_chapter_body(html, selectors)
        if not text:
            text = parse_text(html, ["#chapter-container", ".chapter-content", "article", "main"])
        return text or ""
