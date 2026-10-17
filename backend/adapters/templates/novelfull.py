"""
NovelBridge — NovelFull-style adapter template.

Ported selectors and ajax chapter-option logic from
lncrawl/templates/novelfull.py (MIT).

Sites like novelfull.com use a ``<select>`` element with chapter options
and/or an ajax endpoint ``ajax-chapter-option?novelId=`` to load
the full chapter list without pagination.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from backend.adapters.base import ChapterRef
from backend.adapters.fetcher import fetch_html
from backend.adapters.health import health
from backend.adapters.soup_template import SoupAdapter

logger = logging.getLogger("novelbridge.templates.novelfull")


class NovelFullAdapter(SoupAdapter):
    """Shared NovelFull-style selectors + ajax chapter-option loading.

    Minimal subclass example::

        class MyNovelFullSiteAdapter(NovelFullAdapter):
            site_id = "mysite"
            base_url = "https://mysite.com"

            def can_handle(self, url: str) -> bool:
                return "mysite.com" in url
    """

    novel_title_selectors = ["h3.title", "h1.title", "h1"]
    novel_cover_selectors = [".book img", ".info-holder .book img"]
    novel_author_selectors = [".info-holder .info .author a", ".info .author a"]
    novel_description_selectors = ["#tab-description p", ".desc-text p", ".desc p"]

    chapter_list_selectors = ["ul.list-chapter > li > a[href]", "select > option[value]"]
    chapter_body_selectors = ["#chr-content", "#chapter-content", ".chapter-content"]
    pagination_next_selectors = [".pagination .next", 'li.next > a[href]']

    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        origin = urlparse(novel_url).netloc
        try:
            page_html = await fetch_html(novel_url, self.site_id)
        except Exception as exc:
            health.record(origin, "fetch_error", str(exc)[:200])
            raise

        # Try ajax-chapter-option endpoint first
        ajax_refs = await self._fetch_ajax_chapter_option(novel_url, page_html)
        if ajax_refs is not None:
            return ajax_refs

        # Fall back to parsing select/option or list-chapter directly from page
        refs = self.select_chapter_refs(page_html, self.base_url or novel_url)

        # Handle pagination
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
                for r in self.select_chapter_refs(current_html, self.base_url or novel_url):
                    if r.source_url not in seen:
                        seen.add(r.source_url)
                        r.index = len(refs)
                        refs.append(r)
                current_url = next_url

        return refs

    def select_chapter_refs(self, html: str, base_url: str) -> List[ChapterRef]:
        """Also handle ``<select><option value=URL>`` chapter pickers."""
        # Standard link-based parsing
        refs = super().select_chapter_refs(html, base_url)
        if refs:
            return refs
        # Fallback: option-value chapter picker
        soup = BeautifulSoup(html, "html.parser")
        refs = []
        for opt in soup.select("select > option[value]"):
            href = opt.get("value", "").strip()
            if not href or href == "#":
                continue
            full_url = urljoin(base_url, href)
            title = opt.get_text(strip=True) or f"Chapter {len(refs) + 1}"
            refs.append(ChapterRef(index=len(refs), title=title, source_url=full_url))
        return refs

    async def _fetch_ajax_chapter_option(
        self, novel_url: str, page_html: str
    ) -> Optional[List[ChapterRef]]:
        """Try the ajax-chapter-option?novelId= endpoint."""
        soup = BeautifulSoup(page_html, "html.parser")
        # Find novelId from the page
        novel_id = None
        for tag in soup.select("[data-novel-id]"):
            novel_id = tag.get("data-novel-id")
            break
        if not novel_id:
            # Try extracting from hidden input or page URL
            inp = soup.select_one('input[name="novel_id"], input[name="novelId"]')
            if inp:
                novel_id = inp.get("value")

        if not novel_id:
            return None

        origin_root = "/".join(novel_url.split("/")[:3])
        ajax_url = f"{origin_root}/ajax-chapter-option?novelId={novel_id}"
        try:
            ajax_html = await fetch_html(ajax_url, self.site_id)
            refs = self.select_chapter_refs(ajax_html, self.base_url or novel_url)
            if refs:
                return refs
        except Exception:
            logger.debug("ajax-chapter-option failed for %s", novel_url, exc_info=True)
        return None
