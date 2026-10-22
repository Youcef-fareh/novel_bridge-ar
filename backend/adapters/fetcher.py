"""Shared fetch and parsing helpers used by all site adapters."""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse
import subprocess
import weakref

import httpx
from bs4 import BeautifulSoup
from backend.adapters.scraper_state import DEFAULT_HEADERS
from backend.config import resolve_path

# Try importing curl_cffi; gracefully degrade if not installed
try:
    from curl_cffi.requests import AsyncSession as CurlSession
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False

try:
    from selectolax.parser import HTMLParser
    _SELECTOLAX_AVAILABLE = True
except ImportError:
    _SELECTOLAX_AVAILABLE = False

# Load site config once
_SITES_CONFIG: dict = {}
_cfg_path = resolve_path("config/sites.json")
if _cfg_path.exists():
    _SITES_CONFIG = json.loads(_cfg_path.read_text(encoding="utf-8"))


_HEADERS = DEFAULT_HEADERS


async def fetch_html(
    url: str,
    site_id: str = "",
    *,
    timeout: Optional[float] = None,
    max_attempts: Optional[int] = None,
) -> str:
    """
    Fetch a URL and return HTML text.
    Strategy: httpx first, then curl_cffi if available and site config says so.
    Pacing: uses per-origin pacer (scraper_state) to honour learned intervals.
    """
    from backend.adapters.scraper_state import scraper_state
    from urllib.parse import urlparse as _urlparse

    cfg = _SITES_CONFIG.get(site_id, {})
    use_curl = cfg.get("use_curl_cffi", True) and _CURL_CFFI_AVAILABLE
    delay_min = cfg.get("request_delay_min", 0.5)
    delay_max = cfg.get("request_delay_max", 2.0)
    from backend.config import get_config
    runtime_config = get_config()
    timeout = (
        timeout
        if timeout is not None
        else cfg.get("request_timeout", runtime_config.crawler.request_timeout)
    )
    attempts = (
        max_attempts
        if max_attempts is not None
        else cfg.get("max_attempts", runtime_config.crawler.max_attempts)
    )
    if attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    origin = _urlparse(url).netloc
    declared_interval = (delay_min + delay_max) / 2.0
    ctx = scraper_state.open(origin, declared_interval)

    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        await ctx.wait()
        status_code = 0
        try:
            if attempt == 0 or not use_curl:
                async with httpx.AsyncClient(
                    headers=_HEADERS,
                    follow_redirects=True,
                    timeout=timeout,
                ) as client:
                    response = await client.get(url)
                status_code = response.status_code
                body = response.text
            else:
                async with CurlSession(impersonate="chrome124") as session:
                    response = await session.get(url, headers=_HEADERS, timeout=timeout)
                status_code = response.status_code
                body = response.text
            if status_code == 200 and body.strip():
                ctx.learn(success=True, status_code=status_code)
                return body
            last_error = RuntimeError(
                f"Fetch returned HTTP {status_code} or an empty page for {url} "
                f"(attempt {attempt + 1}/{attempts})."
            )
        except Exception as exc:
            last_error = exc
        ctx.learn(success=False, status_code=status_code)
        retryable_status = status_code in (0, 200, 403, 429, 503) or status_code >= 500
        if attempt + 1 >= attempts or not retryable_status:
            break

    if last_error:
        raise last_error
    raise RuntimeError(f"Failed to fetch {url} after {attempts} attempts")


async def fetch_html_playwright(url: str, wait_for: str = "") -> str:
    """Render a JavaScript page in Chromium and return its final HTML."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "GalaxyNovels needs Playwright. Install dependencies, then run "
            "'playwright install chromium'."
        ) from exc

    if getattr(sys, "frozen", False):
        import playwright

        bundled_browsers = (
            Path(playwright.__file__).resolve().parent
            / "driver"
            / "package"
            / ".local-browsers"
        )
        if bundled_browsers.exists():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(bundled_browsers))

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--window-size=1280,800",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent=_HEADERS["User-Agent"],
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )
            # Hide the 'webdriver' property that bot-detection scripts check for.
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            if wait_for:
                try:
                    await page.wait_for_selector(wait_for, timeout=20_000)
                except Exception:
                    pass
            else:
                await page.wait_for_timeout(2_000)
            return await page.content()
        finally:
            await browser.close()


_nodriver_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()
_nodriver_shutdown_requested = threading.Event()
_nodriver_shutdown_file = (
    Path(tempfile.gettempdir())
    / f"novelbridge-nodriver-{os.getpid()}-{uuid.uuid4().hex}.stop"
)


def _cleanup_nodriver_shutdown_file() -> None:
    try:
        _nodriver_shutdown_file.unlink(missing_ok=True)
    except OSError:
        logging.getLogger(__name__).warning(
            "Could not remove nodriver shutdown signal %s",
            _nodriver_shutdown_file,
            exc_info=True,
        )


atexit.register(_cleanup_nodriver_shutdown_file)


def shutdown_nodriver_fetches() -> None:
    """Stop active and queued nodriver subprocess fetches during app shutdown."""
    _nodriver_shutdown_requested.set()
    try:
        _nodriver_shutdown_file.touch(exist_ok=True)
    except OSError:
        logging.getLogger(__name__).exception(
            "Could not signal nodriver subprocesses to stop"
        )


def _get_nodriver_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _nodriver_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _nodriver_locks[loop] = lock
    return lock


async def fetch_html_nodriver(
    url: str,
    wait_selector: str = "",
    timeout: int = 45,
    click_selector: str = "",
    return_final_url: bool = False,
) -> str | tuple[str, str]:
    """Fetch a page using nodriver (undetected Chrome) to bypass Cloudflare.

    **Runs nodriver in a fully isolated subprocess** so that Chrome's window
    system does not conflict with the Qt GUI process (which would cause a
    ``setHighDpiScaleFactorRoundingPolicy`` crash when opening a headful window
    inside the same process).

    Requests are serialized with a per-event-loop lock so multiple chapter
    scrapes do not launch conflicting headful browser instances simultaneously.

    The child process writes the rendered HTML to a temp file; this function
    reads it back and returns it.

    Args:
        url: The page URL.
        wait_selector: CSS selector to wait for before capturing HTML.
        timeout: Maximum seconds to wait for the selector. Default 45 s.
        click_selector: Optional selector for accordion triggers to click
            after the page is ready. Only elements whose visible text matches
            a chapter range are clicked.
        return_final_url: When True, return ``(html, final_url)`` where
            ``final_url`` is the URL the browser actually ended on after
            any redirects. Use this to detect silent redirects instead of
            trusting the input URL.

    Raises:
        ImportError: if ``nodriver`` is not installed.
        RuntimeError: if the subprocess fails or times out.
    """
    import os
    import tempfile

    # Fail fast with a clear message if nodriver is not installed.
    try:
        import nodriver as _nd  # noqa: F401
    except ImportError:
        raise ImportError(
            "nodriver is not installed. Run:  pip install nodriver"
        )

    if _nodriver_shutdown_requested.is_set():
        raise asyncio.CancelledError

    _script = Path(__file__).parent / "nodriver_fetch.py"

    fd, tmp_path = tempfile.mkstemp(suffix=".nodriver.html")
    os.close(fd)

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    async with _get_nodriver_lock():
        try:
            if _nodriver_shutdown_requested.is_set():
                raise asyncio.CancelledError
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(_script),
                url,
                wait_selector or "",
                str(timeout),
                tmp_path,
                click_selector or "",
                str(_nodriver_shutdown_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
            try:
                _, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=float(timeout) + 30,
                )
            except asyncio.TimeoutError:
                proc.kill()
                # Reap the killed child so it cannot linger as a zombie and
                # its pipes are closed instead of leaking until GC teardown.
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
                raise RuntimeError("nodriver subprocess timed out")

            if _nodriver_shutdown_requested.is_set():
                raise asyncio.CancelledError

            if proc.returncode != 0:
                err = stderr_bytes.decode("utf-8", errors="replace")[:2000]
                raise RuntimeError(f"nodriver subprocess failed: {err}")

            html = Path(tmp_path).read_text(encoding="utf-8", errors="replace")
            if not html:
                raise RuntimeError("nodriver returned empty HTML")
            if return_final_url:
                final_url_path = Path(tmp_path + ".final_url")
                try:
                    final_url = final_url_path.read_text(encoding="utf-8").strip()
                except OSError:
                    final_url = ""
                return html, final_url
            return html
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            try:
                os.unlink(tmp_path + ".final_url")
            except Exception:
                pass


def parse_text(html: str, css_selectors: list[str]) -> Optional[str]:
    """
    Try each CSS selector in order; return the first match's non-empty text.

    <meta> tags are handled specially: their useful data is in the ``content``
    attribute (``.text()`` is always empty for them), and a selector that
    matched but yielded nothing must NOT short-circuit — the next selector may
    point at real content (e.g. ``og:title`` present but empty, then ``h1``).
    """
    if _SELECTOLAX_AVAILABLE:
        tree = HTMLParser(html)
        for sel in css_selectors:
            node = tree.css_first(sel)
            if not node:
                continue
            value = node.text(strip=True)
            if not value and node.tag == "meta":
                value = (
                    node.attributes.get("content", "")
                    or node.attributes.get("value", "")
                ).strip()
            if value:
                return value
    else:
        soup = BeautifulSoup(html, "lxml")
        for sel in css_selectors:
            node = soup.select_one(sel)
            if not node:
                continue
            value = node.get_text(strip=True)
            if not value and node.name == "meta":
                value = (node.get("content") or node.get("value") or "").strip()
            if value:
                return value
    return None


def parse_attr(html: str, css_selectors: list[str], attr: str) -> Optional[str]:
    """Parse a specific attribute from the first matching selector."""
    if _SELECTOLAX_AVAILABLE:
        tree = HTMLParser(html)
        for sel in css_selectors:
            node = tree.css_first(sel)
            if node and node.attributes.get(attr):
                return node.attributes[attr]
    else:
        soup = BeautifulSoup(html, "lxml")
        for sel in css_selectors:
            node = soup.select_one(sel)
            if node and node.get(attr):
                return node[attr]
    return None


def parse_cover_url(
    html: str,
    css_selectors: list[str],
    base_url: str,
) -> Optional[str]:
    """Find a usable novel cover URL, including lazy-loaded image sources."""
    lazy_attributes = (
        "data-src",
        "data-original",
        "data-lazy-src",
        "data-srcset",
        "data-lazy-srcset",
        "srcset",
    )
    placeholder_markers = (
        "placeholder",
        "no-image",
        "no_image",
        "nocover",
        "no-cover",
        "blank.gif",
        "blank.png",
        "transparent.gif",
    )
    selectors = list(dict.fromkeys([
        *css_selectors,
        ".book-img img",
        ".novel-cover img",
        ".cover img",
        ".novel-info img",
        ".book-cover img",
        ".summary_image img",
        ".wp-post-image",
        'img[itemprop="image"]',
    ]))

    def normalize(value: str) -> Optional[str]:
        candidate = value.strip().split(",", 1)[0].split()[0] if value.strip() else ""
        if not candidate or candidate.lower().startswith(("data:", "blob:", "javascript:")):
            return None
        if any(marker in candidate.casefold() for marker in placeholder_markers):
            return None
        resolved = urljoin(base_url, candidate)
        if urlparse(resolved).scheme not in {"http", "https"}:
            return None
        return resolved

    if _SELECTOLAX_AVAILABLE:
        tree = HTMLParser(html)
        image_nodes = []
        for selector in selectors:
            for node in tree.css(selector):
                if node.tag == "img":
                    image_nodes.append(node)
                else:
                    image_nodes.extend(node.css("img"))

        for attribute in lazy_attributes:
            for node in image_nodes:
                value = node.attributes.get(attribute, "")
                if value:
                    cover_url = normalize(value)
                    if cover_url:
                        return cover_url

        for node in image_nodes:
            value = node.attributes.get("src", "")
            if value:
                cover_url = normalize(value)
                if cover_url:
                    return cover_url

        for selector in (
            'meta[property="og:image"]',
            'meta[property="og:image:url"]',
            'meta[name="twitter:image"]',
            'meta[name="twitter:image:src"]',
        ):
            for node in tree.css(selector):
                value = node.attributes.get("content", "")
                if value:
                    cover_url = normalize(value)
                    if cover_url:
                        return cover_url
    else:
        soup = BeautifulSoup(html, "lxml")
        image_nodes = []
        for selector in selectors:
            for node in soup.select(selector):
                if node.name == "img":
                    image_nodes.append(node)
                else:
                    image_nodes.extend(node.select("img"))

        for attribute in lazy_attributes:
            for node in image_nodes:
                value = node.get(attribute, "")
                if value:
                    cover_url = normalize(value)
                    if cover_url:
                        return cover_url

        for node in image_nodes:
            value = node.get("src", "")
            if value:
                cover_url = normalize(value)
                if cover_url:
                    return cover_url

        for selector in (
            'meta[property="og:image"]',
            'meta[property="og:image:url"]',
            'meta[name="twitter:image"]',
            'meta[name="twitter:image:src"]',
        ):
            for node in soup.select(selector):
                value = node.get("content", "")
                if value:
                    cover_url = normalize(value)
                    if cover_url:
                        return cover_url

    return None


def parse_links(html: str, css_selectors: list[str], base_url: str = "") -> list[tuple[str, str]]:
    """Return list of (text, href) for all <a> tags matching any selector.

    Hrefs are resolved against *base_url* with urljoin so relative links
    ("chapter-1.html", "../x/c2.html") become fetchable absolute URLs.
    """
    results: list[tuple[str, str]] = []

    def _resolve(href: str) -> str:
        if not base_url or href.startswith(("#", "javascript:", "mailto:")):
            return href
        return urljoin(base_url, href)

    if _SELECTOLAX_AVAILABLE:
        tree = HTMLParser(html)
        for sel in css_selectors:
            nodes = tree.css(sel)
            for node in nodes:
                href = node.attributes.get("href", "")
                text = node.text(strip=True)
                if href and not href.lstrip().startswith("#"):
                    results.append((text, _resolve(href)))
        if results:
            return results
    else:
        soup = BeautifulSoup(html, "lxml")
        for sel in css_selectors:
            nodes = soup.select(sel)
            for node in nodes:
                href = node.get("href", "")
                text = node.get_text(strip=True)
                if href and not href.lstrip().startswith("#"):
                    results.append((text, _resolve(href)))
        if results:
            return results
    return results


def parse_chapter_body(html: str, css_selectors: list[str]) -> str:
    """Extract chapter body text, preserving paragraph breaks."""
    if _SELECTOLAX_AVAILABLE:
        tree = HTMLParser(html)
        for sel in css_selectors:
            node = tree.css_first(sel)
            if node:
                # Replace <br> nodes with a private-use marker: selectolax's
                # text(strip=True) drops whitespace-only text nodes inserted by
                # replace_with("\n"), which used to glue words together
                # ("line one<br>line two" → "line oneline two").
                for br in node.css("br"):
                    marker = HTMLParser("<html><body><span>&#xe000;</span></body></html>").body.child
                    br.replace_with(marker)
                paragraphs = node.css("p")
                if paragraphs:
                    return "\n\n".join(
                        text
                        for p in paragraphs
                        if (text := p.text(strip=True).replace("\ue000", "\n").strip())
                    )
                return node.text(strip=True).replace("\ue000", "\n").strip()
    else:
        soup = BeautifulSoup(html, "lxml")
        for sel in css_selectors:
            node = soup.select_one(sel)
            if node:
                # Replace <br> with newline characters; unlike the selectolax
                # branch, bs4 keeps them inside text nodes — but each <br>
                # must be replaced before text extraction or words glue
                # together. Use get_text() (no per-node strip) so the
                # newlines survive.
                for br in node.find_all("br"):
                    br.replace_with("\n")
                paragraphs = node.find_all("p")
                if paragraphs:
                    return "\n\n".join(
                        text
                        for p in paragraphs
                        if (text := p.get_text().strip())
                    )
                return node.get_text().strip()
    return ""


def parse_chapter_image_urls(
    html: str,
    css_selectors: list[str],
    base_url: str,
    *,
    limit: int = 8,
) -> list[dict[str, str]]:
    """Extract ordered image URLs and alt text from the selected chapter body."""
    soup = BeautifulSoup(html, "lxml")
    body = None
    for selector in css_selectors:
        body = soup.select_one(selector)
        if body:
            break
    if body is None:
        return []
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in body.select("img"):
        candidate = ""
        for attribute in (
            "data-lazy-src", "data-original", "data-src", "data-lazy-srcset",
            "data-srcset", "srcset", "src",
        ):
            value = node.get(attribute)
            if value:
                candidate = str(value).split(",", 1)[0].split()[0]
                break
        if candidate.startswith(("data:", "blob:", "javascript:")):
            continue
        url = urljoin(base_url, candidate)
        path = urlparse(url).path.casefold()
        if (
            urlparse(url).scheme not in {"http", "https"}
            or any(marker in path for marker in ("placeholder", "no-image", "nocover", "blank.gif"))
            or url in seen
        ):
            continue
        seen.add(url)
        found.append({"url": url, "alt_text": str(node.get("alt", "")).strip()})
        if len(found) >= limit:
            break
    return found


async def post_html(url: str, data: dict, site_id: str = "") -> str:
    """POST *data* to *url* and return the response HTML (for ajax endpoints).

    Used by WordpressAdapter to hit /ajax/chapters/ and admin-ajax.php.
    Falls back to curl_cffi if available and httpx is blocked.
    """
    from backend.adapters.scraper_state import scraper_state
    from urllib.parse import urlparse as _urlparse

    cfg = _SITES_CONFIG.get(site_id, {})
    use_curl = cfg.get("use_curl_cffi", True) and _CURL_CFFI_AVAILABLE
    timeout = cfg.get("request_timeout", 30)
    origin = _urlparse(url).netloc
    ctx = scraper_state.open(origin)
    await ctx.wait()

    last_status = 0
    try:
        async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=timeout) as client:
            resp = await client.post(url, data=data)
            last_status = resp.status_code
            if resp.status_code == 200:
                ctx.learn(success=True)
                return resp.text
            if resp.status_code in (403, 429, 503) and use_curl:
                ctx.learn(success=False, status_code=resp.status_code)
                raise httpx.HTTPStatusError("Blocked", request=resp.request, response=resp)
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError):
        ctx.learn(success=False, status_code=last_status)
        if not use_curl:
            raise

    if use_curl and _CURL_CFFI_AVAILABLE:
        async with CurlSession(impersonate="chrome124") as session:
            resp = await session.post(url, data=data, headers=_HEADERS, timeout=timeout)
            resp.raise_for_status()
            ctx.learn(success=True)
            return resp.text

    ctx.learn(success=False)
    raise RuntimeError(f"POST failed for {url}")
