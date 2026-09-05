"""LightNovelPub.me site adapter."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import List
from urllib.parse import urljoin, urlsplit, urlunsplit

try:
    from selectolax.parser import HTMLParser
    _SELECTOLAX_AVAILABLE = True
except ImportError:
    _SELECTOLAX_AVAILABLE = False
    from bs4 import BeautifulSoup

from backend.adapters.base import ChapterRef, NovelMeta, SiteAdapter
from backend.adapters.fetcher import fetch_html_playwright, parse_attr, parse_chapter_body, parse_links, parse_text

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


async def _goto_lightnovelpub(page, url: str) -> None:
    """Navigate through LightNovelPub's occasionally slow server reliably."""
    last_error = None
    for attempt in range(3):
        try:
            await page.goto(url, wait_until="commit", timeout=60_000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except Exception:
                pass
            return
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
    raise last_error


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
    scraping_method = "Playwright pagination"

    def can_handle(self, url: str) -> bool:
        lowered = url.lower()
        return "lightnovelpub.me" in lowered or "novellive.app" in lowered

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        html = await fetch_html_playwright(novel_url, ".m-desc .tit")
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
        """Read every 40-chapter page using LightNovelPub's numbered URLs."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            html = await fetch_html_playwright(novel_url, ".m-newest2 .ul-list5")
            return _chapter_refs(html, novel_url, set(), 0)

        chapter_selector = _SEL.get("chapter_list", ".m-newest2 .ul-list5 a.con")
        next_selector = _SEL.get("chapter_list_next", ".index-container-btn")
        refs: list[ChapterRef] = []
        seen_urls: set[str] = set()
        visited_pages: set[str] = set()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await _goto_lightnovelpub(page, novel_url)
                for _ in range(500):
                    page_url = page.url
                    if page_url in visited_pages:
                        break
                    visited_pages.add(page_url)
                    try:
                        await page.wait_for_selector(chapter_selector, timeout=15_000)
                    except Exception:
                        pass
                    html = await page.content()
                    refs.extend(_chapter_refs(html, page_url, seen_urls, len(refs)))

                    next_link = page.locator(next_selector).filter(
                        has_text=re.compile(r"^\s*Next\s*$", re.IGNORECASE)
                    ).last
                    if not await next_link.count():
                        break
                    next_url = await next_link.get_attribute("href")
                    if not next_url:
                        break
                    next_url = urljoin(page_url, next_url)
                    if next_url in visited_pages:
                        break
                    await _goto_lightnovelpub(page, next_url)
            finally:
                await browser.close()

        refs.sort(key=lambda ref: (int(re.search(r"chapter-(\d+)", ref.source_url, re.IGNORECASE).group(1)) if re.search(r"chapter-(\d+)", ref.source_url, re.IGNORECASE) else ref.index))
        for index, ref in enumerate(refs):
            ref.index = index
        return refs

    async def get_chapter_text(self, chapter_url: str) -> str:
        html = await fetch_html_playwright(chapter_url, ".read-content, .chapter-content, article")
        selectors = _split_selectors(_SEL.get("chapter_text", ".read-content, .chapter-content, #chapter-container, article"))
        return parse_chapter_body(html, selectors) or parse_text(html, selectors) or ""
