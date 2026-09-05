"""LightNovelPub.me site adapter."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import List
from urllib.parse import urljoin, urlsplit, urlunsplit

logger = logging.getLogger("novelbridge.adapters.lightnovelpub")

try:
    from selectolax.parser import HTMLParser
    _SELECTOLAX_AVAILABLE = True
except ImportError:
    _SELECTOLAX_AVAILABLE = False

from bs4 import BeautifulSoup

from backend.adapters.base import ChapterRef, NovelMeta, SiteAdapter
from backend.adapters.fetcher import (
    fetch_html_nodriver,
    fetch_html_playwright,
    parse_attr,
    parse_chapter_body,
    parse_links,
    parse_text,
)

_SITE_ID = "lightnovelpub"
_BASE = "https://lightnovelpub.me"

_cfg_path = Path("config/sites.json")
_SEL: dict = {}
if _cfg_path.exists():
    _SEL = json.loads(_cfg_path.read_text(encoding="utf-8")).get(_SITE_ID, {}).get("selectors", {})


def _split_selectors(value: str) -> list[str]:
    return [selector.strip() for selector in value.split(",") if selector.strip()]


def _chapter_page_url(novel_url: str, page_number: int) -> str:
    """Build LightNovelPub's numbered chapter-list URL."""
    parsed = urlsplit(novel_url)
    path = re.sub(r"/\d+$", "", parsed.path.rstrip("/"))
    netloc = parsed.netloc
    if page_number > 1 and netloc.lower() in {"lightnovelpub.me", "www.lightnovelpub.me"}:
        netloc = "novellive.app"
    if page_number > 1:
        path = f"{path}/{page_number}"
    return urlunsplit((parsed.scheme or "https", netloc, path, "", ""))


async def _fetch_lightnovelpub_html(url: str, wait_selector: str = "") -> str:
    """Fetch a LightNovelPub page, bypassing Cloudflare.

    Strategy:
    1. nodriver (undetected Chrome subprocess)
    2. Fall back to Playwright if nodriver is not available or fails
    """
    try:
        return await fetch_html_nodriver(url, wait_selector=wait_selector, timeout=45)
    except ImportError:
        logger.info("nodriver not installed; falling back to Playwright")
    except Exception as e:
        logger.warning("nodriver fetch failed for %s: %s; falling back to Playwright", url, e)
    return await fetch_html_playwright(url, wait_selector)


def _chapter_refs(html: str, page_url: str, seen_urls: set[str], start_index: int) -> list[ChapterRef]:
    selectors = _split_selectors(_SEL.get("chapter_list", ".m-newest2 .ul-list5 a.con"))
    refs: list[ChapterRef] = []

    if _SELECTOLAX_AVAILABLE:
        tree = HTMLParser(html)
        nodes = tree.css(", ".join(selectors))
        for node in nodes:
            href = node.attributes.get("href", "")
            if not href:
                continue
            href = urljoin(page_url, href)
            if "/chapter-" not in href or href in seen_urls:
                continue
            seen_urls.add(href)
            title = node.attributes.get("title", "").strip() or node.text(strip=True)
            refs.append(ChapterRef(
                index=start_index + len(refs),
                title=title or f"Chapter {start_index + len(refs) + 1}",
                source_url=href,
            ))
        return refs

    for title, href in parse_links(html, selectors, base_url=page_url):
        if "/chapter-" not in href or href in seen_urls:
            continue
        seen_urls.add(href)
        refs.append(ChapterRef(
            index=start_index + len(refs),
            title=title.strip() or f"Chapter {start_index + len(refs) + 1}",
            source_url=href,
        ))
    return refs


class LightNovelPubAdapter(SiteAdapter):
    site_id = _SITE_ID
    source_language = "English"
    scraping_method = "nodriver (Cloudflare bypass)"

    def can_handle(self, url: str) -> bool:
        lowered = url.lower()
        return "lightnovelpub.me" in lowered or "novellive.app" in lowered

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        html = await _fetch_lightnovelpub_html(novel_url, ".m-desc .tit, h1")
        title = parse_text(html, _split_selectors(_SEL.get("novel_title", ".m-desc .tit, h1"))) or "Unknown Title"
        author = parse_text(html, _split_selectors(_SEL.get("novel_author", ".m-book1 .item [title=Author] + .right a, .m-book1 .item .right a")))
        cover_url = parse_attr(html, _split_selectors(_SEL.get("novel_cover", ".m-book1 .pic img, .m-book1 img")), "src")
        description = parse_text(html, _split_selectors(_SEL.get("novel_description", ".m-desc .inner, .abstract + .txt")))
        return NovelMeta(
            title=title,
            author=author,
            cover_url=urljoin(_BASE, cover_url) if cover_url else None,
            description=description,
            source_url=novel_url,
            source_site=_SITE_ID,
        )

    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        """Read all chapter pages for the novel across multiple pagination pages."""
        chapter_selector = _SEL.get("chapter_list", ".m-newest2 .ul-list5 a.con")
        wait_sel = chapter_selector.split(",")[0].strip()

        refs: list[ChapterRef] = []
        seen_urls: set[str] = set()
        visited_urls: set[str] = set()

        current_url: str | None = novel_url
        page_num = 1
        max_pages = 500

        while current_url and current_url not in visited_urls and page_num <= max_pages:
            visited_urls.add(current_url)
            html = await _fetch_lightnovelpub_html(current_url, wait_selector=wait_sel)
            page_refs = _chapter_refs(html, current_url, seen_urls, len(refs))
            if not page_refs:
                break
            refs.extend(page_refs)

            soup = BeautifulSoup(html, "html.parser")

            # Determine max_pages from 'Last' button if present on page 1
            if page_num == 1:
                for btn in soup.select(".index-container-btn, a[rel=last], .pagination a"):
                    if btn.get_text(strip=True).lower() == "last":
                        m = re.search(r"/(\d+)(?:[#?]|$)", btn.get("href", ""))
                        if m:
                            max_pages = int(m.group(1))
                        break

            # Find next page
            next_a = None
            for btn in soup.select(".index-container-btn, a[rel=next], .pagination a"):
                if btn.get_text(strip=True).lower() == "next":
                    next_a = btn
                    break

            if next_a and next_a.get("href"):
                next_url = urljoin(current_url, next_a["href"])
                page_num += 1
                if next_url in visited_urls:
                    break
                current_url = next_url
            elif page_num < max_pages:
                page_num += 1
                current_url = _chapter_page_url(novel_url, page_num)
            else:
                break

        refs.sort(key=lambda ref: (
            int(re.search(r"chapter-(\d+)", ref.source_url, re.IGNORECASE).group(1))
            if re.search(r"chapter-(\d+)", ref.source_url, re.IGNORECASE)
            else ref.index
        ))
        for index, ref in enumerate(refs):
            ref.index = index
        return refs

    async def get_chapter_text(self, chapter_url: str) -> str:
        html = await _fetch_lightnovelpub_html(
            chapter_url,
            wait_selector=".txt, .read-content, .chapter-content, #chapter-container, article",
        )
        selectors = _split_selectors(
            _SEL.get("chapter_text", ".read-content, .txt, .chapter-content, #chapter-container, article")
        )
        return parse_chapter_body(html, selectors) or parse_text(html, selectors) or ""
