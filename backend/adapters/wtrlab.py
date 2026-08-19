"""
NovelBridge — WTR-Lab.com site adapter.

Corrected version.

Key behavior:
- Metadata is fetched with ?tab=about.
- Chapter list is fetched with ?tab=toc.
- The full chapter list on wtr-lab.com's current site is split into
  collapsed accordion sections ("Chapters 1 - 250", "Chapters 251 -
  500", ...). Those sections are populated by client-side JavaScript
  only after they are clicked open — they are NOT present in the
  server-rendered HTML at all. A plain HTTP GET therefore only ever
  sees the handful of links that already exist in the raw markup:
  the "Start Reading" link and the ~5-chapter "Latest Release"
  widget.
- To get the real, complete list this adapter drives a headless
  browser (Playwright) that opens every accordion section before
  reading the rendered DOM. If Playwright isn't installed, it falls
  back to a plain HTTP fetch, which will only recover the small
  "Latest Release"/"Start Reading" set described above.
- The "Latest Release" widget is ignored where detectable, so it
  never masquerades as the full chapter list.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

try:
    from selectolax.parser import HTMLParser

    _SELECTOLAX_AVAILABLE = True
except ImportError:
    _SELECTOLAX_AVAILABLE = False

try:
    from bs4 import BeautifulSoup

    _BS4_AVAILABLE = True
except ImportError:
    BeautifulSoup = None
    _BS4_AVAILABLE = False

try:
    from playwright.async_api import async_playwright

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False

from backend.adapters.base import ChapterRef, NovelMeta, SiteAdapter
from backend.adapters.fetcher import (
    fetch_html,
    parse_attr,
    parse_chapter_body,
    parse_text,
)

_SITE_ID = "wtrlab"
_BASE = "https://wtr-lab.com"

_CHAPTER_NUM_RE = re.compile(r"/chapter-(\d+)(?:[/?#]|$)")

_LATEST_CLASS_TOKENS = (
    "toc-latest-row",
    "toc-latest-list",
    "latest-release",
    "latest-list",
)

_TOC_CONTAINER_SELECTORS = (
    ".table-of-content",
    "[class*='table-of-content']",
    "[class*='toc-list']",
    "[class*='chapter-list']",
)

_cfg_path = Path("config/sites.json")
_SEL: dict = {}

if _cfg_path.exists():
    try:
        _SEL = (
            json.loads(_cfg_path.read_text(encoding="utf-8"))
            .get(_SITE_ID, {})
            .get("selectors", {})
        )
    except Exception:
        _SEL = {}


def _split_selectors(value: str) -> List[str]:
    return [s.strip() for s in value.split(",") if s.strip()]


def _with_tab(url: str, tab: str) -> str:
    """
    Return `url` with its `tab` query parameter set/replaced.

    Examples:
        ?tab=about
        ?tab=toc
    """
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    query["tab"] = tab
    return urlunparse(parsed._replace(query=urlencode(query)))


def _chapter_number(url: str) -> Optional[int]:
    m = _CHAPTER_NUM_RE.search(url)
    return int(m.group(1)) if m else None


_NOVEL_ID_RE = re.compile(r"/novel/(\d+)(?:/|$|\?)")


def _novel_id_from_url(url: str) -> Optional[str]:
    """
    Extract the numeric novel id from a wtr-lab.com URL, e.g.
    ".../novel/91978/i-took-my-whole-family-in-an-airship-to-survive"
    -> "91978".

    Used to detect and filter out chapter links that ended up pointing
    at the wrong novel (e.g. after a stray client-side navigation).
    """
    m = _NOVEL_ID_RE.search(url)
    return m.group(1) if m else None


class WTRLabAdapter(SiteAdapter):
    site_id = _SITE_ID

    def can_handle(self, url: str) -> bool:
        return "wtr-lab.com" in url

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        """
        Fetch novel metadata.

        The description is only reliably present on the About tab, so this
        explicitly requests ?tab=about.
        """
        html = await fetch_html(_with_tab(novel_url, "about"), _SITE_ID)

        title = parse_text(
            html,
            _split_selectors(_SEL.get("novel_title", "h1")),
        )

        author = parse_text(
            html,
            _split_selectors(_SEL.get("novel_author", "")),
        )

        cover_url = parse_attr(
            html,
            _split_selectors(_SEL.get("novel_cover", "")),
            "src",
        )

        description = parse_text(
            html,
            _split_selectors(_SEL.get("novel_description", "")),
        )

        native_title: Optional[str] = None
        stats: Dict[str, str] = {}
        genres: List[str] = []

        if _SELECTOLAX_AVAILABLE:
            tree = HTMLParser(html)

            h1 = tree.css_first("h1")

            if not title and h1 is not None:
                title = h1.text(strip=True)

            # Native-language title, usually the Chinese title under the H1.
            if not native_title and h1 is not None:
                header = h1.parent

                for _ in range(3):
                    if header is None:
                        break

                    for p in header.css("p"):
                        t = p.text(strip=True)

                        if t and t != title and any(ord(ch) > 0x2E80 for ch in t):
                            native_title = t
                            break

                    if native_title:
                        break

                    header = header.parent

            # Cover image.
            if not cover_url:
                for img in tree.css("img"):
                    if img.attributes.get("alt") == title:
                        cover_url = img.attributes.get("src")
                        break

            if not cover_url:
                for img in tree.css("img"):
                    src = img.attributes.get("src") or ""

                    if "series" in src or "serie" in src:
                        cover_url = src
                        break

            if cover_url:
                cover_url = urljoin(_BASE, cover_url)

            # Stat cards.
            for card in tree.css("div.flex.flex-col.rounded-lg"):
                spans = card.css("span")

                if len(spans) >= 2:
                    label = spans[0].text(strip=True)
                    value = spans[-1].text(strip=True)

                    if label:
                        stats[label.lower()] = value

            # Genre/tag badges.
            for span in tree.css("span.capitalize"):
                t = span.text(strip=True)

                if t and t not in ("Male", "Female") and t not in genres:
                    genres.append(t)

            # Description fallback.
            if not description:
                panel = tree.css_first("[role=tabpanel]")

                if panel is not None:
                    for p in panel.css("p"):
                        t = p.text(strip=True)

                        if len(t) > 60:
                            description = t
                            break

        return NovelMeta(
            title=title or "Unknown Title",
            author=author,
            cover_url=cover_url,
            description=description,
            source_url=novel_url,
            source_site=_SITE_ID,
        )

    # ------------------------------------------------------------------
    # Chapter list
    # ------------------------------------------------------------------
    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        """
        Fetch the complete chapter list.

        The full list lives entirely under a single ?tab=toc URL, split
        into collapsed accordion sections (e.g. "Chapters 1 - 250").
        Those sections only render their chapter rows client-side, after
        being clicked open, so a plain HTTP GET can never see them.

        When Playwright is available, this drives a real (headless)
        browser: load the page, click every accordion section open, then
        read the fully rendered DOM. This is the supported path.

        Without Playwright, this degrades to a single plain HTTP fetch,
        which will only recover the "Start Reading" link and the
        handful of chapters shown in the "Latest Release" widget — not
        the full list. A warning-worthy situation, but better than
        crashing outright.
        """
        toc_url = _with_tab(novel_url, "toc")
        expected_novel_id = _novel_id_from_url(novel_url)

        if _PLAYWRIGHT_AVAILABLE:
            html = await self._fetch_rendered_toc_html(toc_url, expected_novel_id)
            refs = self._extract_chapter_refs(html, toc_url)
        else:
            html = await fetch_html(toc_url, _SITE_ID)
            refs = self._extract_chapter_refs(html, toc_url)

            # The "Latest Release" widget commonly shows only five
            # chapters. If we see five or fewer, try the Next.js-data
            # fallback (a no-op on the current site, but harmless and
            # kept in case a future/alternate build restores it).
            if len(refs) <= 5:
                refs.extend(self._extract_chapters_from_next_data(html, toc_url))

        # Safety net: no matter what happened upstream (a stray SPA
        # navigation during the accordion-click loop, a stale cached
        # page, etc.), never let a chapter link belonging to a
        # different novel slip into the result.
        if expected_novel_id is not None:
            good_refs = []
            dropped = 0
            for ref in refs:
                ref_id = _novel_id_from_url(ref.source_url)
                if ref_id is not None and ref_id != expected_novel_id:
                    dropped += 1
                    continue
                good_refs.append(ref)
            if dropped:
                print(
                    f"[wtrlab] warning: dropped {dropped} chapter link(s) "
                    f"belonging to a different novel id while scraping "
                    f"novel {expected_novel_id!r} — the headless browser "
                    f"likely navigated away mid-scrape."
                )
            refs = good_refs

        by_url: Dict[str, ChapterRef] = {ref.source_url: ref for ref in refs}

        ordered = sorted(
            by_url.values(),
            key=lambda r: (
                _chapter_number(r.source_url) is None,
                _chapter_number(r.source_url) or 0,
            ),
        )

        return [
            ChapterRef(
                index=i,
                title=r.title,
                source_url=r.source_url,
            )
            for i, r in enumerate(ordered)
        ]

    async def _fetch_rendered_toc_html(
        self, toc_url: str, expected_novel_id: Optional[str] = None
    ) -> str:
        """
        Load the TOC page in a real (headless) browser, click open every
        accordion section, and return the fully rendered HTML.

        This is required because wtr-lab.com only populates an
        accordion section's chapter rows (e.g. "Chapters 251 - 500")
        after that section has been clicked; the rows do not exist in
        the server-rendered HTML beforehand.

        This app is client-side routed (Next.js), so a click can
        silently navigate the browser to an entirely different page
        (e.g. a different novel) without raising any error. To guard
        against that:
        - Only elements whose visible text actually looks like
          "Chapters N - M" are clicked (never a generically-matched
          "accordion-trigger" that might belong to an unrelated widget).
        - After every click, the current URL is checked. If it no
          longer points at the expected novel, we navigate straight
          back to toc_url before continuing, and skip counting that
          click's result.
        """
        chapters_pattern = re.compile(r"chapters?\s+\d+\s*-\s*\d+", re.IGNORECASE)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)

            try:
                page = await browser.new_page()
                await page.goto(toc_url, wait_until="networkidle")

                triggers = page.locator('[data-slot="accordion-trigger"]')
                trigger_count = await triggers.count()

                # Snapshot which trigger indices are genuinely chapter-range
                # accordions *before* clicking anything, since clicking can
                # change what's on the page and shift indices around.
                chapter_trigger_indices = []
                for i in range(trigger_count):
                    try:
                        text = (await triggers.nth(i).inner_text()).strip()
                    except Exception:
                        continue
                    if chapters_pattern.search(text):
                        chapter_trigger_indices.append(i)

                for i in chapter_trigger_indices:
                    # Re-query each time: the DOM may have re-rendered
                    # after the previous click.
                    triggers = page.locator('[data-slot="accordion-trigger"]')

                    if i >= await triggers.count():
                        continue

                    trigger = triggers.nth(i)

                    try:
                        text = (await trigger.inner_text()).strip()
                        if not chapters_pattern.search(text):
                            # DOM shifted under us; this index no longer
                            # points at a chapter-range trigger. Skip it
                            # rather than risk clicking something else.
                            continue

                        await trigger.scroll_into_view_if_needed()
                        await trigger.click()

                        # Give the click handler time to fetch/render the
                        # section's chapter rows before moving on.
                        await page.wait_for_load_state("networkidle")
                        await page.wait_for_timeout(300)
                    except Exception:
                        # If one section fails to expand, keep going —
                        # we still want whatever the other sections give.
                        continue

                    # Guard against a silent client-side navigation away
                    # from this novel (this site is a Next.js SPA, so a
                    # misdirected click doesn't throw — it just changes
                    # the page).
                    if expected_novel_id is not None:
                        current_id = _novel_id_from_url(page.url)
                        if current_id is not None and current_id != expected_novel_id:
                            print(
                                f"[wtrlab] warning: navigated away to novel "
                                f"{current_id!r} while expecting "
                                f"{expected_novel_id!r}; returning to TOC."
                            )
                            await page.goto(toc_url, wait_until="networkidle")

                # Final settle, in case the last click's content was
                # still streaming in.
                await page.wait_for_timeout(500)

                return await page.content()
            finally:
                await browser.close()

    # ------------------------------------------------------------------
    # DOM extraction
    # ------------------------------------------------------------------
    def _extract_chapter_refs(self, html: str, page_url: str) -> List[ChapterRef]:
        if _SELECTOLAX_AVAILABLE:
            return self._extract_chapter_refs_selectolax(html, page_url)

        if _BS4_AVAILABLE:
            return self._extract_chapter_refs_bs4(html, page_url)

        return []

    def _extract_chapter_refs_selectolax(self, html: str, page_url: str) -> List[ChapterRef]:
        tree = HTMLParser(html)

        title_candidates: Dict[str, List[str]] = {}
        order: Dict[str, int] = {}
        counter = 0

        def consider(anchor) -> None:
            nonlocal counter

            href = self._attr(anchor, "href")
            full_url = self._valid_chapter_url(href, page_url)

            if not full_url:
                return

            if full_url not in order:
                order[full_url] = counter
                counter += 1
                title_candidates[full_url] = []

            texts: List[str] = []

            try:
                whole = anchor.text(strip=True)
                if whole:
                    texts.append(whole)
            except Exception:
                pass

            try:
                for span in anchor.css("span"):
                    t = span.text(strip=True)
                    if t:
                        texts.append(t)
            except Exception:
                pass

            title_candidates[full_url].extend(texts)

        # 1. Configured selector.
        for selector in _split_selectors(_SEL.get("chapter_list", "")):
            try:
                nodes = tree.css(selector)
            except Exception:
                continue

            for node in nodes:
                if self._attr(node, "href"):
                    consider(node)
                else:
                    try:
                        for a in node.css("a[href*='/chapter-']"):
                            consider(a)
                    except Exception:
                        continue

        # 2. Known TOC containers.
        containers = []

        for selector in _TOC_CONTAINER_SELECTORS:
            try:
                containers.extend(tree.css(selector))
            except Exception:
                continue

        for container in containers:
            if self._has_latest_class_selectolax(container):
                continue

            try:
                anchors = container.css("a[href*='/chapter-']")
            except Exception:
                continue

            for a in anchors:
                if self._is_inside_latest_selectolax(a):
                    continue

                consider(a)

        # 3. Conservative global fallback.
        if not order:
            try:
                anchors = tree.css("a[href*='/chapter-']")
            except Exception:
                anchors = []

            for a in anchors:
                if self._is_inside_latest_selectolax(a):
                    continue

                consider(a)

        refs: List[ChapterRef] = []

        for url, _index in sorted(order.items(), key=lambda item: item[1]):
            chapter_num = _chapter_number(url)
            title = self._choose_best_title(
                title_candidates.get(url, []),
                chapter_num,
                len(refs) + 1,
            )

            refs.append(
                ChapterRef(
                    index=len(refs),
                    title=title,
                    source_url=url,
                )
            )

        return refs

    def _extract_chapter_refs_bs4(self, html: str, page_url: str) -> List[ChapterRef]:
        if not _BS4_AVAILABLE:
            return []

        soup = BeautifulSoup(html, "html.parser")

        title_candidates: Dict[str, List[str]] = {}
        order: Dict[str, int] = {}
        counter = 0

        def consider(anchor) -> None:
            nonlocal counter

            href = anchor.get("href", "")
            full_url = self._valid_chapter_url(href, page_url)

            if not full_url:
                return

            if full_url not in order:
                order[full_url] = counter
                counter += 1
                title_candidates[full_url] = []

            texts: List[str] = []

            whole = anchor.get_text(" ", strip=True)
            if whole:
                texts.append(whole)

            for span in anchor.find_all("span"):
                t = span.get_text(" ", strip=True)
                if t:
                    texts.append(t)

            title_candidates[full_url].extend(texts)

        # 1. Configured selector.
        for selector in _split_selectors(_SEL.get("chapter_list", "")):
            try:
                nodes = soup.select(selector)
            except Exception:
                continue

            for node in nodes:
                if node.name == "a":
                    consider(node)
                else:
                    for a in node.select("a[href*='/chapter-']"):
                        consider(a)

        # 2. Known TOC containers.
        containers = []

        for selector in _TOC_CONTAINER_SELECTORS:
            try:
                containers.extend(soup.select(selector))
            except Exception:
                continue

        for container in containers:
            if self._has_latest_class_bs4(container):
                continue

            for a in container.select("a[href*='/chapter-']"):
                if self._is_inside_latest_bs4(a):
                    continue

                consider(a)

        # 3. Conservative global fallback.
        if not order:
            for a in soup.select("a[href*='/chapter-']"):
                if self._is_inside_latest_bs4(a):
                    continue

                consider(a)

        refs: List[ChapterRef] = []

        for url, _index in sorted(order.items(), key=lambda item: item[1]):
            chapter_num = _chapter_number(url)
            title = self._choose_best_title(
                title_candidates.get(url, []),
                chapter_num,
                len(refs) + 1,
            )

            refs.append(
                ChapterRef(
                    index=len(refs),
                    title=title,
                    source_url=url,
                )
            )

        return refs

    # ------------------------------------------------------------------
    # Next.js fallback
    # ------------------------------------------------------------------
    def _extract_chapters_from_next_data(self, html: str, page_url: str) -> List[ChapterRef]:
        """
        Extract chapter references from Next.js embedded JSON, if present.

        This is useful when the complete TOC is not rendered directly in
        the initial DOM.
        """
        raw_json: Optional[str] = None

        if _SELECTOLAX_AVAILABLE:
            tree = HTMLParser(html)
            script = tree.css_first("script#__NEXT_DATA__")

            if script is not None:
                try:
                    raw_json = script.text()
                except Exception:
                    raw_json = None

        elif _BS4_AVAILABLE:
            soup = BeautifulSoup(html, "html.parser")
            script = soup.find("script", id="__NEXT_DATA__")

            if script is not None:
                raw_json = script.string or script.get_text()

        if not raw_json:
            return []

        try:
            payload = json.loads(raw_json)
        except Exception:
            return []

        title_candidates: Dict[str, List[str]] = {}
        order: Dict[str, int] = {}
        counter = 0

        def add(href: str, title: Optional[str]) -> None:
            nonlocal counter

            full_url = self._valid_chapter_url(href, page_url)

            if not full_url:
                return

            if full_url not in order:
                order[full_url] = counter
                counter += 1
                title_candidates[full_url] = []

            if title:
                title_candidates[full_url].append(title)

        def walk(obj) -> None:
            if isinstance(obj, dict):
                href: Optional[str] = None

                for key in (
                    "href",
                    "url",
                    "path",
                    "link",
                    "chapterUrl",
                    "chapter_url",
                    "slug",
                ):
                    value = obj.get(key)

                    if isinstance(value, str) and _CHAPTER_NUM_RE.search(value):
                        href = value
                        break

                if href:
                    title: Optional[str] = None

                    for key in (
                        "title",
                        "name",
                        "chapterTitle",
                        "chapter_title",
                    ):
                        value = obj.get(key)

                        if isinstance(value, str) and value.strip():
                            title = value
                            break

                    add(href, title)

                for value in obj.values():
                    walk(value)

            elif isinstance(obj, list):
                for item in obj:
                    walk(item)

        walk(payload)

        refs: List[ChapterRef] = []

        for url, _index in sorted(order.items(), key=lambda item: item[1]):
            chapter_num = _chapter_number(url)
            title = self._choose_best_title(
                title_candidates.get(url, []),
                chapter_num,
                len(refs) + 1,
            )

            refs.append(
                ChapterRef(
                    index=len(refs),
                    title=title,
                    source_url=url,
                )
            )

        return refs

    # ------------------------------------------------------------------
    # URL/title helpers
    # ------------------------------------------------------------------
    def _valid_chapter_url(self, href: str, page_url: str) -> Optional[str]:
        """
        Validate that a URL is a chapter URL belonging to the same novel.
        """
        if not href:
            return None

        full_url = urljoin(_BASE, href)

        parsed = urlparse(full_url)

        if parsed.netloc and parsed.netloc != urlparse(_BASE).netloc:
            return None

        if not _CHAPTER_NUM_RE.search(full_url):
            return None

        novel_path = urlparse(page_url).path.rstrip("/")
        chapter_path = parsed.path.rstrip("/")

        if novel_path and not chapter_path.startswith(novel_path):
            return None

        return full_url

    def _choose_best_title(
        self,
        candidates: List[str],
        chapter_num: Optional[int],
        fallback_index: int,
    ) -> str:
        """
        Choose the best chapter title from one or more text candidates.

        WTR-Lab may render multiple anchors for the same chapter row.
        This avoids choosing "#" or "56" as the title.
        """
        preferred: List[str] = []
        acceptable: List[str] = []

        for raw in candidates:
            text = re.sub(r"\s+", " ", raw or "").strip()

            if not text:
                continue

            if text == "#":
                continue

            if re.fullmatch(r"#?\s*\d+", text):
                continue

            text = re.sub(r"^#\s*", "", text).strip()
            text = re.sub(r"^\d+\s+", "", text).strip()

            if not text:
                continue

            if re.search(r"chapter", text, re.IGNORECASE):
                if re.fullmatch(r"Chapter\s*\d+", text, re.IGNORECASE):
                    acceptable.append(text)
                else:
                    preferred.append(text)
            else:
                acceptable.append(text)

        if preferred:
            return max(preferred, key=len)

        if acceptable:
            return max(acceptable, key=len)

        if chapter_num is not None:
            return f"Chapter {chapter_num}"

        return f"Chapter {fallback_index}"

    # ------------------------------------------------------------------
    # Latest-widget guards
    # ------------------------------------------------------------------
    def _attr(self, node, name: str) -> str:
        try:
            return node.attributes.get(name) or ""
        except Exception:
            return ""

    def _has_latest_class_selectolax(self, node) -> bool:
        class_value = self._attr(node, "class")

        return any(token in class_value for token in _LATEST_CLASS_TOKENS)

    def _is_inside_latest_selectolax(self, node) -> bool:
        current = node

        for _ in range(10):
            if current is None:
                return False

            if self._has_latest_class_selectolax(current):
                return True

            try:
                current = current.parent
            except Exception:
                return False

        return False

    def _has_latest_class_bs4(self, tag) -> bool:
        try:
            class_value = " ".join(tag.get("class", []))
        except Exception:
            class_value = ""

        return any(token in class_value for token in _LATEST_CLASS_TOKENS)

    def _is_inside_latest_bs4(self, tag) -> bool:
        current = tag

        while current is not None:
            if self._has_latest_class_bs4(current):
                return True

            current = getattr(current, "parent", None)

        return False

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------
    # NOTE: not currently called by get_chapter_list(). The whole
    # chapter list on wtr-lab.com lives under a single ?tab=toc URL,
    # split into in-page accordion sections rather than separate pages,
    # so there is no "next page" link to follow. Kept here (unused) in
    # case the site reintroduces real multi-page pagination, or for
    # reuse by other adapters.
    def _find_next_toc_page_auto(self, current_url: str, html: str) -> Optional[str]:
        """
        Find the next TOC page.

        Priority:
        1. Explicit selector from config/sites.json.
        2. Visible anchor containing "next" in text, aria-label, title, or rel.
        """
        next_selector = _SEL.get("chapter_list_next_page")

        candidate: Optional[str] = None

        if next_selector:
            if _SELECTOLAX_AVAILABLE:
                tree = HTMLParser(html)
                el = tree.css_first(next_selector)

                if el is not None:
                    href = self._attr(el, "href")

                    if href:
                        candidate = urljoin(_BASE, href)

            elif _BS4_AVAILABLE:
                soup = BeautifulSoup(html, "html.parser")
                el = soup.select_one(next_selector)

                if el is not None and el.get("href"):
                    candidate = urljoin(_BASE, el.get("href"))

            if candidate and self._is_acceptable_pagination(candidate, current_url):
                return _with_tab(candidate, "toc")

        if _SELECTOLAX_AVAILABLE:
            tree = HTMLParser(html)

            try:
                anchors = tree.css("a[href]")
            except Exception:
                anchors = []

            for a in anchors:
                text = (a.text(strip=True) or "").lower()
                aria = self._attr(a, "aria-label").lower()
                title = self._attr(a, "title").lower()
                rel = self._attr(a, "rel").lower()

                if (
                    "next" in text
                    or "next" in aria
                    or "next" in title
                    or "next" in rel
                ):
                    href = self._attr(a, "href")

                    if not href or href.startswith("javascript:"):
                        continue

                    candidate = urljoin(_BASE, href)

                    if self._is_acceptable_pagination(candidate, current_url):
                        return _with_tab(candidate, "toc")

        elif _BS4_AVAILABLE:
            soup = BeautifulSoup(html, "html.parser")

            for a in soup.find_all("a", href=True):
                text = a.get_text(" ", strip=True).lower()
                aria = (a.get("aria-label") or "").lower()
                title = (a.get("title") or "").lower()

                rel_value = a.get("rel") or []

                if isinstance(rel_value, str):
                    rel = rel_value.lower()
                else:
                    rel = " ".join(rel_value).lower()

                if (
                    "next" in text
                    or "next" in aria
                    or "next" in title
                    or "next" in rel
                ):
                    candidate = urljoin(_BASE, a["href"])

                    if self._is_acceptable_pagination(candidate, current_url):
                        return _with_tab(candidate, "toc")

        return None

    def _is_acceptable_pagination(self, candidate_url: str, current_url: str) -> bool:
        """
        Ensure a pagination URL belongs to the same novel and is not a
        chapter link.
        """
        parsed = urlparse(candidate_url)

        if parsed.netloc and parsed.netloc != urlparse(_BASE).netloc:
            return False

        if not parsed.path:
            return False

        if _CHAPTER_NUM_RE.search(candidate_url):
            return False

        current_path = urlparse(current_url).path.rstrip("/")
        candidate_path = parsed.path.rstrip("/")

        if not current_path:
            return True

        return (
            candidate_path == current_path
            or candidate_path.startswith(current_path + "/")
        )

    # ------------------------------------------------------------------
    # Chapter body
    # ------------------------------------------------------------------
    async def get_chapter_text(self, chapter_url: str) -> str:
        """
        Extract chapter body text.
        """
        expected_novel_id = _novel_id_from_url(chapter_url)
        expected_chapter = _chapter_number(chapter_url)

        if _PLAYWRIGHT_AVAILABLE:
            html = await self._fetch_rendered_chapter_html(
                chapter_url,
                expected_novel_id,
                expected_chapter,
            )
        else:
            html = await fetch_html(chapter_url, _SITE_ID)

        configured = _split_selectors(_SEL.get("chapter_text", ""))

        selectors = configured or [
            ".chapter-content",
            ".story-part",
            "#chapter-container",
            "article",
            "main",
        ]

        text = parse_chapter_body(html, selectors)

        if text:
            return text

        if _SELECTOLAX_AVAILABLE:
            tree = HTMLParser(html)

            candidates = tree.css(
                "article, main, [class*='chapter'], [class*='content'], [id*='chapter']"
            )

            best_text = ""
            best_len = 0

            for el in candidates:
                paragraphs = [p.text(strip=True) for p in el.css("p")]
                joined = "\n\n".join(p for p in paragraphs if p)

                if len(joined) > best_len:
                    best_text = joined
                    best_len = len(joined)

            if best_text:
                return best_text

        return (
            parse_text(
                html,
                [
                    ".chapter-content",
                    ".story-part",
                    "article",
                    "main",
                ],
            )
            or ""
        )

    async def _fetch_rendered_chapter_html(
        self,
        chapter_url: str,
        expected_novel_id: Optional[str],
        expected_chapter: Optional[int],
    ) -> str:
        """Load a chapter and reject silent redirects to another novel."""
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(chapter_url, wait_until="networkidle")

                actual_novel_id = _novel_id_from_url(page.url)
                actual_chapter = _chapter_number(page.url)
                if expected_novel_id and actual_novel_id != expected_novel_id:
                    raise RuntimeError(
                        f"WTR-Lab redirected chapter to another novel: "
                        f"expected {expected_novel_id}, got {actual_novel_id}"
                    )
                if expected_chapter and actual_chapter != expected_chapter:
                    raise RuntimeError(
                        f"WTR-Lab redirected to another chapter: "
                        f"expected {expected_chapter}, got {actual_chapter}"
                    )

                try:
                    await page.wait_for_selector(
                        ".chapter-content, .story-part, #chapter-container, article",
                        timeout=15_000,
                    )
                except Exception:
                    pass
                return await page.content()
            finally:
                await browser.close()