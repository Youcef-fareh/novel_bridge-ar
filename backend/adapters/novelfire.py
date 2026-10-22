"""
NovelBridge — NovelFire.net site adapter.
"""
from __future__ import annotations

import json
import re
from typing import List
from urllib.parse import quote_plus, urljoin, urlparse

try:
    from selectolax.parser import HTMLParser
    _SELECTOLAX_AVAILABLE = True
except ImportError:
    _SELECTOLAX_AVAILABLE = False

from backend.adapters.base import ChapterRef, NovelMeta, SearchResult, host_matches
from backend.adapters.fetcher import (
    fetch_html, parse_links
)
from backend.config import resolve_path

_SITE_ID = "novelfire"
_BASE    = "https://novelfire.net"

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


from backend.adapters.soup_template import SoupAdapter


class NovelFireAdapter(SoupAdapter):
    site_id = _SITE_ID
    base_url = _BASE
    version = "2"
    tier = "legacy"
    can_search = True

    novel_title_selectors = ["h1.novel-title", ".book-name h1", ".novel-info h1", "h1"]
    novel_author_selectors = [".author", ".author-content a", ".novel-author .author-name"]
    novel_cover_selectors = [".cover img", ".book-img img", ".novel-cover img", ".novel-info img"]
    novel_description_selectors = [".content", ".summary", ".summary__content", ".novel-description"]
    chapter_list_selectors = [".chapter-list li a", ".chapter-list a", ".chapter-item a", ".wp-manga-chapter a"]
    chapter_body_selectors = ["#chapter-container", ".chapter-content", ".text-left"]

    def can_handle(self, url: str) -> bool:
        return host_matches(url, ("novelfire.net", "novelfire.com"))

    async def search(self, query: str, limit: int = 20) -> list[SearchResult]:
        from bs4 import BeautifulSoup

        search_url = f"{_BASE}/search?keyword={quote_plus(query)}"
        html = await fetch_html(search_url, _SITE_ID)
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select(".novel-item, .novel-card, .book-item, .list-novel .item")
        if not cards:
            cards = soup.select('a[href*="/book/"], a[href*="/novel/"]')

        results: list[SearchResult] = []
        seen: set[str] = set()
        for card in cards:
            anchor = card if card.name == "a" else card.select_one("a[href]")
            if not anchor:
                continue
            url = urljoin(_BASE, str(anchor.get("href", "")).strip())
            host = (urlparse(url).hostname or "").lower()
            if (
                urlparse(url).scheme not in {"http", "https"}
                or not (host == "novelfire.net" or host.endswith(".novelfire.net")
                        or host == "novelfire.com" or host.endswith(".novelfire.com"))
                or url in seen
            ):
                continue
            image = card.select_one("img")
            title = (
                anchor.get_text(" ", strip=True)
                or (str(image.get("alt", "")).strip() if image else "")
                or str(anchor.get("title", "")).strip()
            )
            if not title:
                continue
            cover = None
            if image:
                for attribute in ("data-src", "data-original", "src"):
                    value = image.get(attribute)
                    if value:
                        cover = urljoin(url, str(value))
                        break
            seen.add(url)
            results.append(SearchResult(
                title=title,
                url=url,
                source_site=self.site_id,
                cover_url=cover,
            ))
            if len(results) >= max(1, min(limit, 50)):
                break
        return results

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        html = await fetch_html(novel_url, self.site_id)
        meta = self.parse_novel_metadata(html, novel_url)
        if meta.author and meta.author.lower().startswith("author:"):
            meta.author = meta.author[7:].strip()
        if meta.description and meta.description.lower().startswith("summary"):
            meta.description = meta.description[7:].strip()
        return meta

    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        # If /chapters URL exists, check it; otherwise check novel page
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
                    try:
                        html = await fetch_html(novel_url, _SITE_ID)
                    except Exception:
                        break
                else:
                    break

            selectors = _split_selectors(_SEL.get("chapter_list", ".chapter-list li a, .chapter-list a, .chapter-item a, .wp-manga-chapter a"))
            new_on_page = 0

            if _SELECTOLAX_AVAILABLE:
                tree = HTMLParser(html)
                items = tree.css(".chapter-list li, .chapter-list a, .chapter-item a, .wp-manga-chapter a")
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
                # No more chapters on this page, stop paginating
                break
            page += 1

        return refs
