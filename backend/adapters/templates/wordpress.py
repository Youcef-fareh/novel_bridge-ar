"""
NovelBridge — Madara / WordPress-manga adapter template.

Ported selectors and ajax chapter-list logic from
lncrawl/templates/wordpress.py (MIT).

Two-step chapter-list strategy (mirrors lncrawl exactly):
  1. POST {novel_url}/ajax/chapters/
  2. Fallback: POST admin-ajax.php?action=manga_get_chapters with the
     manga ID from ``#manga-chapters-holder[data-id]``.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from bs4 import BeautifulSoup

from backend.adapters.base import ChapterRef
from backend.adapters.fetcher import fetch_html, post_html
from backend.adapters.health import health
from backend.adapters.soup_template import SoupAdapter

logger = logging.getLogger("novelbridge.templates.wordpress")


class WordpressAdapter(SoupAdapter):
    """Shared Madara / WordPress-manga selectors + ajax chapter loading.

    Minimal subclass example::

        class RanovelAdapter(WordpressAdapter):
            site_id = "ranovel"
            base_url = "https://ranovel.com"

            chapter_list_selectors = ["li.wp-manga-chapter a"]
            chapter_body_selectors = [".reading-content", ".text-left"]

            def can_handle(self, url: str) -> bool:
                return "ranovel.com" in url.lower()
    """

    # Madara theme selectors
    novel_title_selectors = [".post-title h1"]
    novel_cover_selectors = [".summary_image a img"]
    novel_author_selectors = ['.author-content a[href*="manga-author"]']
    novel_tags_selectors = ['.genres-content a[rel="tag"]']
    novel_description_selectors = [".description-summary"]

    chapter_list_selectors = ["li.wp-manga-chapter a"]
    chapter_body_selectors = ["div.reading-content"]
    pagination_next_selectors = [".pagination a.next", ".nav-links .nav-next a", 'a[rel="next"]']

    # If True, the honeypot-clean hook runs before body extraction
    madara_body_from_paragraphs: bool = False

    def select_chapter_refs(self, html: str, base_url: str) -> List[ChapterRef]:  # type: ignore[override]
        """Override: try ajax/chapters/ then admin-ajax.php fallback."""
        # This is normally called from get_chapter_list(); we need the novel URL.
        # store it via a side-channel set by get_chapter_list hook.
        return super().select_chapter_refs(html, base_url)

    async def _fetch_ajax_chapter_list(
        self, novel_url: str, page_html: str
    ) -> Optional[List[ChapterRef]]:
        """Try the two Madara ajax strategies; return refs or None on failure."""
        clean_novel_url = novel_url.split("?")[0].rstrip("/")

        # Strategy 1: POST {novel}/ajax/chapters/
        try:
            ajax_html = await post_html(
                f"{clean_novel_url}/ajax/chapters/",
                data={},
                site_id=self.site_id,
            )
            refs = super().select_chapter_refs(ajax_html, self.base_url or novel_url)
            if refs:
                refs.reverse()
                for i, r in enumerate(refs):
                    r.index = i
                return refs
        except Exception:
            logger.debug("ajax/chapters/ failed for %s", novel_url, exc_info=True)

        # Strategy 2: admin-ajax.php with manga ID
        try:
            soup = BeautifulSoup(page_html, "html.parser")
            holder = soup.select_one("#manga-chapters-holder[data-id]")
            if not holder:
                return None
            manga_id = holder.get("data-id", "")
            origin_root = "/".join(novel_url.split("/")[:3])
            ajax_html = await post_html(
                f"{origin_root}/wp-admin/admin-ajax.php",
                data={"action": "manga_get_chapters", "manga": manga_id},
                site_id=self.site_id,
            )
            refs = super().select_chapter_refs(ajax_html, self.base_url or novel_url)
            if refs:
                refs.reverse()
                for i, r in enumerate(refs):
                    r.index = i
                return refs
        except Exception:
            logger.debug("admin-ajax.php fallback failed for %s", novel_url, exc_info=True)

        return None

    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        from urllib.parse import urlparse
        origin = urlparse(novel_url).netloc

        try:
            page_html = await fetch_html(novel_url, self.site_id)
        except Exception as exc:
            health.record(origin, "fetch_error", str(exc)[:200])
            raise

        # Try ajax strategy first
        ajax_refs = await self._fetch_ajax_chapter_list(novel_url, page_html)
        if ajax_refs is not None:
            return ajax_refs

        # Fallback: parse chapter list directly from page HTML
        logger.debug("Falling back to DOM chapter list for %s", novel_url)
        refs = super().select_chapter_refs(page_html, self.base_url or novel_url)

        # Follow next-page links if needed
        if self.pagination_next_selectors:
            seen = {r.source_url for r in refs}
            current_html = page_html
            current_url = novel_url
            while True:
                next_url = self._find_next_page(current_html, current_url)
                if not next_url or next_url in seen:
                    break
                seen.add(next_url)
                try:
                    current_html = await fetch_html(next_url, self.site_id)
                except Exception:
                    break
                new_refs = super().select_chapter_refs(current_html, self.base_url or novel_url)
                for r in new_refs:
                    if r.source_url not in seen:
                        seen.add(r.source_url)
                        r.index = len(refs)
                        refs.append(r)
                current_url = next_url

        # Same order as both ajax paths above: Madara lists newest-first, so
        # reverse once (AFTER pagination has appended later pages) and reindex.
        refs.reverse()
        for i, r in enumerate(refs):
            r.index = i
        return refs
