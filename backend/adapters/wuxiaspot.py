"""WuxiaSpot.com site adapter."""
from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse

try:
    from selectolax.parser import HTMLParser

    _SELECTOLAX_AVAILABLE = True
except ImportError:
    _SELECTOLAX_AVAILABLE = False

from backend.adapters.base import ChapterRef, NovelMeta, SiteAdapter
from backend.adapters.fetcher import (
    fetch_html,
    parse_attr,
    parse_chapter_body,
    parse_links,
    parse_text,
)

_SITE_ID = "wuxiaspot"
_BASE = "https://www.wuxiaspot.com"
_NOVEL_RE = re.compile(r"/novel/([^/?#]+?)(?:\.html)?/?$", re.IGNORECASE)
_CHAPTER_RE = re.compile(r"/novel/([^/?#]+?)_(\d+)\.html(?:[/?#]|$)", re.IGNORECASE)


def _chapter_number(url: str) -> Optional[int]:
    match = _CHAPTER_RE.search(url)
    return int(match.group(2)) if match else None


class WuxiaSpotAdapter(SiteAdapter):
    site_id = _SITE_ID
    source_language = "English"
    scraping_method = "curl"

    def can_handle(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc.lower().removeprefix("www.") == "wuxiaspot.com" and "/novel/" in parsed.path.lower()

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        html = await fetch_html(novel_url, _SITE_ID)
        title = parse_text(html, ["h1.novel-title", "h1[itemprop=name]", "h1"]) or "Unknown Title"
        cover = parse_attr(html, [".fixed-img img", ".novel-cover img", "img[itemprop=image]"], "data-src")
        cover = cover or parse_attr(html, [".fixed-img img", ".novel-cover img", "img[itemprop=image]"], "src")
        return NovelMeta(
            title=title,
            author=parse_text(html, [".author a", ".author", "[rel=author]"]),
            cover_url=urljoin(_BASE, cover) if cover else None,
            description=parse_text(html, [".summary .content", ".summary", ".description"]),
            source_url=novel_url,
            source_site=_SITE_ID,
        )

    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        html = await fetch_html(novel_url, _SITE_ID)
        refs = self._extract_chapters(html, novel_url)
        novel_id = self._novel_id(novel_url)
        if not novel_id:
            return self._ordered(refs)

        page_urls = self._pagination_urls(html, novel_id)
        for page_url in page_urls:
            page_html = await fetch_html(page_url, _SITE_ID)
            refs.extend(self._extract_chapters(page_html, page_url))

        by_url: Dict[str, ChapterRef] = {ref.source_url: ref for ref in refs}
        return self._ordered(list(by_url.values()))

    def _extract_chapters(self, html: str, page_url: str) -> List[ChapterRef]:
        if _SELECTOLAX_AVAILABLE:
            return self._extract_chapters_selectolax(html, page_url)

        links = parse_links(html, ["#chpagedlist .chapter-list a", ".chapter-list a"], base_url=_BASE)
        refs = []
        for title, href in links:
            full_url = urljoin(page_url, href)
            if _CHAPTER_RE.search(urlparse(full_url).path):
                refs.append(ChapterRef(index=len(refs), title=title.strip() or "Chapter", source_url=full_url))
        return refs

    def _extract_chapters_selectolax(self, html: str, page_url: str) -> List[ChapterRef]:
        tree = HTMLParser(html)
        refs = []
        anchors = tree.css("#chpagedlist .chapter-list a[href]")
        if not anchors:
            anchors = tree.css(".chapter-list a[href]")
        seen_urls = set()
        for anchor in anchors:
            full_url = urljoin(page_url, anchor.attributes.get("href", ""))
            if not _CHAPTER_RE.search(urlparse(full_url).path):
                continue
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            title = anchor.attributes.get("title", "").strip() or anchor.text(strip=True)
            volume = self._volume_label(anchor)
            if volume and not title.lower().startswith(volume.lower()):
                title = f"{volume} - {title}"
            refs.append(ChapterRef(index=len(refs), title=title or "Chapter", source_url=full_url))
        return refs

    @staticmethod
    def _volume_label(anchor) -> Optional[str]:
        """Find a nearby volume heading for chapter cards that use volumes."""
        current = anchor.parent
        for _ in range(5):
            if current is None:
                break
            for heading in current.css("h2, h3, h4, .volume-title, .volume-name, [class*='volume']"):
                text = heading.text(strip=True)
                if re.search(r"\bvolume\b", text, re.IGNORECASE):
                    return text
            current = current.parent
        return None

    def _pagination_urls(self, html: str, novel_id: str) -> List[str]:
        urls: Dict[int, str] = {}
        if _SELECTOLAX_AVAILABLE:
            tree = HTMLParser(html)
            anchors = tree.css(".pagination-container a[href]")
            hrefs = [anchor.attributes.get("href", "") for anchor in anchors]
        else:
            hrefs = [href for _text, href in parse_links(html, [".pagination-container a"], base_url=_BASE)]
        for href in hrefs:
            parsed = urlparse(urljoin(_BASE, href))
            if not parsed.path.endswith(("/fy.php", "/fy1.php")):
                continue
            query = parse_qs(parsed.query)
            if query.get("wjm", [""])[0] != novel_id:
                continue
            try:
                page = int(query["page"][0])
            except (KeyError, ValueError):
                continue
            urls[page] = urljoin(_BASE, href)
        return [urls[page] for page in sorted(urls)]

    @staticmethod
    def _novel_id(url: str) -> Optional[str]:
        match = _NOVEL_RE.search(urlparse(url).path)
        return match.group(1) if match else None

    @staticmethod
    def _ordered(refs: List[ChapterRef]) -> List[ChapterRef]:
        refs.sort(key=lambda ref: (_chapter_number(ref.source_url) is None, _chapter_number(ref.source_url) or 0))
        return [ChapterRef(index=index, title=ref.title, source_url=ref.source_url) for index, ref in enumerate(refs)]

    async def get_chapter_text(self, chapter_url: str) -> str:
        html = await fetch_html(chapter_url, _SITE_ID)
        return parse_chapter_body(html, [".chapter-content", ".content-wrap article"]) or parse_text(
            html, [".chapter-content", ".content-wrap", "article", "main"]
        ) or ""