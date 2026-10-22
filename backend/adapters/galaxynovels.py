"""
NovelBridge — GalaxyNovels.com (Arabic) site adapter.
"""
from __future__ import annotations

import json
import logging
import re
from typing import List
from urllib.parse import urljoin

try:
    from selectolax.parser import HTMLParser
    _SELECTOLAX_AVAILABLE = True
except ImportError:
    _SELECTOLAX_AVAILABLE = False

from bs4 import BeautifulSoup

from backend.adapters.base import ChapterRef, NovelMeta, SiteAdapter, host_matches
from backend.adapters.fetcher import (
    fetch_html, fetch_html_playwright, parse_attr, parse_chapter_body,
    parse_links, parse_text,
)
from backend.config import resolve_path

_SITE_ID = "galaxynovels"
_BASE    = "https://galaxynovels.com"

logger = logging.getLogger("novelbridge.adapters.galaxynovels")

# Load selectors from config
_cfg_path = resolve_path("config/sites.json")
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


async def _fetch_galaxy_html_with_all_chapters(url: str) -> str:
    """Render the chapter list and click its load-more control until exhausted."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is required to expand the Galaxy chapter list") from exc

    chapter_selector = ".wor-novel-chapters-list a, .wor-novel-chapter-item a"
    more_pattern = re.compile(r"عرض المزيد|المزيد|load\s+more|more", re.IGNORECASE)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            try:
                await page.wait_for_selector(chapter_selector, timeout=15_000)
            except Exception:
                pass

            for _ in range(1000):
                before = await page.locator(chapter_selector).count()
                more = page.locator(
                    ".wor-novel-chapters-wrap button, "
                    ".wor-novel-chapters-wrap a, "
                    ".wor-novel-chapters-list button, "
                    ".wor-novel-chapters-list a"
                ).filter(has_text=more_pattern)
                if not await more.count() or not await more.last.is_visible():
                    break

                await more.last.click()
                try:
                    await page.wait_for_function(
                        "([selector, count]) => document.querySelectorAll(selector).length > count",
                        [chapter_selector, before],
                        timeout=15_000,
                    )
                except Exception:
                    break

            return await page.content()
        finally:
            await browser.close()


class GalaxyNovelsAdapter(SiteAdapter):
    site_id = _SITE_ID
    is_native_arabic = True

    def can_handle(self, url: str) -> bool:
        return host_matches(url, ("galaxynovels.com",))

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        html = await _fetch_galaxy_html(novel_url, "h1")

        title = parse_text(html, _split_selectors(_SEL.get("novel_title", "h1, .wor-single-title"))) or "Unknown Title"
        author = parse_text(html, _split_selectors(_SEL.get("novel_author", ".author, .wor-author a")))
        
        # If author not found in standard selectors, search for author metadata in meta tags or descriptions
        # NOTE: capture up to the structural delimiters (comma/newline) or the
        # " على" separator — never exclude the Arabic letters ع/ل/ي as the old
        # "[^على]" class did, which truncated any author containing them.
        if not author:
            if _SELECTOLAX_AVAILABLE:
                tree = HTMLParser(html)
                desc_meta = tree.css_first("meta[name=description]")
                if desc_meta:
                    m_text = desc_meta.attributes.get("content", "")
                    match = re.search(r"المؤلف\s+([^،\n]+?)\s+على", m_text)
                    if match:
                        author = match.group(1).strip()
            else:
                soup = BeautifulSoup(html, "html.parser")
                desc_meta = soup.select_one("meta[name=description]")
                if desc_meta:
                    m_text = desc_meta.get("content", "")
                    match = re.search(r"المؤلف\s+([^،\n]+?)\s+على", m_text)
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
        html = await _fetch_galaxy_html(
            novel_url, ".wor-novel-chapters-list, [data-manifest-url], [data-index-url]"
        )
        
        refs: list[ChapterRef] = []
        seen_urls = set()

        # Method 1: Read the full chapter manifest before falling back to the paginated DOM.
        index_url = ""
        if _SELECTOLAX_AVAILABLE:
            tree = HTMLParser(html)
            container = tree.css_first(
                ".wor-novel-chapters-wrap, [data-manifest-url], [data-index-url]"
            )
            if container:
                index_url = (
                    container.attributes.get("data-manifest-url")
                    or container.attributes.get("data-index-url")
                    or ""
                )
        else:
            soup = BeautifulSoup(html, "html.parser")
            container = soup.select_one(
                ".wor-novel-chapters-wrap, [data-manifest-url], [data-index-url]"
            )
            if container:
                index_url = (
                    container.get("data-manifest-url")
                    or container.get("data-index-url")
                    or ""
                )

        if index_url:
            try:
                json_str = await fetch_html(urljoin(novel_url, index_url), _SITE_ID)
                data = json.loads(json_str)
                raw_chapters = data.get("chapters", [])
                for item in raw_chapters:
                    ch_url = item.get("url")
                    if ch_url:
                        ch_url = urljoin(novel_url, ch_url)
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

        # The initial HTML contains only 30 chapters. Expand the client-side
        # list so older chapters are available to the pipeline as well.
        if refs and len(refs) <= 30:
            try:
                expanded_html = await _fetch_galaxy_html_with_all_chapters(novel_url)
                if expanded_html != html:
                    html = expanded_html
                    refs = []
                    seen_urls = set()
                    if _SELECTOLAX_AVAILABLE:
                        tree = HTMLParser(html)
                        items = tree.css(".wor-novel-chapters-list a, .wor-novel-chapter-item a")
                        for a in items:
                            href = a.attributes.get("href", "")
                            if not href or "/chapter" not in href:
                                continue
                            href = urljoin(novel_url, href)
                            if href in seen_urls:
                                continue
                            seen_urls.add(href)
                            refs.append(ChapterRef(
                                index=len(refs),
                                title=a.text(strip=True) or f"Chapter {len(refs)+1}",
                                source_url=href,
                            ))
                    else:
                        for text, href in parse_links(html, selectors, base_url=_BASE):
                            if "/chapter" in href and href not in seen_urls:
                                seen_urls.add(href)
                                refs.append(ChapterRef(
                                    index=len(refs),
                                    title=text.strip() or f"Chapter {len(refs)+1}",
                                    source_url=href,
                                ))
            except Exception:
                pass

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
            html = await fetch_html_playwright(
                novel_url, ".wor-novel-chapters-list, [data-manifest-url], [data-index-url]"
            )
            index_url = ""
            if _SELECTOLAX_AVAILABLE:
                tree = HTMLParser(html)
                container = tree.css_first(
                    ".wor-novel-chapters-wrap, [data-manifest-url], [data-index-url]"
                )
                if container:
                    index_url = (
                        container.attributes.get("data-manifest-url")
                        or container.attributes.get("data-index-url")
                        or ""
                    )
            else:
                soup = BeautifulSoup(html, "html.parser")
                container = soup.select_one(
                    ".wor-novel-chapters-wrap, [data-manifest-url], [data-index-url]"
                )
                if container:
                    index_url = (
                        container.get("data-manifest-url")
                        or container.get("data-index-url")
                        or ""
                    )

            if index_url:
                try:
                    index_html = await fetch_html_playwright(urljoin(novel_url, index_url))
                    data = json.loads(index_html)
                    raw_chapters = data.get("chapters", [])
                    for item in raw_chapters:
                        ch_url = item.get("url")
                        if ch_url:
                            ch_url = urljoin(novel_url, ch_url)
                        if ch_url and ch_url not in seen_urls:
                            seen_urls.add(ch_url)
                            label = item.get("label", "")
                            title_extra = item.get("title", "")
                            refs.append(ChapterRef(
                                index=len(refs),
                                title=f"{label} {title_extra}".strip() or f"Chapter {len(refs) + 1}",
                                source_url=ch_url,
                            ))
                except Exception:
                    logger.debug(
                        "Could not load GalaxyNovels manifest index %s",
                        index_url,
                        exc_info=True,
                    )

            if not refs:
                selectors = _split_selectors(_SEL.get("chapter_list", ".wor-novel-chapters-list a, .wor-novel-chapter-item a"))
                if _SELECTOLAX_AVAILABLE:
                    tree = HTMLParser(html)
                    items = tree.css(".wor-novel-chapters-list a, .wor-novel-chapter-item a")
                    for a in items:
                        href = a.attributes.get("href", "")
                        if not href or "/chapter" not in href:
                            continue
                        href = urljoin(novel_url, href)
                        if href in seen_urls:
                            continue
                        seen_urls.add(href)
                        refs.append(ChapterRef(
                            index=len(refs),
                            title=a.text(strip=True) or f"Chapter {len(refs) + 1}",
                            source_url=href,
                        ))
                else:
                    for title, href in parse_links(html, selectors, base_url=_BASE):
                        if "/chapter" in href and href not in seen_urls:
                            seen_urls.add(href)
                            refs.append(ChapterRef(
                                index=len(refs),
                                title=title or f"Chapter {len(refs) + 1}",
                                source_url=href,
                            ))
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
