"""EasternWordSmith.com site adapter."""
from __future__ import annotations

import logging
import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from backend.adapters.base import ChapterRef, NovelMeta, SiteAdapter
from backend.adapters.fetcher import fetch_html_nodriver, fetch_html_playwright

logger = logging.getLogger("novelbridge.adapters.easternwordsmith")

_SITE_ID = "easternwordsmith"
_BASE_URL = "https://easternwordsmith.com"
_CHAPTER_RE = re.compile(r"^/chapter/(\d+)/?$")


async def _fetch_easternwordsmith_html(url: str, wait_selector: str) -> str:
    try:
        return await fetch_html_nodriver(url, wait_selector=wait_selector, timeout=45)
    except ImportError:
        logger.info("nodriver not installed; falling back to Playwright")
    except Exception as exc:
        logger.warning("nodriver fetch failed for %s: %s; falling back to Playwright", url, exc)
    return await fetch_html_playwright(url, wait_selector)


def _label_value(soup: BeautifulSoup, label: str) -> Optional[str]:
    for anchor in soup.select("a"):
        if anchor.get_text(" ", strip=True).rstrip(":").casefold() != label.casefold():
            continue
        row = anchor.find_parent("div", class_="row")
        value = row.select_one(".col-8") if row else None
        if value:
            paragraphs = value.find_all("p")
            text = "\n\n".join(p.get_text(" ", strip=True) for p in paragraphs)
            text = text or value.get_text(" ", strip=True)
            if text:
                return text
    return None


def _extract_chapter_refs(html: str, novel_url: str) -> List[ChapterRef]:
    soup = BeautifulSoup(html, "html.parser")
    refs_by_url: dict[str, tuple[int, str]] = {}
    for anchor in soup.select('a[href*="/chapter/"]'):
        if "early-access-link" in anchor.get("class", []):
            continue
        chapter_url = urljoin(novel_url, anchor.get("href", ""))
        parsed = urlparse(chapter_url)
        match = _CHAPTER_RE.match(parsed.path)
        if parsed.netloc.lower().removeprefix("www.") != "easternwordsmith.com" or not match:
            continue
        chapter_id = int(match.group(1))
        title = " ".join(anchor.get_text(" ", strip=True).split())
        refs_by_url[chapter_url] = (chapter_id, title or f"Chapter {chapter_id}")

    ordered = sorted(refs_by_url.items(), key=lambda item: item[1][0])
    return [
        ChapterRef(index=index, title=title, source_url=url)
        for index, (url, (_chapter_id, title)) in enumerate(ordered)
    ]


def _extract_chapter_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("section#CS, .wcontainer")
    if not container:
        return ""

    for element in container.select(
        "script, style, iframe, ins, .google-anno-sc, [data-google-vignette]"
    ):
        element.decompose()

    paragraphs = [
        re.sub(r"\s+([,.;:!?])", r"\1", p.get_text(" ", strip=True))
        for p in container.select("p")
    ]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    if paragraphs:
        return "\n\n".join(paragraphs)
    return container.get_text("\n", strip=True)


class EasternWordsmithAdapter(SiteAdapter):
    site_id = _SITE_ID
    source_language = "English"
    scraping_method = "nodriver (Cloudflare bypass)"

    def can_handle(self, url: str) -> bool:
        parsed = urlparse(url)
        return (
            parsed.netloc.lower().removeprefix("www.") == "easternwordsmith.com"
            and parsed.path.lower().startswith("/novel/")
        )

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        html = await _fetch_easternwordsmith_html(novel_url, "h4.center, title")
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if "just a moment" in title.casefold() or "performing security verification" in html.casefold():
            raise RuntimeError("Eastern WordSmith returned a Cloudflare security challenge.")
        if not title:
            heading = soup.select_one("h1, h2")
            title = heading.get_text(" ", strip=True) if heading else "Unknown Title"

        cover = soup.select_one('img[width="40%"]')
        cover_url = None
        if cover:
            cover_src = cover.get("data-src") or cover.get("data-original") or cover.get("src")
            if cover_src:
                cover_url = urljoin(_BASE_URL, cover_src)

        return NovelMeta(
            title=title,
            author=_label_value(soup, "Author"),
            cover_url=cover_url,
            description=_label_value(soup, "Description"),
            source_url=novel_url,
            source_site=_SITE_ID,
        )

    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        html = await _fetch_easternwordsmith_html(novel_url, "h4.center")
        return _extract_chapter_refs(html, novel_url)

    async def get_chapter_text(self, chapter_url: str) -> str:
        html = await _fetch_easternwordsmith_html(chapter_url, "#CS")
        return _extract_chapter_text(html)