"""Ranovel.com site adapter."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import List
from urllib.parse import urljoin

logger = logging.getLogger("novelbridge.adapters.ranovel")

try:
    from selectolax.parser import HTMLParser
    _SELECTOLAX_AVAILABLE = True
except ImportError:
    _SELECTOLAX_AVAILABLE = False

# Always import BeautifulSoup — used for honeypot cleaning regardless of selectolax.
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

_SITE_ID = "ranovel"
_BASE = "https://ranovel.com"

_cfg_path = Path("config/sites.json")
_SEL: dict = {}
if _cfg_path.exists():
    _SEL = json.loads(_cfg_path.read_text(encoding="utf-8")).get(_SITE_ID, {}).get("selectors", {})

# ── Honeypot / watermark cleaning ────────────────────────────────────────────

# Text patterns Ranovel injects inside hidden paragraphs.
_WATERMARK_RE = re.compile(
    r"ran\(?o\)?vel"
    r"|ra/n\(o\)ve/l"
    r"|read\s+novel"
    r"|ranovel\s+dot\s+com"
    r"|korean\s+novel"
    r"|only\s+ran",
    re.IGNORECASE,
)


def _color_equals_bg(style: str) -> bool:
    """Return True when CSS 'color' equals 'background-color' (invisible text trick)."""
    c = re.search(r"(?:^|;)\s*color\s*:\s*([^;]+)", style, re.IGNORECASE)
    b = re.search(r"background(?:-color)?\s*:\s*([^;]+)", style, re.IGNORECASE)
    if c and b:
        return c.group(1).strip().lower() == b.group(1).strip().lower()
    return False


def _clean_chapter_html(html: str) -> str:
    """Strip Ranovel honeypot/watermark injections from raw chapter HTML.

    Removes:
    * Elements whose CSS ``color`` == ``background-color`` (invisible text).
    * ``<div class="code-block …">`` containers (ad/watermark wrappers).
    * Paragraphs whose plain text matches known watermark patterns.
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1. Remove invisible-text elements (same color as background).
    for el in soup.find_all(style=True):
        if _color_equals_bg(el.get("style", "")):
            el.decompose()

    # 2. Remove code-block divs (ad / watermark wrappers).
    for el in soup.find_all(
        "div",
        class_=lambda c: c and any("code-block" in cls for cls in c),
    ):
        el.decompose()

    # 3. Remove any surviving paragraph whose text matches a watermark pattern.
    for el in soup.find_all("p"):
        if _WATERMARK_RE.search(el.get_text()):
            el.decompose()

    return str(soup)


async def _fetch_ranovel_html(url: str, wait_selector: str = "") -> str:
    """Fetch a Ranovel page, bypassing Cloudflare.

    Strategy (in order):
    1. **nodriver** — launches an undetected Chrome binary; Cloudflare's JS
       challenge completes automatically inside the real browser (~15-20 s).
    2. **Stealth Playwright** — fallback if nodriver is not installed or fails.
    """
    try:
        return await fetch_html_nodriver(url, wait_selector=wait_selector, timeout=45)
    except ImportError:
        logger.info("nodriver not installed; falling back to stealth Playwright")
    except Exception as e:
        logger.warning("nodriver fetch failed for %s: %s; falling back to stealth Playwright", url, e)
    return await fetch_html_playwright(url, wait_selector)



def _split_selectors(value: str) -> list[str]:
    return [selector.strip() for selector in value.split(",") if selector.strip()]


def _chapter_number(url: str) -> int:
    match = re.search(r"/chapter-(\d+)(?:[/#?]|$)", url, re.IGNORECASE)
    return int(match.group(1)) if match else -1


_STYLE_COLOR_RE = re.compile(r"(?:^|;)\s*color\s*:\s*(#[0-9a-f]{3,8}|[a-z]+)", re.IGNORECASE)
_STYLE_BG_RE = re.compile(r"(?:^|;)\s*background(?:-color)?\s*:\s*(#[0-9a-f]{3,8}|[a-z]+)", re.IGNORECASE)


def _is_hidden_style(style: str) -> bool:
    """Detect text hidden via CSS (used by ranovel.com to poison scrapers).

    Ranovel injects extra <p> tags whose inline style sets the text color
    identical to the background color, making them invisible on the page
    but still present in the raw HTML/text. Also catches display:none and
    visibility:hidden as a general precaution.
    """
    if not style:
        return False
    if re.search(r"display\s*:\s*none", style, re.IGNORECASE):
        return True
    if re.search(r"visibility\s*:\s*hidden", style, re.IGNORECASE):
        return True
    color_match = _STYLE_COLOR_RE.search(style)
    bg_match = _STYLE_BG_RE.search(style)
    if color_match and bg_match and color_match.group(1).strip().lower() == bg_match.group(1).strip().lower():
        return True
    return False


def _extract_description(html: str, selectors: list[str]) -> str:
    """Extract the novel synopsis, skipping honeypot paragraphs.

    Ranovel's description block contains decoy <p> tags with matching
    text/background color (invisible to a reader) stuffed with
    self-promotional filler text. A naive text-dump of the container picks
    that up as if it were part of the synopsis. This walks the paragraph
    (and blockquote) children instead and drops any with a hiding style.
    """
    if _SELECTOLAX_AVAILABLE:
        tree = HTMLParser(html)
        container = None
        for selector in selectors:
            container = tree.css_first(selector)
            if container is not None:
                break
        if container is None:
            return ""
        parts: list[str] = []
        for node in container.css("p, blockquote"):
            if _is_hidden_style(node.attributes.get("style", "") or ""):
                continue
            text = node.text(strip=True)
            if text:
                parts.append(text)
        if not parts:
            parts = [container.text(strip=True)]
        return "\n\n".join(parts).strip()

    soup = BeautifulSoup(html, "html.parser")
    container = None
    for selector in selectors:
        container = soup.select_one(selector)
        if container is not None:
            break
    if container is None:
        return ""
    parts = []
    for node in container.find_all(["p", "blockquote"]):
        if _is_hidden_style(node.get("style") or ""):
            continue
        text = node.get_text(strip=True)
        if text:
            parts.append(text)
    if not parts:
        parts = [container.get_text(strip=True)]
    return "\n\n".join(parts).strip()


def _extract_refs(html: str, page_url: str, seen: set[str], start_index: int) -> list[ChapterRef]:
    selectors = _split_selectors(_SEL.get("chapter_list", "#manga-chapters-holder .wp-manga-chapter a"))
    refs: list[ChapterRef] = []
    if _SELECTOLAX_AVAILABLE:
        tree = HTMLParser(html)
        nodes = tree.css(", ".join(selectors))
        for node in nodes:
            href = node.attributes.get("href", "")
            if not href:
                continue
            href = urljoin(page_url, href)
            if "/chapter-" not in href or href in seen:
                continue
            seen.add(href)
            title = node.text(strip=True) or node.attributes.get("title", "").strip()
            refs.append(ChapterRef(start_index + len(refs), title or f"Chapter {start_index + len(refs) + 1}", href))
        return refs

    for title, href in parse_links(html, selectors, base_url=page_url):
        if "/chapter-" not in href or href in seen:
            continue
        seen.add(href)
        refs.append(ChapterRef(start_index + len(refs), title.strip() or f"Chapter {start_index + len(refs) + 1}", href))
    return refs


class RanovelAdapter(SiteAdapter):
    site_id = _SITE_ID
    source_language = "English"
    scraping_method = "nodriver (Cloudflare bypass)"

    def can_handle(self, url: str) -> bool:
        return "ranovel.com" in url.lower()

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        html = await _fetch_ranovel_html(novel_url, ".post-title, .summary_content")
        title = parse_text(html, _split_selectors(_SEL.get("novel_title", ".post-title, h1.entry-title, h1"))) or "Unknown Title"
        # .author-content a is scoped to the "Author(s)" row specifically; the
        # broader selectors are only fallbacks for markup variants.
        author = parse_text(html, _split_selectors(_SEL.get("novel_author", ".author-content a, .post-content_item .summary-content a, .post-content_item")))
        cover_url = parse_attr(html, _split_selectors(_SEL.get("novel_cover", ".summary_image img, .summary_image a img, .profile-manga .summary_image img")), "src")
        description = _extract_description(
            html,
            _split_selectors(_SEL.get("novel_description", ".description-summary .summary__content, .description-summary, .summary_content")),
        )
        return NovelMeta(
            title=title,
            author=author,
            cover_url=urljoin(_BASE, cover_url) if cover_url else None,
            description=description,
            source_url=novel_url,
            source_site=_SITE_ID,
        )

    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        """Load the rendered Ranovel chapter list and any linked list pages.

        Fetches each page via :func:`_fetch_ranovel_html` (subprocess nodriver
        so Chrome is isolated from the Qt GUI process), then finds the next-page
        link in the returned HTML and repeats until there are no more pages.
        """
        chapter_selector = _SEL.get(
            "chapter_list", "#manga-chapters-holder .wp-manga-chapter a"
        )
        next_selector_list = _split_selectors(
            _SEL.get(
                "chapter_list_next",
                ".pagination a.next, .nav-links .nav-next a, a[rel=next]",
            )
        )

        refs: list[ChapterRef] = []
        seen: set[str] = set()
        visited: set[str] = set()
        current_url: str | None = novel_url

        while current_url and current_url not in visited:
            visited.add(current_url)
            html = await _fetch_ranovel_html(
                current_url,
                wait_selector=chapter_selector.split(",")[0].strip(),
            )
            refs.extend(_extract_refs(html, current_url, seen, len(refs)))

            # Check for a next-page link in the fetched HTML.
            soup = BeautifulSoup(html, "html.parser")
            next_a = None
            for sel in next_selector_list:
                next_a = soup.select_one(sel)
                if next_a:
                    break

            href = next_a.get("href") if next_a else None
            if href and (href.strip() == "#" or href.strip().lower().startswith("javascript:")):
                href = None
            current_url = urljoin(current_url, href) if href else None

        return self._ordered(refs)

    @staticmethod
    def _ordered(refs: list[ChapterRef]) -> list[ChapterRef]:
        refs.sort(key=lambda ref: (_chapter_number(ref.source_url), ref.source_url))
        for index, ref in enumerate(refs):
            ref.index = index
        return refs

    async def get_chapter_text(self, chapter_url: str) -> str:
        html = await _fetch_ranovel_html(
            chapter_url,
            wait_selector=".reading-content, .text-left, .entry-content",
        )

        # Detect Cloudflare / anti-bot challenge pages that contain no real content.
        if not html or (
            ".reading-content" not in html
            and "just a moment" in html.lower()
        ):
            raise RuntimeError(
                f"Ranovel is blocked by Cloudflare at {chapter_url}. "
                "Cloudflare challenge did not resolve within timeout. "
                "Ensure Chrome is installed and retry."
            )

        # Strip invisible honeypot text and watermark injections before parsing.
        html = _clean_chapter_html(html)

        selectors = _split_selectors(_SEL.get("chapter_text", ".reading-content, .text-left, .entry-content, #chapter-container"))
        text = parse_chapter_body(html, selectors) or parse_text(html, selectors) or ""

        # Final safety-net: drop any line that still mentions the watermark site.
        lines = [
            ln for ln in text.splitlines()
            if not _WATERMARK_RE.search(ln)
        ]
        return "\n".join(lines).strip()