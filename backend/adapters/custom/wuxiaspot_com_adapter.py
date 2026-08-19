"""Generated adapter template for www.wuxiaspot.com."""
from __future__ import annotations

from typing import List
from urllib.parse import urljoin

from backend.adapters.base import ChapterRef, NovelMeta, SiteAdapter
from backend.adapters.fetcher import (
    fetch_html,
    fetch_html_playwright,
    parse_chapter_body,
    parse_links,
    parse_text,
)

SITE_ID = "wuxiaspot_com"
BASE_URL = "https://www.wuxiaspot.com"


class WuxiaspotComAdapter(SiteAdapter):
    site_id = SITE_ID

    def can_handle(self, url: str) -> bool:
        return "www.wuxiaspot.com" in url.lower()

    async def _fetch(self, url: str, wait_for: str = "") -> str:
        return await fetch_html(url, SITE_ID)

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        html = await self._fetch(novel_url, "h1")
        title = parse_text(html, ["h1", "title"]) or "Unknown Title"
        return NovelMeta(
            title=title,
            author=parse_text(html, [".author", "[rel=author]"]),
            description=parse_text(html, [".description", ".summary", "article"]),
            source_url=novel_url,
            source_site=SITE_ID,
        )

    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        html = await self._fetch(novel_url, ".chapter-list, .chapters, article")
        refs = []
        for index, (title, href) in enumerate(parse_links(
            html,
            [".chapter-list a", ".chapters a", "a[href*=chapter]"],
            base_url=BASE_URL,
        )):
            refs.append(ChapterRef(
                index=index,
                title=title or f"Chapter {index + 1}",
                source_url=urljoin(BASE_URL, href),
            ))
        return refs

    async def get_chapter_text(self, chapter_url: str) -> str:
        html = await self._fetch(chapter_url, ".chapter-content, article, main")
        return parse_chapter_body(
            html,
            [".chapter-content", ".chapter-body", "article", "main"],
        ) or parse_text(html, [".chapter-content", "article", "main"]) or ""
