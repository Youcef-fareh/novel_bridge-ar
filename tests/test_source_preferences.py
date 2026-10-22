from __future__ import annotations

import json

import pytest


def test_source_preferences_persist_and_validate(tmp_path, monkeypatch):
    from backend.adapters import preferences

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")

    saved = preferences.save_preferences(
        search_limit=35,
        source_language="en-ar",
        category="webnovel",
    )

    assert saved["search_limit"] == 35
    assert preferences.get_preferences()["source_language"] == "en-ar"
    assert json.loads((tmp_path / "preferences.json").read_text(encoding="utf-8"))[
        "category"
    ] == "webnovel"

    preferences.set_adapter_enabled("example", False)
    assert not preferences.adapter_is_enabled("example")
    assert preferences.adapter_is_enabled("other")

    with pytest.raises(ValueError, match="between 1 and 50"):
        preferences.save_preferences(search_limit=51)


def test_source_search_applies_language_category_and_enabled_filters(tmp_path, monkeypatch):
    import asyncio
    from backend import main
    from backend.adapters import preferences
    from backend.adapters.base import SearchResult

    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")

    class SearchAdapter:
        can_search = True
        source_language = "English"
        category = "webnovel"
        is_native_arabic = False
        base_url = "https://example.org"

        def __init__(self, site_id):
            self.site_id = site_id

        def can_handle(self, url):
            return url.startswith(self.base_url)

        async def search(self, query, limit=20):
            return [SearchResult(query, f"{self.base_url}/novel", self.site_id)]

    monkeypatch.setattr(
        main.AdapterRegistry,
        "_adapters",
        {"english": SearchAdapter("english"), "disabled": SearchAdapter("disabled")},
    )
    preferences.set_adapter_enabled("disabled", False)

    results = asyncio.run(
        main.search_sources(
            "sample", domain=None, limit=10, source_language="en-ar", category="webnovel"
        )
    )

    assert results["total"] == 1
    assert results["items"][0].source_site == "english"


def test_novel_cover_endpoint_proxies_validated_image(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from backend import main

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def download(_client, url):
        assert url == "https://covers.example/cover.png"
        return b"\x89PNG\r\n\x1a\nimage", "image/png"

    monkeypatch.setattr(
        main, "get_novel", lambda _novel_id: SimpleNamespace(
            cover_url="https://covers.example/cover.png"
        )
    )
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(main, "_download_image", download)

    response = asyncio.run(main.novel_cover(1))

    assert response.body.startswith(b"\x89PNG")
    assert response.media_type == "image/png"
    assert response.headers["cache-control"] == "private, max-age=3600"


def test_progressive_search_emits_fast_results_before_slow_source_finishes(monkeypatch):
    import asyncio
    from backend import main
    from backend.adapters.base import SearchResult

    class SearchAdapter:
        can_search = True
        source_language = "English"
        category = "webnovel"
        is_native_arabic = False
        base_url = "https://example.org"

        def __init__(self, site_id, delay):
            self.site_id = site_id
            self.delay = delay

        def can_handle(self, _url):
            return False

        async def search(self, query, limit=20):
            await asyncio.sleep(self.delay)
            return [SearchResult(query, f"https://example.org/{self.site_id}", self.site_id)]

    monkeypatch.setattr(
        main.AdapterRegistry,
        "_adapters",
        {
            "fast": SearchAdapter("fast", 0.01),
            "slow": SearchAdapter("slow", 0.5),
            "timeout": SearchAdapter("timeout", 0.4),
        },
    )
    result_events = []
    progress_events = []

    async def run():
        return await main.search_sources_progressive(
            "Sample",
            limit=3,
            on_results=lambda site, batch: result_events.append(
                (site, batch, len(progress_events))
            ),
            on_progress=progress_events.append,
            source_timeout=0.15,
        )

    result = asyncio.run(run())

    assert result_events[0][0] == "fast"
    assert result["failed"] == 2
    assert result["total"] == 1
    assert progress_events[0]["total"] == 3
    assert progress_events[-1]["completed"] == 3


def test_progressive_search_cancellation_keeps_received_results(monkeypatch):
    import asyncio
    import threading
    from backend import main
    from backend.adapters.base import SearchResult

    class SearchAdapter:
        can_search = True
        source_language = "English"
        category = "webnovel"
        is_native_arabic = False
        base_url = "https://example.org"

        def __init__(self, site_id, delay):
            self.site_id = site_id
            self.delay = delay

        def can_handle(self, _url):
            return False

        async def search(self, query, limit=20):
            await asyncio.sleep(self.delay)
            return [SearchResult(query, f"https://example.org/{self.site_id}", self.site_id)]

    monkeypatch.setattr(
        main.AdapterRegistry,
        "_adapters",
        {
            "fast": SearchAdapter("fast", 0.01),
            "slow": SearchAdapter("slow", 1),
            "slower": SearchAdapter("slower", 1),
        },
    )
    cancel_event = threading.Event()

    async def run():
        def receive(_site, _batch):
            cancel_event.set()

        return await main.search_sources_progressive(
            "Sample",
            limit=3,
            on_results=receive,
            cancel_event=cancel_event,
            source_timeout=2,
        )

    result = asyncio.run(run())

    assert result["cancelled"] is True
    assert result["total"] == 1
    assert result["items"][0].source_site == "fast"


def test_source_search_completion_serializes_metadata_not_search_result_objects():
    from backend.adapters.base import SearchResult
    from gui.web_bridge import _source_search_completion

    serialized = _source_search_completion({
        "query": "Sample",
        "items": [SearchResult("Sample", "https://example.org/novel", "example")],
        "total": 1,
        "failed": 2,
        "cancelled": False,
    })

    assert json.loads(serialized) == {
        "query": "Sample",
        "total": 1,
        "failed": 2,
        "cancelled": False,
    }
