"""Standalone nodriver fetch helper — always run as a subprocess, never imported.

Invocation:
    python nodriver_fetch.py <url> <wait_selector> <timeout_seconds> <output_html_file>
        [click_selector] [shutdown_signal_file]

Writes the rendered HTML to <output_html_file> on success.
Exits with code 0 on success, non-zero on failure.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import sys
from pathlib import Path


async def _stop_browser(browser) -> None:
    process = getattr(browser, "_process", None)
    stop = getattr(browser, "stop", None)
    if stop is not None:
        result = stop()
        if inspect.isawaitable(result):
            await result

    if process is None:
        return
    try:
        await asyncio.to_thread(process.wait, timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        await asyncio.to_thread(process.wait)


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
    browser = None
    try:
        browser = await uc.start(
            headless=False,
            sandbox=False,
            no_sandbox=True,
            user_data_dir=profile_dir,
        )
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

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, timeout)
        while loop.time() < deadline:
            await asyncio.sleep(min(0.25, max(0, deadline - loop.time())))
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

        await asyncio.sleep(1 if click_sel else 0.15)

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

            await _stop_browser(browser)
            await asyncio.sleep(1)
            browser = await uc.start(
                headless=False,
                sandbox=False,
                no_sandbox=True,
                user_data_dir=profile_dir,
            )
            tab = await browser.get(url)
            await asyncio.sleep(1)
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
        if browser is not None:
            await _stop_browser(browser)


async def _watch_for_shutdown(signal_path: str) -> None:
    while not Path(signal_path).exists():
        await asyncio.sleep(0.2)


async def _run_with_shutdown_signal(
    url: str,
    wait_sel: str,
    timeout: int,
    out_path: str,
    click_sel: str,
    shutdown_signal: str,
) -> None:
    fetch_task = asyncio.create_task(_run(url, wait_sel, timeout, out_path, click_sel))
    shutdown_task = asyncio.create_task(_watch_for_shutdown(shutdown_signal))
    done, _pending = await asyncio.wait(
        (fetch_task, shutdown_task),
        return_when=asyncio.FIRST_COMPLETED,
    )

    if shutdown_task in done:
        fetch_task.cancel()
        try:
            await fetch_task
        except asyncio.CancelledError:
            return
    else:
        shutdown_task.cancel()
        try:
            await shutdown_task
        except asyncio.CancelledError:
            pass
        await fetch_task


def main() -> None:
    if len(sys.argv) < 5:
        print(
            "Usage: nodriver_fetch.py <url> <wait_selector> <timeout> <out_file>",
            file=sys.stderr,
        )
        sys.exit(1)

    url, wait_sel, timeout_str, out_path = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    click_sel = sys.argv[5] if len(sys.argv) > 5 else ""
    shutdown_signal = sys.argv[6] if len(sys.argv) > 6 else ""
    if shutdown_signal:
        asyncio.run(
            _run_with_shutdown_signal(
                url,
                wait_sel,
                int(timeout_str),
                out_path,
                click_sel,
                shutdown_signal,
            )
        )
    else:
        asyncio.run(_run(url, wait_sel, int(timeout_str), out_path, click_sel))


if __name__ == "__main__":
    main()
