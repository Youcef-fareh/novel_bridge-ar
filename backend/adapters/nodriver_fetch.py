"""Standalone nodriver fetch helper — always run as a subprocess, never imported.

Invocation:
    python nodriver_fetch.py <url> <wait_selector> <timeout_seconds> <output_html_file> [click_selector]

Writes the rendered HTML to <output_html_file> on success.
Exits with code 0 on success, non-zero on failure.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path


async def _run(
    url: str,
    wait_sel: str,
    timeout: int,
    out_path: str,
    click_sel: str,
) -> None:
    import nodriver as uc  # noqa: PLC0415

    profile_dir = Path(__file__).resolve().parents[2] / "data" / "nodriver_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    browser = await uc.start(
        headless=False,
        sandbox=False,
        user_data_dir=profile_dir,
    )
    try:
        tab = await browser.get(url)

        selectors = [part.strip() for part in wait_sel.split(",") if part.strip()]
        challenge_script = """
            (() => {
                const text = (document.body?.innerText || '').toLowerCase();
                return text.includes('unusual reading activity') ||
                    text.includes('security challenge') ||
                    text.includes('loading security challenge') ||
                    text.includes('captcha') ||
                    text.includes('نشاط قراءة غير معتاد') ||
                    text.includes('تحدي الأمان') ||
                    text.includes('جاري تحميل تحدي الأمان');
            })()
        """

        for _ in range(timeout):
            await asyncio.sleep(1)
            try:
                if selectors:
                    ready = await tab.evaluate(
                        "Boolean(" + " || ".join(
                            f"document.querySelector({json.dumps(selector)})"
                            for selector in selectors
                        ) + ")",
                        return_by_value=True,
                    )
                else:
                    ready = await tab.evaluate(
                        "document.readyState === 'complete'",
                        return_by_value=True,
                    )
                challenge = await tab.evaluate(
                    challenge_script,
                    return_by_value=True,
                )
                if ready and not challenge:
                    break
            except Exception:
                pass  # page may be mid-navigation; keep waiting

        # WTR-Lab renders the page shell first and fills its content shortly
        # afterward. Let that second render settle before capturing HTML.
        await asyncio.sleep(5)

        if click_sel:
            await tab.evaluate(
                """
                (async (selector) => {
                    const range = /chapters?\\s+\\d+\\s*-\\s*\\d+/i;
                    const triggers = Array.from(document.querySelectorAll(selector))
                        .filter((element) => range.test(element.innerText || ''));

                    for (const trigger of triggers) {
                        if (trigger.getAttribute('aria-expanded') !== 'true') {
                            trigger.click();
                        }
                        await new Promise((resolve) => setTimeout(resolve, 500));
                    }
                })
                """ + f"({json.dumps(click_sel)})",
                await_promise=True,
                return_by_value=True,
            )

        try:
            html = await tab.get_content()
        except Exception as exc:
            # Chrome can close nodriver's websocket while a challenge or SPA
            # navigation is settling. Restart the isolated browser once and
            # capture the same URL again instead of losing the scrape.
            if "ConnectionClosed" not in type(exc).__name__:
                raise

            try:
                browser.stop()
            except Exception:
                pass
            await asyncio.sleep(1)
            browser = await uc.start(
                headless=False,
                sandbox=False,
                user_data_dir=profile_dir,
            )
            tab = await browser.get(url)
            await asyncio.sleep(5)
            html = await tab.get_content()
        lowered = html.lower()
        if (
            "unusual reading activity" in lowered
            or "loading security challenge" in lowered
            or "security challenge" in lowered
            or "نشاط قراءة غير معتاد" in lowered
            or "تحدي الأمان" in lowered
        ):
            raise RuntimeError(
                "WTR-Lab security challenge did not finish. "
                "Complete the challenge in the opened browser window, then retry."
            )
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
    click_sel = sys.argv[5] if len(sys.argv) > 5 else ""
    asyncio.run(_run(url, wait_sel, int(timeout_str), out_path, click_sel))


if __name__ == "__main__":
    main()
