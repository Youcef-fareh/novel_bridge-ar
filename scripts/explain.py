"""
NovelBridge — Source & scraper explain CLI.

Ported from lncrawl/commands/dev/explain.py (MIT).
Prints actionable diagnosis for a URL:
  - Matched adapter, template type, tier, version
  - Configured selectors
  - Scraper state: declared vs learned interval, request counts, last status
  - Health tallies (empty_body, 403, 429, challenges)
  - Actionable advice/prose

Usage:
  python scripts/explain.py https://novelfire.net/book/example
  python scripts/explain.py https://ranovel.com/novel/example/ --probe
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.adapters import register_standard_adapters
from backend.adapters.base import AdapterRegistry
from backend.adapters.fetcher import fetch_html
from backend.adapters.health import health
from backend.adapters.scraper_state import pacer
from backend.adapters.soup_template import SoupAdapter


def explain_url(url: str, probe: bool = False) -> None:
    register_standard_adapters()

    parsed = urlparse(url)
    origin = parsed.netloc.lower()
    if not origin:
        print(f"Invalid URL: {url}")
        sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"NovelBridge Explain: {origin}")
    print(f"{'=' * 60}")
    print(f"Target URL   : {url}")

    # 1. Adapter resolution
    adapter = AdapterRegistry.find(url)
    if adapter:
        print(f"Site ID      : {adapter.site_id}")
        print(f"Adapter Class: {adapter.__class__.__name__}")
        is_soup = isinstance(adapter, SoupAdapter)
        print(f"Template     : {'SoupAdapter' if is_soup else 'Custom SiteAdapter'}")
        print(f"Tier         : {getattr(adapter, 'tier', 'legacy')}")
        print(f"Version      : {getattr(adapter, 'version', '1')}")
        print(f"Method       : {getattr(adapter, 'scraping_method', 'HTTP')}")

        if is_soup:
            print("\nSelectors:")
            print(f"  Title      : {adapter.novel_title_selectors}")
            print(f"  Author     : {adapter.novel_author_selectors}")
            print(f"  Cover      : {adapter.novel_cover_selectors}")
            print(f"  Description: {adapter.novel_description_selectors}")
            print(f"  ChapterList: {adapter.chapter_list_selectors}")
            print(f"  ChapterBody: {adapter.chapter_body_selectors}")
    else:
        print("Site ID      : [None] No registered adapter matches this URL.")

    # 2. Scraper State / Pacer
    profile = pacer.get_profile(origin)
    print("\nPacer & Scraper State:")
    print(f"  Learned Mean Delay : {profile.mean_interval:.2f}s")
    print(f"  Total Requests OK  : {profile.ok_count}")
    print(f"  Total Failures     : {profile.fail_count}")
    if profile.last_ok:
        print(f"  Last OK Timestamp  : {time.ctime(profile.last_ok)}")
    if getattr(profile, "clearance_at", 0):
        print(f"  Clearance Timestamp: {time.ctime(profile.clearance_at)}")

    # 3. Source Health Tally
    counts = health.reasons(origin)
    print("\nSource Health Tally:")
    if counts:
        for reason, cnt in sorted(counts.items()):
            samples = health.samples(origin, reason)
            sample_str = f" (e.g. {samples[0]})" if samples else ""
            print(f"  - {reason:<15}: {cnt} times{sample_str}")
    else:
        print("  - No negative signals recorded for this origin.")

    # 4. Optional Live Probe
    if probe:
        print("\nLive Probe (--probe requested):")
        start = time.perf_counter()
        try:
            html = asyncio.run(fetch_html(url, getattr(adapter, "site_id", "")))
            elapsed = time.perf_counter() - start
            print(f"  Fetch Success! Status 200, length: {len(html)} chars ({elapsed:.2f}s)")
            if is_soup:
                reason = adapter.check_response(html)
                if reason:
                    print(f"  [WARN] check_response flagged page as: '{reason}'")
                else:
                    print("  [OK] Page passed challenge and empty-body checks.")
        except Exception as e:
            elapsed = time.perf_counter() - start
            print(f"  [ERROR] Live fetch failed after {elapsed:.2f}s: {e}")

    # 5. Diagnostic Advice
    print("\nDiagnostic Advice:")
    fail_ratio = (profile.fail_count / max(1, profile.ok_count + profile.fail_count))
    if counts.get("challenge", 0) > 0 or "cloudflare" in str(counts).lower():
        print("  * Site employs Cloudflare/Bot-challenges. Use nodriver or stealth Playwright.")
    if counts.get("rate_429", 0) > 0 or profile.mean_interval > 5.0:
        print("  * Frequent rate limits (429) detected. Increase request delay in config/sites.json.")
    if counts.get("empty_body", 0) > 0:
        print("  * Empty body responses seen. Check if selectors changed or if dynamic JS rendering is needed.")
    if fail_ratio == 0 and not counts:
        print("  * Clean origin profile! No issues detected.")

    print(f"{'=' * 60}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="NovelBridge Source Explain CLI")
    parser.add_argument("url", help="Novel or chapter URL to inspect")
    parser.add_argument("--probe", action="store_true", help="Perform one live test fetch")
    args = parser.parse_args()

    explain_url(args.url, probe=args.probe)


if __name__ == "__main__":
    main()
