"""Shared fetch and parsing helpers used by all site adapters."""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
import subprocess
import weakref

import httpx

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
    from bs4 import BeautifulSoup

# Load site config once
_SITES_CONFIG: dict = {}
_cfg_path = Path("config/sites.json")
if _cfg_path.exists():
    _SITES_CONFIG = json.loads(_cfg_path.read_text(encoding="utf-8"))


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


async def fetch_html(url: str, site_id: str = "") -> str:
    """
    Fetch a URL and return HTML text.
    Strategy: httpx first, then curl_cffi if available and site config says so.
    """
    cfg = _SITES_CONFIG.get(site_id, {})
    use_curl = cfg.get("use_curl_cffi", True) and _CURL_CFFI_AVAILABLE
    delay_min = cfg.get("request_delay_min", 0.5)
    delay_max = cfg.get("request_delay_max", 2.0)

    await asyncio.sleep(random.uniform(delay_min, delay_max))

    # 1. Try plain httpx
    try:
        async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=30) as client:
            resp = await client.get(url)
            if resp.status_code == 200 and len(resp.text) > 500:
                return resp.text
            if resp.status_code in (403, 429, 503) and use_curl:
                raise httpx.HTTPStatusError("Blocked", request=resp.request, response=resp)
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError):
        if not use_curl:
            raise

    # 2. Escalate to curl_cffi for Cloudflare bypass
    if use_curl and _CURL_CFFI_AVAILABLE:
        await asyncio.sleep(random.uniform(delay_min, delay_max))
        async with CurlSession(impersonate="chrome124") as session:
            resp = await session.get(url, headers=_HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.text

    raise RuntimeError(f"Failed to fetch {url}")


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


def _get_nodriver_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _nodriver_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _nodriver_locks[loop] = lock
    return lock


async def fetch_html_nodriver(url: str, wait_selector: str = "", timeout: int = 45) -> str:
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

    _script = Path(__file__).parent / "nodriver_fetch.py"

    fd, tmp_path = tempfile.mkstemp(suffix=".nodriver.html")
    os.close(fd)

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    async with _get_nodriver_lock():
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(_script),
                url,
                wait_selector or "",
                str(timeout),
                tmp_path,
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
                raise RuntimeError("nodriver subprocess timed out")

            if proc.returncode != 0:
                err = stderr_bytes.decode("utf-8", errors="replace")[:400]
                raise RuntimeError(f"nodriver subprocess failed: {err}")

            html = Path(tmp_path).read_text(encoding="utf-8", errors="replace")
            if not html:
                raise RuntimeError("nodriver returned empty HTML")
            return html
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def parse_text(html: str, css_selectors: list[str]) -> Optional[str]:
    """
    Try each CSS selector in order; return the first match's text content.
    """
    if _SELECTOLAX_AVAILABLE:
        tree = HTMLParser(html)
        for sel in css_selectors:
            node = tree.css_first(sel)
            if node:
                return node.text(strip=True)
    else:
        soup = BeautifulSoup(html, "lxml")
        for sel in css_selectors:
            node = soup.select_one(sel)
            if node:
                return node.get_text(strip=True)
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


def parse_links(html: str, css_selectors: list[str], base_url: str = "") -> list[tuple[str, str]]:
    """Return list of (text, href) for all <a> tags matching any selector."""
    results: list[tuple[str, str]] = []
    parsed_base = urlparse(base_url)
    scheme_host = f"{parsed_base.scheme}://{parsed_base.netloc}"

    if _SELECTOLAX_AVAILABLE:
        tree = HTMLParser(html)
        for sel in css_selectors:
            nodes = tree.css(sel)
            for node in nodes:
                href = node.attributes.get("href", "")
                text = node.text(strip=True)
                if href:
                    if href.startswith("/"):
                        href = scheme_host + href
                    results.append((text, href))
        if results:
            return results
    else:
        soup = BeautifulSoup(html, "lxml")
        for sel in css_selectors:
            nodes = soup.select(sel)
            for node in nodes:
                href = node.get("href", "")
                text = node.get_text(strip=True)
                if href:
                    if href.startswith("/"):
                        href = scheme_host + href
                    results.append((text, href))
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
                # Replace <br> and <p> with newlines
                for br in node.css("br"):
                    br.replace_with("\n")
                paragraphs = node.css("p")
                if paragraphs:
                    return "\n\n".join(p.text(strip=True) for p in paragraphs if p.text(strip=True))
                return node.text(strip=True)
    else:
        soup = BeautifulSoup(html, "lxml")
        for sel in css_selectors:
            node = soup.select_one(sel)
            if node:
                paragraphs = node.find_all("p")
                if paragraphs:
                    return "\n\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))
                return node.get_text(separator="\n", strip=True)
    return ""
