from __future__ import annotations

import importlib
import json
import zipfile
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_typed_config_rejects_invalid_runtime_limits():
    from backend.config import Config

    with pytest.raises(ValueError, match="MAX_CONCURRENT_JOBS"):
        Config.from_env({"MAX_CONCURRENT_JOBS": "0"})
    assert Config.from_env({"MAX_CONCURRENT_JOBS": "2"}).crawler.max_concurrent_jobs == 2


def test_config_redacts_provider_credentials(monkeypatch):
    from backend.config import Config

    monkeypatch.setenv("GEMINI_API_KEY", "example-secret-value")
    public = Config.from_env().to_public_dict()
    assert public["secrets"]["GEMINI_API_KEY"] == {
        "configured": True,
        "value": "***",
    }
    assert "example-secret-value" not in json.dumps(public)


def test_config_saves_and_reloads_non_secret_settings(tmp_path, monkeypatch):
    import backend.config as config_module
    Config = config_module.Config

    path = tmp_path / "app-config.json"
    monkeypatch.setenv("CONFIG_PATH", str(path))
    monkeypatch.setattr(config_module, "dotenv_values", lambda: {})
    config = Config.from_env({"MAX_CONCURRENT_JOBS": "4"})
    config.save()
    monkeypatch.delenv("MAX_CONCURRENT_JOBS", raising=False)
    assert Config.from_env().crawler.max_concurrent_jobs == 4
    assert "secrets" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("fmt", ["txt", "json"])
def test_native_artifacts_have_metadata_and_volume_contents(tmp_path, fmt):
    from backend.binder import make_artifact
    from backend.models import Chapter, ChapterStatus, Novel

    novel = Novel(id=12, title="Test / Novel", source_url="https://example.org/n", source_site="example")
    first = Chapter(
        id=1, novel_id=12, index=0, title="First", source_url="https://example.org/c1",
        raw_text="first chapter text", status=ChapterStatus.scraped, volume_serial=1,
    )
    second = Chapter(
        id=2, novel_id=12, index=100, title="Second", source_url="https://example.org/c2",
        raw_text="second chapter text", status=ChapterStatus.scraped, volume_serial=2,
    )

    output = make_artifact(
        novel, [first, second], fmt, tmp_path, volume=2, native_arabic=True
    )
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert any("meta." in name for name in names)
        if fmt == "txt":
            assert len([name for name in names if name.endswith(".txt") and "chapter_" in name]) == 1
            assert "second chapter text" in archive.read(
                "chapters/vol_002/chapter_00101.txt"
            ).decode("utf-8")
        else:
            chapters = json.loads(archive.read("chapters.json"))
            assert [chapter["title"] for chapter in chapters] == ["Second"]


def test_arabic_artifact_filenames_keep_arabic_title_and_unique_fallback():
    from backend.binder import _safe_title

    assert _safe_title("رواية النار", 1) == "رواية النار"
    assert _safe_title("عالم السحر", 2) == "عالم السحر"
    assert _safe_title("////", 17) == "novel_17"


def test_artifact_rejects_unimplemented_language(tmp_path):
    from backend.binder import make_artifact
    from backend.models import Chapter, ChapterStatus, Novel

    novel = Novel(id=1, title="Novel", source_url="https://example.org", source_site="example")
    chapter = Chapter(
        id=1,
        novel_id=1,
        index=0,
        title="Chapter",
        source_url="https://example.org/chapter",
        translated_text="نص",
        status=ChapterStatus.translated,
    )
    with pytest.raises(ValueError, match="Arabic artifacts"):
        make_artifact(novel, [chapter], "epub", tmp_path, language="en")


def test_request_validation_uses_uniform_error_envelope():
    from fastapi import FastAPI, Query
    from fastapi.testclient import TestClient

    from backend.api_errors import install_exception_handlers

    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/search")
    def search(query: str = Query(min_length=2)):
        return {"query": query}

    with TestClient(app) as client:
        response = client.get("/search", params={"query": "x"})
    assert response.status_code == 422
    assert response.json()["error"] == "validation_error"
    assert response.json()["detail"][0]["location"] == ["query", "query"]


def test_novel_glossary_storage_and_job_pagination(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "enhancements.db"))
    import backend.database as database

    database = importlib.reload(database)
    database.init_db()
    novel = database.create_novel("https://example.org/book", "example")
    database.set_novel_glossary_term(novel.id, "Dragon", "تنين")
    assert database.get_novel_glossary(novel.id) == {"Dragon": "تنين"}

    from backend.models import JobType

    first = database.enqueue_job(novel.id, JobType.scrape)
    database.enqueue_job(novel.id, JobType.translate)
    items, total = database.list_jobs(offset=0, limit=1)
    assert total == 2
    assert len(items) == 1
    assert database.replay_job(first.id).parent_job_id == first.id


@pytest.mark.asyncio
async def test_scrape_transient_failure_is_not_empty_attempt_and_fails_job(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "scrape-failure.db"))
    import backend.database as database

    database = importlib.reload(database)
    database.init_db()
    import backend.pipeline as pipeline

    pipeline = importlib.reload(pipeline)
    from backend.adapters.base import AdapterRegistry, ChapterRef, NovelMeta, SiteAdapter

    class TimeoutAdapter(SiteAdapter):
        site_id = "audit-timeout"
        calls = 0

        async def get_novel_metadata(self, novel_url):
            return NovelMeta(title="Audit", source_url=novel_url)

        async def get_chapter_list(self, novel_url):
            return [ChapterRef(0, "One", f"{novel_url}/chapter")]

        async def get_chapter_text(self, chapter_url):
            self.calls += 1
            raise TimeoutError("temporary timeout")

        def can_handle(self, url):
            return "audit-timeout.example" in url

    adapter = TimeoutAdapter()
    monkeypatch.setattr(AdapterRegistry, "_adapters", {adapter.site_id: adapter})
    novel = database.create_novel(
        "https://audit-timeout.example/book", adapter.site_id
    )

    first_job = await pipeline.run_scrape_job(novel.id)
    first_chapter = database.get_chapters(novel.id)[0]
    assert first_job.status.value == "failed"
    assert first_job.failed_items == 1
    assert first_chapter.empty_attempts == 0

    second_job = await pipeline.run_scrape_job(novel.id)
    assert adapter.calls == 2
    assert second_job.status.value == "failed"
    assert database.get_chapters(novel.id)[0].empty_attempts == 0


@pytest.mark.asyncio
async def test_empty_chapter_retry_cap_remains_visible_as_job_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "empty-chapter.db"))
    import backend.database as database

    database = importlib.reload(database)
    database.init_db()
    import backend.pipeline as pipeline

    pipeline = importlib.reload(pipeline)
    from backend.adapters.base import AdapterRegistry, ChapterRef, NovelMeta, SiteAdapter

    class EmptyAdapter(SiteAdapter):
        site_id = "audit-empty"
        calls = 0

        async def get_novel_metadata(self, novel_url):
            return NovelMeta(title="Audit", source_url=novel_url)

        async def get_chapter_list(self, novel_url):
            return [ChapterRef(0, "One", f"{novel_url}/chapter")]

        async def get_chapter_text(self, chapter_url):
            self.calls += 1
            return ""

        def can_handle(self, url):
            return "audit-empty.example" in url

    adapter = EmptyAdapter()
    monkeypatch.setattr(AdapterRegistry, "_adapters", {adapter.site_id: adapter})
    novel = database.create_novel("https://audit-empty.example/book", adapter.site_id)

    for _ in range(3):
        job = await pipeline.run_scrape_job(novel.id)
        assert job.status.value == "failed"
    exhausted_job = await pipeline.run_scrape_job(novel.id)

    chapter = database.get_chapters(novel.id)[0]
    assert adapter.calls == 3
    assert chapter.empty_attempts == 3
    assert exhausted_job.status.value == "failed"
    assert exhausted_job.failed_items == 1


def test_volume_index_is_persisted_by_scrape_order(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "volumes.db"))
    import backend.database as database

    database = importlib.reload(database)
    database.init_db()
    novel = database.create_novel("https://example.org/book", "example")
    for index in (0, 1, 100):
        database.upsert_chapter(novel.id, index, f"Chapter {index}", f"https://example.org/{index}")
    volumes = database.refresh_volumes(novel.id)
    assert [(volume.serial, volume.chapter_count) for volume in volumes] == [(1, 2), (2, 1)]


def test_chapter_source_url_change_resets_empty_retry_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "chapter-url.db"))
    import backend.database as database

    database = importlib.reload(database)
    database.init_db()
    novel = database.create_novel("https://example.org/book", "example")
    chapter = database.upsert_chapter(
        novel.id, 0, "Chapter", "https://example.org/chapter-old"
    )
    database.update_chapter(chapter.id, empty_attempts=3, raw_text="stale")

    updated = database.upsert_chapter(
        novel.id, 0, "Chapter", "https://example.org/chapter-new"
    )

    assert updated.source_url == "https://example.org/chapter-new"
    assert updated.empty_attempts == 0
    assert updated.raw_text is None
    assert updated.status.value == "pending"


def test_kolnovel_pdf_duplicates_are_repaired_and_scraped_chapters_kept(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "kolnovel-duplicates.db"))
    import backend.database as database
    from backend.models import ChapterStatus

    database = importlib.reload(database)
    database.init_db()
    novel = database.create_novel("https://kolnovel.com/novel", "lncrawl_ar_kolnovel")
    chapter_urls = [
        "https://kolnovel.com/book/chapter-1",
        "https://kolnovel.com/book/chapter-2",
        "https://kolnovel.com/book/chapter-3",
    ]
    for index, url in enumerate(chapter_urls):
        pdf = database.upsert_chapter(
            novel.id, index * 2, f"Chapter {index + 1} PDF", f"{url}/pdf"
        )
        database.update_chapter(pdf.id, status=ChapterStatus.failed)
        chapter = database.upsert_chapter(
            novel.id, index * 2 + 1, f"الفصل {index + 1}", url
        )
        database.update_chapter(
            chapter.id,
            raw_text=f"Arabic chapter {index + 1}",
            status=ChapterStatus.scraped,
        )

    removed = database.repair_kolnovel_pdf_chapter_duplicates(
        novel.id, chapter_urls
    )
    chapters = database.get_chapters(novel.id)

    assert removed == 3
    assert [chapter.index for chapter in chapters] == [0, 1, 2]
    assert [chapter.source_url for chapter in chapters] == chapter_urls
    assert [chapter.raw_text for chapter in chapters] == [
        "Arabic chapter 1",
        "Arabic chapter 2",
        "Arabic chapter 3",
    ]
    assert all(chapter.status == ChapterStatus.scraped for chapter in chapters)


def test_kolnovel_pdf_duplicate_repair_preserves_nonempty_pdf_rows(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "kolnovel-pdf-content.db"))
    import backend.database as database
    from backend.models import ChapterStatus

    database = importlib.reload(database)
    database.init_db()
    novel = database.create_novel("https://kolnovel.com/novel", "lncrawl_ar_kolnovel")
    chapter_url = "https://kolnovel.com/book/chapter-1"
    pdf = database.upsert_chapter(
        novel.id, 0, "Chapter PDF", f"{chapter_url}/pdf"
    )
    database.update_chapter(
        pdf.id, raw_text="PDF content", status=ChapterStatus.failed
    )
    chapter = database.upsert_chapter(
        novel.id, 1, "الفصل 1", chapter_url
    )
    database.update_chapter(
        chapter.id, raw_text="Arabic chapter", status=ChapterStatus.scraped
    )

    removed = database.repair_kolnovel_pdf_chapter_duplicates(
        novel.id, [chapter_url]
    )
    chapters = database.get_chapters(novel.id)

    assert removed == 0
    assert len(chapters) == 2
    assert chapters[0].source_url.endswith("/pdf")
    assert chapters[0].raw_text == "PDF content"
    assert chapters[1].raw_text == "Arabic chapter"


@pytest.mark.asyncio
async def test_fetch_honors_single_attempt_budget(monkeypatch):
    from types import SimpleNamespace

    from backend.adapters import fetcher
    from backend.adapters.scraper_state import scraper_state

    class Context:
        waits = 0

        async def wait(self):
            self.waits += 1

        def learn(self, success, status_code=200):
            return None

    context = Context()
    monkeypatch.setattr(scraper_state, "open", lambda *args: context)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url):
            return SimpleNamespace(status_code=503, text="temporarily unavailable")

    monkeypatch.setattr(fetcher.httpx, "AsyncClient", lambda **_kwargs: Client())
    with pytest.raises(RuntimeError, match="attempt 1/1"):
        await fetcher.fetch_html(
            "https://example.org/chapter",
            "not-configured",
            max_attempts=1,
        )
    assert context.waits == 1


@pytest.mark.asyncio
async def test_fetch_concurrency_is_capped_per_origin(monkeypatch):
    import asyncio

    from backend.pipeline import _get_origin_semaphore

    monkeypatch.setenv("MAX_CONCURRENT_PER_DOMAIN", "2")
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def request():
        nonlocal active, peak
        async with _get_origin_semaphore("example.org"):
            async with lock:
                active += 1
                peak = max(peak, active)
            await asyncio.sleep(0)
            async with lock:
                active -= 1

    await asyncio.gather(*(request() for _ in range(8)))
    assert peak == 2


def test_api_exposes_additive_jobs_search_and_artifact_routes():
    from backend.main import app

    paths = app.openapi()["paths"]
    assert "/jobs" in paths
    assert "/jobs/{job_id}/replay" in paths
    assert "/search" in paths
    assert "/novels/page" in paths
    assert "/novels/{novel_id}/glossary/suggestions" in paths
    assert "/chapters/{chapter_id}/image/{image_id}" in paths
    assert "/novels/{novel_id}/artifacts" in paths
    assert "/artifacts/{job_id}/download" in paths


def test_translation_prompt_marks_previous_tail_as_reference_only():
    from backend.translation.base import build_system_prompt

    prompt = build_system_prompt([], context="previous chapter ending")
    assert "do not translate or repeat it" in prompt
    assert "previous chapter ending" in prompt


@pytest.mark.asyncio
async def test_translation_context_and_hash_skip(tmp_path, monkeypatch):
    import hashlib

    monkeypatch.setenv("DB_PATH", str(tmp_path / "translation.db"))
    import backend.database as database
    database = importlib.reload(database)
    database.init_db()
    import backend.pipeline as pipeline
    pipeline = importlib.reload(pipeline)

    from backend.models import ChapterStatus

    novel = database.create_novel("https://example.org/book", "example")
    previous = database.upsert_chapter(novel.id, 0, "One", "https://example.org/1")
    next_chapter = database.upsert_chapter(novel.id, 1, "Two", "https://example.org/2")
    database.update_chapter(
        previous.id,
        raw_text="Original one",
        translated_text="المقطع السابق",
        translated_hash=None,
        status=ChapterStatus.translated,
    )
    database.update_chapter(
        next_chapter.id,
        raw_text="Original two",
        status=ChapterStatus.scraped,
    )

    class Provider:
        provider_name = "test"
        calls = []

        async def translate_chapter(self, text, glossary, model=None, context=None):
            self.calls.append(context)
            return "الترجمة"

    provider = Provider()
    monkeypatch.setattr(pipeline, "get_provider", lambda *args, **kwargs: provider)
    completed = await pipeline.run_translation_job(novel.id)
    assert completed.status.value == "completed"
    assert provider.calls == ["المقطع السابق"]
    translated = database.get_chapters(novel.id)[1]
    assert translated.translated_hash == hashlib.sha256(b"Original two").hexdigest()

    await pipeline.run_translation_job(novel.id)
    assert provider.calls == ["المقطع السابق"]


def test_explicit_source_volume_metadata_overrides_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "volume-metadata.db"))
    import backend.database as database

    database = importlib.reload(database)
    database.init_db()
    novel = database.create_novel("https://example.org/book", "example")
    database.upsert_chapter(
        novel.id, 0, "Volume Seven - Chapter 1", "https://example.org/c1",
        volume_serial=7, volume_title="The Seventh Volume",
    )
    database.upsert_chapter(novel.id, 1, "Chapter 2", "https://example.org/c2")

    volumes = database.refresh_volumes(novel.id)
    assert [(volume.serial, volume.title, volume.chapter_count) for volume in volumes] == [
        (1, "Volume 1", 1),
        (7, "The Seventh Volume", 1),
    ]
    assert [chapter.volume_serial for chapter in database.get_chapters(novel.id)] == [7, 1]


def test_database_novel_pagination_and_chapter_image_records(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "novel-page-images.db"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import backend.database as database

    database = importlib.reload(database)
    database.init_db()
    first = database.create_novel("https://example.org/one", "example", title="One")
    database.create_novel("https://example.org/two", "example", title="Two")
    page, total = database.get_novels_page(offset=0, limit=1)
    assert total == 2
    assert len(page) == 1

    chapter = database.upsert_chapter(
        first.id, 0, "Chapter", "https://example.org/chapter"
    )
    image_path = tmp_path / "chapter-images" / "image.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image-data")
    stored = database.replace_chapter_images(chapter.id, [{
        "image_id": "abcdef123456",
        "file_path": str(image_path),
        "media_type": "image/png",
        "alt_text": "Page",
        "size_bytes": 10,
    }])
    assert database.get_chapter_image(chapter.id, "abcdef123456").id == stored[0].id
    assert database.get_chapter_images(chapter.id)[0].alt_text == "Page"
    database.upsert_chapter(
        first.id, 0, "Updated", "https://example.org/chapter-updated"
    )
    assert database.get_chapter_images(chapter.id) == []
    assert not image_path.exists()


def test_glossary_suggestions_are_review_only_and_exclude_existing_terms():
    from backend.glossary_suggestions import suggest_glossary_terms
    from backend.models import Chapter, ChapterStatus

    chapters = [
        Chapter(
            id=index, novel_id=1, index=index, title=f"Chapter {index}",
            source_url=f"https://example.org/{index}",
            raw_text="Aria met the Silver Dragon. Aria returned.",
            status=ChapterStatus.scraped,
        )
        for index in range(3)
    ]
    suggestions = suggest_glossary_terms(chapters, existing_terms={"Aria"})
    by_term = {item["source_term"]: item for item in suggestions}
    assert "Aria" not in by_term
    assert by_term["Silver Dragon"]["target_term"] is None
    assert by_term["Silver Dragon"]["status"] == "needs_review"
    assert by_term["Silver Dragon"]["chapter_count"] == 3


def test_chapter_image_parser_extracts_lazy_image_sources():
    from backend.adapters.fetcher import parse_chapter_image_urls

    html = """
    <div class="chapter">
      <img data-src="/media/page-1.jpg" alt="Page one">
      <img src="data:image/gif;base64,abc">
      <img src="/media/page-2.png">
    </div>
    """
    assert parse_chapter_image_urls(html, [".chapter"], "https://example.org/book/ch1") == [
        {"url": "https://example.org/media/page-1.jpg", "alt_text": "Page one"},
        {"url": "https://example.org/media/page-2.png", "alt_text": ""},
    ]


def test_chapter_image_store_validates_and_persists_bounded_image(
    tmp_path, monkeypatch
):
    import backend.chapter_images as image_store

    image_data = b"\x89PNG\r\n\x1a\n" + b"fake-png"
    persisted = []

    class Response:
        is_redirect = False
        headers = {"content-type": "image/png"}

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield image_data

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        @asynccontextmanager
        async def stream(self, *_args, **_kwargs):
            yield Response()

    monkeypatch.setattr(image_store, "_assert_public_url", lambda _url: None)
    monkeypatch.setattr(image_store.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        image_store,
        "get_config",
        lambda: SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path)),
    )
    monkeypatch.setattr(image_store, "get_chapter_images", lambda _chapter_id: [])

    def replace(_chapter_id, records):
        persisted.extend(records)
        return [SimpleNamespace(**record) for record in records]

    monkeypatch.setattr(image_store, "replace_chapter_images", replace)
    stored = asyncio.run(image_store.store_chapter_images(
        2, 5, [{"url": "https://cdn.example.org/page.png", "alt_text": "Page"}]
    ))
    assert len(stored) == 1
    assert stored[0].media_type == "image/png"
    assert Path(stored[0].file_path).read_bytes() == image_data
    assert persisted[0]["size_bytes"] == len(image_data)


def test_chapter_image_url_rejects_private_network_targets():
    from backend.chapter_images import _assert_public_url

    with pytest.raises(ValueError, match="public IP"):
        _assert_public_url("http://127.0.0.1/private.png")


def test_epub_embeds_stored_chapter_images(tmp_path, monkeypatch):
    from backend import epub_builder
    from backend.models import Chapter, ChapterStatus, Novel

    data_dir = tmp_path / "data"
    image_path = data_dir / "chapter-images" / "1" / "2" / "image.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    monkeypatch.setattr(
        epub_builder,
        "get_config",
        lambda: SimpleNamespace(paths=SimpleNamespace(data_dir=data_dir)),
    )
    chapter = Chapter(
        id=2, novel_id=1, index=0, title="Chapter one",
        source_url="https://example.org/chapter",
        translated_text="النص المترجم", status=ChapterStatus.translated,
    )
    image = SimpleNamespace(
        file_path=str(image_path), media_type="image/png", alt_text="Illustration"
    )
    output = epub_builder.build_epub(
        Novel(id=1, title="Novel", source_url="https://example.org", source_site="example"),
        [chapter],
        tmp_path / "output",
        chapter_images={2: [image]},
    )
    with zipfile.ZipFile(output) as archive:
        assert "EPUB/images/chapter_2_0.png" in archive.namelist()
        chapter_html = archive.read("EPUB/text/chapter_0000.xhtml").decode("utf-8")
        assert 'alt="Illustration"' in chapter_html


def test_novelfire_search_returns_normalized_results(monkeypatch):
    import backend.adapters.novelfire as novelfire

    requested = []

    async def fake_fetch(url, site_id):
        requested.append((url, site_id))
        return """
        <div class="novel-item">
          <a href="/novel/storm"><img src="/covers/storm.jpg" alt="Storm"></a>
        </div>
        <div class="novel-item"><a href="https://attacker.example/book">Bad</a></div>
        """

    monkeypatch.setattr(novelfire, "fetch_html", fake_fetch)
    results = asyncio.run(novelfire.NovelFireAdapter().search("storm & sky", limit=3))
    assert requested[0][0] == "https://novelfire.net/search?keyword=storm+%26+sky"
    assert [(result.title, result.url, result.source_site) for result in results] == [
        ("Storm", "https://novelfire.net/novel/storm", "novelfire")
    ]
    assert results[0].cover_url == "https://novelfire.net/covers/storm.jpg"


def test_novel_page_and_search_api_contracts(monkeypatch):
    import backend.main as main
    from backend.adapters.base import SearchResult

    monkeypatch.setattr(
        main,
        "get_novels_page",
        lambda offset, limit: (
            [SimpleNamespace(
                id=8, title="Novel", source_url="https://example.org/book",
                source_site="example", author=None, cover_url=None,
                status=SimpleNamespace(value="scraped"),
                created_at=SimpleNamespace(isoformat=lambda: "2026-10-17T00:00:00+00:00"),
            )],
            4,
        ),
    )

    class SearchAdapter:
        site_id = "example"
        can_search = True

        async def search(self, query, limit=20):
            return [SearchResult("Found", "https://example.org/found", self.site_id)]

    monkeypatch.setattr(
        main.AdapterRegistry, "_adapters", {"example": SearchAdapter()}
    )
    page = asyncio.run(main.list_novels_page(offset=2, limit=1))
    results = asyncio.run(main.search_sources("found", domain="example", limit=1))
    assert (page["total"], page["offset"], page["limit"]) == (4, 2, 1)
    assert page["items"][0]["id"] == 8
    assert results["total"] == 1
    assert results["items"][0].url == "https://example.org/found"


@pytest.mark.asyncio
async def test_soup_adapter_reuses_chapter_html_for_image_extraction(monkeypatch):
    import backend.adapters.soup_template as soup_template

    class ImageAdapter(soup_template.SoupAdapter):
        site_id = "image-test"
        chapter_body_selectors = [".chapter"]

    html = (
        '<div class="chapter"><p>' + ("Long chapter text. " * 5)
        + '</p><img src="/page.png" alt="Page"></div>'
    )
    calls = []

    async def fake_fetch(url, site_id):
        calls.append((url, site_id))
        return html

    monkeypatch.setattr(soup_template, "fetch_html", fake_fetch)
    adapter = ImageAdapter()
    text = await adapter.get_chapter_text("https://example.org/chapter")
    images = await adapter.get_chapter_images("https://example.org/chapter")
    assert len(text) >= 50
    assert images == [{"url": "https://example.org/page.png", "alt_text": "Page"}]
    assert calls == [("https://example.org/chapter", "image-test")]
