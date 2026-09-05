"""Standalone nodriver fetch helper — always run as a subprocess, never imported.

Invocation:
    python nodriver_fetch.py <url> <wait_selector> <timeout_seconds> <output_html_file>

Writes the rendered HTML to <output_html_file> on success.
Exits with code 0 on success, non-zero on failure.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path


async def _run(url: str, wait_sel: str, timeout: int, out_path: str) -> None:
    import nodriver as uc  # noqa: PLC0415

    browser = await uc.start(headless=False, sandbox=False)
    try:
        tab = await browser.get(url)

        # Primary selector to poll for (first selector in the comma-separated list).
        sel = wait_sel.split(",")[0].strip() if wait_sel else ""

        for _ in range(timeout):
            await asyncio.sleep(1)
            try:
                if sel:
                    ready = await tab.evaluate(
                        f"document.querySelector({repr(sel)}) !== null"
                    )
                else:
                    ready = await tab.evaluate(
                        "document.readyState === 'complete'"
                    )
                if ready:
                    break
            except Exception:
                pass  # page may be mid-navigation; keep waiting

        html = await tab.get_content()
        Path(out_path).write_text(html, encoding="utf-8", errors="replace")
    finally:
        try:
            browser.stop()
        except Exception:
            pass


def main() -> None:
    if len(sys.argv) < 5:
        print(
            "Usage: nodriver_fetch.py <url> <wait_selector> <timeout> <out_file>",
            file=sys.stderr,
        )
        sys.exit(1)

    url, wait_sel, timeout_str, out_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    asyncio.run(_run(url, wait_sel, int(timeout_str), out_path))


if __name__ == "__main__":
    main()
