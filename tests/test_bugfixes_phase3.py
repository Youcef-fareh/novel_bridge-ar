"""Regression coverage for the remaining Phase 3 correctness fixes."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_relative_config_paths_are_independent_of_working_directory(tmp_path, monkeypatch):
    from backend.config import Config, base_dir, resolve_path

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"paths": {"db": "data/test.db", "output": "books", "data_dir": "data"}}),
        encoding="utf-8",
    )
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    explicit_env = {
        "CONFIG_PATH": str(config_path),
        "DB_PATH": "data/test.db",
        "OUTPUT_DIR": "books",
        "DATA_DIR": "data",
    }
    first = Config.from_env(explicit_env)
    first_sites_path = resolve_path("config/sites.json")
    monkeypatch.chdir(second_cwd)
    second = Config.from_env(explicit_env)

    assert first.paths == second.paths
    assert first.paths.db == (base_dir() / "data" / "test.db").resolve()
    assert first.paths.output == (base_dir() / "books").resolve()
    assert first_sites_path == resolve_path("config/sites.json")


def test_config_cache_refreshes_for_config_mtime_and_environment(tmp_path, monkeypatch):
    from backend import config

    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"paths": {"output": "configured-output"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "dotenv_values", lambda *_args, **_kwargs: {})
    monkeypatch.setenv("CONFIG_PATH", str(path))
    monkeypatch.delenv("OUTPUT_DIR", raising=False)

    first = config.get_config().paths.output
    monkeypatch.setenv("OUTPUT_DIR", "env-output")
    from_env = config.get_config().paths.output
    monkeypatch.delenv("OUTPUT_DIR")
    path.write_text(
        json.dumps({"paths": {"output": "updated-output"}}),
        encoding="utf-8",
    )
    updated = config.get_config().paths.output

    assert first == config.resolve_path("configured-output")
    assert from_env == config.resolve_path("env-output")
    assert updated == config.resolve_path("updated-output")


def test_pipeline_semaphore_refreshes_when_concurrency_changes(monkeypatch):
    import backend.pipeline as pipeline

    configured_limit = [1]
    monkeypatch.setattr(
        pipeline,
        "_max_concurrent_jobs",
        lambda: configured_limit[0],
    )

    async def make_semaphores():
        first = pipeline._get_semaphore()
        configured_limit[0] = 2
        second = pipeline._get_semaphore()
        return first, second

    first, second = asyncio.run(make_semaphores())

    assert first is not second
    assert first._value == 1
    assert second._value == 2


def test_bad_html_cleaner_removes_disallowed_tags_and_ads():
    from backend.adapters.soup_template import _strip_bad_html

    cleaned = _strip_bad_html(
        '<article><p>Keep this text</p><script>bad()</script>'
        '<nav>menu</nav><div class="ads">advert</div></article>'
    )

    assert "Keep this text" in cleaned
    assert "bad()" not in cleaned
    assert "menu" not in cleaned
    assert "advert" not in cleaned


def test_novelfire_host_matching_rejects_lookalike_urls():
    from backend.adapters.novelfire import NovelFireAdapter

    adapter = NovelFireAdapter()
    assert adapter.can_handle("https://novelfire.net/book/one")
    assert adapter.can_handle("https://sub.novelfire.com/book/one")
    assert not adapter.can_handle("https://evil.example/novelfire.net/book/one")
    assert not adapter.can_handle("https://novelfire.docs/book/one")


def test_translation_completion_reports_unavailable_requested_chapter(monkeypatch):
    import backend.pipeline as pipeline

    chapter = SimpleNamespace(
        id=7,
        index=0,
        title="Already translated",
        raw_text="source",
        translated_text="translation",
        translated_hash=None,
        status=pipeline.ChapterStatus.translated,
    )
    novel = SimpleNamespace(id=3, source_url="https://example.org/book")
    created_job = SimpleNamespace(id=23)
    captured: dict = {}
    monkeypatch.setattr(pipeline, "get_novel", lambda _novel_id: novel)
    monkeypatch.setattr(pipeline.AdapterRegistry, "find", lambda _url: None)
    monkeypatch.setattr(
        pipeline,
        "get_provider",
        lambda *_args, **_kwargs: SimpleNamespace(provider_name="test"),
    )
    monkeypatch.setattr(pipeline, "get_all_glossary_rules", lambda: [])
    monkeypatch.setattr(pipeline, "get_novel_glossary", lambda *_args: {})
    monkeypatch.setattr(pipeline, "get_pending_translation_chapters", lambda _id: [])
    monkeypatch.setattr(pipeline, "get_chapters", lambda _id: [chapter])
    monkeypatch.setattr(pipeline, "create_job", lambda *_args, **_kwargs: created_job)
    monkeypatch.setattr(
        pipeline,
        "update_job",
        lambda _job_id, **kwargs: captured.update(kwargs),
    )
    monkeypatch.setattr(
        pipeline,
        "get_job_obj",
        lambda _job_id: SimpleNamespace(current_message=captured["current_message"]),
    )

    result = asyncio.run(
        pipeline.run_translation_job(novel.id, chapter_ids=[999])
    )

    assert "999 (chapter does not belong to this novel)" in result.current_message


def test_epub_job_uses_configured_output_directory(tmp_path, monkeypatch):
    import backend.binder as binder
    import backend.pipeline as pipeline

    novel = SimpleNamespace(id=4, source_site="test", title="Test book")
    chapter = SimpleNamespace(
        index=0,
        volume_serial=None,
        translated_text="Translated content",
        raw_text="Source content",
    )
    job = SimpleNamespace(id=24)
    captured: dict = {}
    artifact = tmp_path / "book.epub"

    def fake_build(_novel, _chapters, _format, output_dir, **_kwargs):
        captured["output_dir"] = output_dir
        artifact.write_bytes(b"book")
        return artifact

    monkeypatch.setattr(pipeline, "get_novel", lambda _novel_id: novel)
    monkeypatch.setattr(pipeline, "get_chapters", lambda _novel_id: [chapter])
    monkeypatch.setattr(pipeline, "get_job", lambda _job_id: None)
    monkeypatch.setattr(pipeline, "create_job", lambda *_args, **_kwargs: job)
    monkeypatch.setattr(pipeline, "update_job", lambda _job_id, **kwargs: captured.update(kwargs))
    monkeypatch.setattr(pipeline, "get_job_obj", lambda _job_id: job)
    monkeypatch.setattr(pipeline, "output_path", lambda: tmp_path)
    monkeypatch.setattr(pipeline, "load_novel_chapter_images", lambda _novel_id: {})
    monkeypatch.setattr(binder, "make_artifact", fake_build)

    _result_job, result_path = asyncio.run(pipeline.run_epub_job(novel.id))

    assert captured["output_dir"] == tmp_path
    assert captured["output_path"] == str(artifact)
    assert result_path == artifact


def test_epub_builder_uses_configured_output_without_shadowing_helper(tmp_path, monkeypatch):
    from backend import epub_builder
    from backend.models import Chapter, ChapterStatus, Novel

    monkeypatch.setattr(epub_builder, "output_path", lambda: tmp_path)
    novel = Novel(
        id=41,
        title="Configured output",
        source_url="https://example.org/book",
        source_site="test",
    )
    chapter = Chapter(
        id=42,
        novel_id=41,
        index=0,
        title="Chapter 1",
        source_url="https://example.org/chapter",
        translated_text="Translated content.",
        status=ChapterStatus.translated,
    )

    output = epub_builder.build_epub(novel, [chapter])

    assert output.parent == tmp_path
    assert output.is_file()


@pytest.mark.parametrize("selectolax_on", [False, True])
def test_meta_selectors_and_relative_links_work_in_both_parsers(
    selectolax_on, monkeypatch
):
    from backend.adapters import fetcher

    if selectolax_on and not fetcher._SELECTOLAX_AVAILABLE:
        pytest.skip("selectolax is not installed")
    monkeypatch.setattr(fetcher, "_SELECTOLAX_AVAILABLE", selectolax_on)
    html = (
        '<meta property="og:title" content="Open Graph title">'
        '<meta name="author" content="Novel author">'
        '<meta property="og:description" content="Novel description">'
        '<a href="chapter-1.html">Relative</a>'
        '<a href="../x/c2.html">Parent</a>'
        '<a href="/abs">Root</a>'
        '<a href="#section">Anchor</a>'
        '<a href="https://example.org/chapter">Absolute</a>'
    )

    assert fetcher.parse_text(html, ['meta[property="og:title"]']) == "Open Graph title"
    assert fetcher.parse_text(html, ['meta[name="author"]']) == "Novel author"
    assert fetcher.parse_text(html, ['meta[property="og:description"]']) == "Novel description"
    assert fetcher.parse_links(
        html, ["a"], base_url="https://example.org/novel/index.html"
    ) == [
        ("Relative", "https://example.org/novel/chapter-1.html"),
        ("Parent", "https://example.org/x/c2.html"),
        ("Root", "https://example.org/abs"),
        ("Absolute", "https://example.org/chapter"),
    ]


def test_chapter_image_url_is_revalidated_after_redirect(monkeypatch):
    from backend import chapter_images

    checked_urls: list[str] = []
    monkeypatch.setattr(
        chapter_images,
        "_assert_public_url",
        lambda url: checked_urls.append(url),
    )

    class FakeResponse:
        def __init__(self, url):
            self.is_redirect = url.endswith("/start")
            self.headers = (
                {"location": "/final"}
                if self.is_redirect
                else {"content-type": "image/png"}
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"\x89PNG\r\n\x1a\nimage"

    class FakeClient:
        def stream(self, _method, url, **_kwargs):
            return FakeResponse(url)

    image, media_type = asyncio.run(
        chapter_images._download_image(
            FakeClient(), "https://images.example/start"
        )
    )

    assert checked_urls == [
        "https://images.example/start",
        "https://images.example/final",
    ]
    assert media_type == "image/png"
    assert image.startswith(b"\x89PNG")


def test_soup_adapter_ignores_challenge_markers_in_scripts():
    from backend.adapters.soup_template import SoupAdapter

    adapter = SoupAdapter()

    assert adapter.check_response(
        '<script src="/captcha/enable-javascript.js">checking your browser</script>'
        "<title>Chapter</title><p>Story</p>"
    ) is None
    assert adapter.check_response("<title>Just a moment</title>captcha") == "challenge"


def test_chapter_list_predicate_excludes_toc_and_pagination():
    from backend.adapters.novelfire import _is_chapter_href
    from backend.adapters.novelphoenix import _is_chapter_href as phoenix_chapter_href

    for predicate in (_is_chapter_href, phoenix_chapter_href):
        assert predicate("https://novel.test/novel/chapter-123")
        assert not predicate("https://novel.test/novel/chapters?page=2")
        assert not predicate("https://novel.test/novel/table-of-contents")


def test_galaxynovels_author_extraction_keeps_arabic_name_letters(monkeypatch):
    from backend.adapters import galaxynovels

    html = (
        '<h1>Novel title</h1>'
        '<meta name="description" content="المؤلف علي بن محمد على رواية جديدة">'
        '<div class="description">A novel description.</div>'
    )

    async def fake_fetch(_url, _wait_for=""):
        return html

    monkeypatch.setattr(galaxynovels, "_fetch_galaxy_html", fake_fetch)
    metadata = asyncio.run(
        galaxynovels.GalaxyNovelsAdapter().get_novel_metadata(
            "https://galaxynovels.com/novel/one"
        )
    )

    assert metadata.author == "علي بن محمد"
