"""
NovelBridge — SoupAdapter: declarative selector-based adapter base class.

Inspired by lncrawl/core/template.py SoupTemplate.

Adding a new site = ~15 lines + a sites.json entry:
  - Subclass SoupAdapter
  - Set class-level selector lists + base_url + site_id
  - Override hooks only when the site needs non-standard behaviour

SiteAdapter ABC is unchanged for backward compatibility;
SoupAdapter is purely additive.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from backend.adapters.base import ChapterRef, NovelMeta, SiteAdapter
from backend.adapters.fetcher import (
    fetch_html,
    parse_chapter_body,
    parse_chapter_image_urls,
    parse_cover_url,
    parse_links,
    parse_text,
)
from backend.adapters.health import health
from backend.config import resolve_path

logger = logging.getLogger("novelbridge.soup_template")

# ── Shared bad-tag / bad-CSS cleaner defaults (from lncrawl/core/cleaner.py) ──

BAD_TAGS: set[str] = {
    "address", "amp-auto-ads", "audio", "button", "figcaption", "footer",
    "form", "header", "iframe", "input", "ins", "map", "nav", "noscript",
    "object", "output", "script", "select", "source", "style", "textarea",
    "tfoot", "video",
}

BAD_CSS: set[str] = {
    ".adblock-service", ".sharedaddy", ".saboxplugin-wrap", ".adbox",
    ".ads-middle", ".ads", ".adsbygoogle", ".adsense-code",
    ".cb_p6_patreon_button", ".code-block", ".ezoic-ad-adaptive",
    ".ezoic-ad", ".ezoic-adpicker-ad", ".googlepublisherads",
    ".inline-ad-slot", ".jp-relatedposts", ".wp-post-navigation",
    "a[href*='patreon.com']", "a[href*='paypal.me']",
}

# Attributes checked (in order) when resolving lazy-loaded cover images
LAZY_IMG_ATTRS = ("data-lazy-src", "data-original", "data-src", "srcset", "src", "content")

# ── Challenge-page markers used by check_response() ───────────────────────────

_CHALLENGE_MARKERS = (
    "cf-challenge",
    "just a moment",
    "captcha",
    "access denied",
    "enable javascript",
    "checking your browser",
    "ddos-guard",
    "please wait",
)
# Infrastructure identifiers that only ever appear on a real challenge page
# (never in prose or a legit head). Everything else needs >= 2 hits.
_STRONG_CHALLENGE_MARKERS = (
    "cf-challenge",
    "ddos-guard",
)
_ADBLOCK_MARKERS = (
    "disable your ad blocker",
    "disable adblock",
    "please turn off your adblocker",
)
_MIN_CHAPTER_CHARS = int(__import__("os").getenv("MIN_CHAPTER_CHARS", "50"))

# Noise excluded from the challenge scan: script/style/noscript/iframe blocks
# and HTML comments. Legit heads routinely mention "captcha", "please wait" or
# "enable javascript" inside script src URLs and <noscript> hints, which used
# to flag real content as a challenge page. The trailing alternatives handle
# blocks cut off by the slice boundary (unterminated body / opening tag).
_BLOCK_NOISE_RE = re.compile(
    r"<!--.*?-->"
    r"|<(script|style|noscript|iframe)\b[^>]*>.*?</\1\s*>"
    r"|<(script|style|noscript|iframe)\b[^>]*>.*\Z"
    r"|<(script|style|noscript|iframe)\b[^>]*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _challenge_scan_text(html: str) -> str:
    """Return the noise-free, lowercased head slice used for marker matching."""
    return _BLOCK_NOISE_RE.sub(" ", html[:3000]).lower()


def _strip_bad_html(html: str) -> str:
    """Remove known ad/nav/script tags from raw HTML before parsing."""
    soup = BeautifulSoup(html, "html.parser")
    for tag_name in BAD_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    for selector in BAD_CSS:
        for tag in soup.select(selector):
            tag.decompose()
    return str(soup)


def _load_site_selectors(site_id: str) -> dict:
    """Load selector overrides from config/sites.json for *site_id*."""
    cfg = resolve_path("config/sites.json")
    if cfg.exists():
        try:
            return (
                json.loads(cfg.read_text(encoding="utf-8"))
                .get(site_id, {})
                .get("selectors", {})
            )
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not load selector overrides from %s: %s", cfg, exc)
    return {}


def _split(value: str) -> list[str]:
    """Split a comma-separated CSS selector string into a list."""
    return [s.strip() for s in value.split(",") if s.strip()]


# ── SoupAdapter ────────────────────────────────────────────────────────────────

class SoupAdapter(SiteAdapter):
    """Declarative selector-based adapter.

    Subclasses only need to override class-level selectors + site_id/base_url.
    Hook methods (``select_chapter_refs``, ``clean_chapter_text``,
    ``check_response``, ``build_chapter_list_url``) may be overridden for
    site-specific quirks.

    Version / tier stamping support for content deduplication (§B4):
      - Set ``version = "2"`` in the subclass when selectors change.
      - The pipeline uses ``tiers.is_stale()`` to skip unchanged chapters.
    """

    # ── Class-level selector defaults (override in subclass) ──────────────────
    base_url: str = ""

    novel_title_selectors: list[str] = ['meta[property="og:title"]', "h1", "title"]
    novel_author_selectors: list[str] = ['meta[property="og:author"]', 'meta[name="author"]']
    novel_cover_selectors: list[str] = ['meta[property="og:image"]']
    novel_description_selectors: list[str] = ['meta[property="og:description"]', 'meta[name="description"]']

    chapter_list_selectors: list[str] = []
    chapter_title_selector: str = ""
    chapter_body_selectors: list[str] = []
    pagination_next_selectors: list[str] = []
    chapter_list_reverse: bool = False

    # Content versioning (bump when parse logic changes)
    version: str = "1"
    tier: str = "legacy"

    def __init__(self) -> None:
        self._chapter_html_cache: dict[str, str] = {}
        self._chapter_html_lock = threading.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Merge class selectors with any sites.json overrides at class-definition time
        if cls.site_id:
            sel = _load_site_selectors(cls.site_id)
            if sel.get("novel_title"):
                cls.novel_title_selectors = _split(sel["novel_title"])
            if sel.get("novel_author"):
                cls.novel_author_selectors = _split(sel["novel_author"])
            if sel.get("novel_cover"):
                cls.novel_cover_selectors = _split(sel["novel_cover"])
            if sel.get("novel_description"):
                cls.novel_description_selectors = _split(sel["novel_description"])
            if sel.get("chapter_list"):
                cls.chapter_list_selectors = _split(sel["chapter_list"])
            if sel.get("chapter_title"):
                cls.chapter_title_selector = sel["chapter_title"]
            if sel.get("chapter_text"):
                cls.chapter_body_selectors = _split(sel["chapter_text"])
            if sel.get("chapter_list_next"):
                cls.pagination_next_selectors = _split(sel["chapter_list_next"])

    # ── Hook methods (override for site-specific behaviour) ──────────────────

    def build_chapter_list_url(self, novel_url: str, page: int) -> Optional[str]:
        """Return the URL for chapter list page *page* (1-based).

        Return ``None`` to stop pagination.  Default: no pagination.
        """
        return None

    def select_chapter_refs(self, html: str, base_url: str) -> List[ChapterRef]:
        """Extract chapter refs from *html*.  Override for custom selection logic."""
        refs: list[ChapterRef] = []
        seen: set[str] = set()
        links = parse_links(html, self.chapter_list_selectors, base_url=base_url)
        for text, href in links:
            if not href or href in seen:
                continue
            seen.add(href)
            refs.append(ChapterRef(
                index=len(refs),
                title=text.strip() or f"Chapter {len(refs) + 1}",
                source_url=href,
            ))
        if self.chapter_list_reverse:
            refs.reverse()
            for i, r in enumerate(refs):
                r.index = i
        return refs

    def clean_chapter_text(self, text: str) -> str:
        """Post-process scraped chapter text.  Override for site-specific cleaning."""
        return text.strip()

    def check_response(self, html: str, status_code: int = 200) -> Optional[str]:
        """Detect a 200-but-challenge or adblock-wall response.

        Returns a reason string if the page is not real content, or None if OK.
        Default implementation detects common Cloudflare/DDoS-Guard patterns.

        A single generic marker is not enough: legit pages say "please wait"
        or "enable javascript" in prose and hints. Infrastructure identifiers
        (cf-challenge / ddos-guard) and two or more markers confirm a wall.
        """
        if not html:
            return "empty_body"
        lower = _challenge_scan_text(html)
        if any(marker in lower for marker in _STRONG_CHALLENGE_MARKERS):
            return "challenge"
        marker_hits = sum(1 for marker in _CHALLENGE_MARKERS if marker in lower)
        if marker_hits >= 2:
            return "challenge"
        for marker in _ADBLOCK_MARKERS:
            if marker in lower:
                return "adblock_wall"
        return None

    # ── SiteAdapter interface implementations ─────────────────────────────────

    async def fetch(self, url: str) -> str:
        """Fetch HTML for *url*. Subclasses or tests can override."""
        return await fetch_html(url, self.site_id)

    def parse_novel_metadata(self, html: str, novel_url: str) -> NovelMeta:
        """Extract NovelMeta from *html* using configured selectors."""
        title = parse_text(html, self.novel_title_selectors) or "Unknown Title"
        author = parse_text(html, self.novel_author_selectors)
        cover_url = parse_cover_url(html, self.novel_cover_selectors, novel_url) if self.novel_cover_selectors else None
        description = parse_text(html, self.novel_description_selectors)

        return NovelMeta(
            title=title,
            author=author,
            cover_url=cover_url,
            description=description,
            source_url=novel_url,
            source_site=self.site_id,
        )

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        origin = urlparse(novel_url).netloc
        try:
            html = await self.fetch(novel_url)
        except Exception as exc:
            health.record(origin, "fetch_error", str(exc)[:200])
            raise

        reason = self.check_response(html)
        if reason:
            health.record(origin, reason, novel_url)
            logger.warning("check_response() blocked metadata for %s: %s", novel_url, reason)

        return self.parse_novel_metadata(html, novel_url)

    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        origin = urlparse(novel_url).netloc
        all_refs: list[ChapterRef] = []
        seen_urls: set[str] = set()
        visited_pages: set[str] = {novel_url}
        page = 1
        next_page_url: Optional[str] = None

        while True:
            if page == 1:
                url = novel_url
            elif next_page_url is not None:
                url = next_page_url
            else:
                url = self.build_chapter_list_url(novel_url, page)
                if url is None or url in visited_pages:
                    break
                visited_pages.add(url)

            try:
                html = await fetch_html(url, self.site_id)
            except Exception as exc:
                health.record(origin, "fetch_error", str(exc)[:200])
                if page == 1:
                    raise
                break

            reason = self.check_response(html)
            if reason:
                health.record(origin, reason, url)

            page_refs = self.select_chapter_refs(html, self.base_url or novel_url)
            new = [r for r in page_refs if r.source_url not in seen_urls]
            if not new:
                break
            for r in new:
                seen_urls.add(r.source_url)
                r.index = len(all_refs)
                all_refs.append(r)

            # Follow the next-page link when the site exposes one. This must be
            # checked on every page (including page 1), otherwise next-link
            # pagination can never start.
            next_page_url = None
            if self.pagination_next_selectors:
                candidate = self._find_next_page(html, url)
                if candidate and candidate not in visited_pages:
                    next_page_url = candidate

            if next_page_url is None:
                # No next link: fall back to an explicit numbered URL scheme.
                candidate = self.build_chapter_list_url(novel_url, page + 1)
                if candidate is not None and candidate not in visited_pages:
                    next_page_url = candidate

            if next_page_url is None:
                break
            visited_pages.add(next_page_url)
            page += 1

        return all_refs

    async def get_chapter_text(self, chapter_url: str) -> str:
        origin = urlparse(chapter_url).netloc
        try:
            html = await fetch_html(chapter_url, self.site_id)
        except Exception as exc:
            health.record(origin, "fetch_error", str(exc)[:200])
            raise
        with self._chapter_html_lock:
            self._chapter_html_cache[chapter_url] = html
            if len(self._chapter_html_cache) > 64:
                self._chapter_html_cache.pop(next(iter(self._chapter_html_cache)))

        reason = self.check_response(html)
        if reason:
            health.record(origin, reason, chapter_url)
            logger.warning("check_response() blocked chapter %s: %s", chapter_url, reason)
            # Raise instead of returning "": an empty string would be counted
            # as an (retryable) empty chapter; this is a blocked fetch.
            raise RuntimeError(
                f"Blocked by {reason} while fetching {chapter_url}"
            )

        cleaned_html = _strip_bad_html(html)
        text = parse_chapter_body(cleaned_html, self.chapter_body_selectors)
        if not text:
            text = parse_text(cleaned_html, self.chapter_body_selectors)
        text = self.clean_chapter_text(text or "")

        if len(text) < _MIN_CHAPTER_CHARS:
            health.record(origin, "empty_body", chapter_url)
            logger.warning(
                "Chapter text too short (%d chars) for %s — treating as empty",
                len(text), chapter_url,
            )
            return ""
        return text

    async def get_chapter_images(self, chapter_url: str) -> list[dict[str, str]]:
        with self._chapter_html_lock:
            html = self._chapter_html_cache.pop(chapter_url, None)
        if html is None:
            html = await self.fetch(chapter_url)
        return parse_chapter_image_urls(
            html,
            self.chapter_body_selectors,
            chapter_url,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _find_next_page(self, html: str, current_url: str) -> Optional[str]:
        """Find the next-page link in *html* using ``pagination_next_selectors``."""
        soup = BeautifulSoup(html, "html.parser")
        for selector in self.pagination_next_selectors:
            tag = soup.select_one(selector)
            if tag:
                href = tag.get("href", "")
                if href and not href.strip().startswith("javascript"):
                    return urljoin(current_url, href)
        return None
