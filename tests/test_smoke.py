"""
NovelBridge — Smoke tests. Run with: pytest tests/
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, MagicMock


# ── Test: models importable ────────────────────────────────────────────────────
def test_models_import():
    from backend.models import Novel, Chapter, GlossaryRule, Job
    assert Novel
    assert Chapter
    assert GlossaryRule
    assert Job


def test_wuxiaspot_cover_extraction():
    from backend.adapters.wuxiaspot import WuxiaSpotAdapter

    html = '<meta property="og:image" content="/covers/example.jpg">'
    adapter = WuxiaSpotAdapter()
    assert adapter._extract_cover(html) == "https://www.wuxiaspot.com/covers/example.jpg"


@pytest.mark.parametrize(
    ("module_name", "adapter_name", "novel_url", "html", "expected_cover"),
    [
        (
            "backend.adapters.novelfire",
            "NovelFireAdapter",
            "https://novelfire.net/book/example",
            '<div class="book-img"><img src="/images/placeholder.jpg" '
            'data-src="//cdn.novelfire.net/covers/example.jpg"></div>',
            "https://cdn.novelfire.net/covers/example.jpg",
        ),
        (
            "backend.adapters.novelphoenix",
            "NovelPhoenixAdapter",
            "https://novelphoenix.net/novel/example",
            '<div class="cover"><img src="data:image/gif;base64,R0lGODlh" '
            'data-original="/covers/example.jpg"></div>',
            "https://novelphoenix.net/covers/example.jpg",
        ),
    ],
)
@pytest.mark.asyncio
async def test_novel_adapters_extract_lazy_cover_images(
    monkeypatch, module_name, adapter_name, novel_url, html, expected_cover
):
    import importlib

    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "fetch_html", AsyncMock(return_value=html))
    metadata = await getattr(module, adapter_name)().get_novel_metadata(novel_url)

    assert metadata.cover_url == expected_cover


def test_cover_extraction_falls_back_to_open_graph_image():
    from backend.adapters.fetcher import parse_cover_url

    html = (
        '<meta property="og:image" content="//cdn.example.com/cover.jpg">'
        '<div class="cover"><img src="/images/placeholder.jpg"></div>'
    )
    assert parse_cover_url(html, [".cover img"], "https://novelphoenix.com/novel/book") == (
        "https://cdn.example.com/cover.jpg"
    )


def test_easternwordsmith_extracts_public_chapters_in_order():
        from backend.adapters.easternwordsmith import (
                EasternWordsmithAdapter,
                _extract_chapter_refs,
                _extract_chapter_text,
                _label_value,
        )
        from bs4 import BeautifulSoup
        from backend.adapters.base import AdapterRegistry

        novel_url = "https://easternwordsmith.com/novel/example"
        html = """
        <title>Example Novel</title>
        <div class="row"><div class="col-4"><a>Author</a></div>
            <div class="col-8"><a>Test Author</a></div></div>
        <div class="row"><div class="col-4"><a>Description</a></div>
            <div class="col-8"><p>Novel summary.</p></div></div>
        <a href="../chapter/30">Chapter 3</a>
        <a class="early-access-link" href="../chapter/40">Locked Chapter 4</a>
        <a href="../chapter/10">Chapter 1</a>
        <a href="../chapter/20">Chapter 2</a>
        <section id="CS"><p>Story paragraph.</p>
            <p>Second paragraph<ins>Ad content</ins>.</p></section>
        """
        refs = _extract_chapter_refs(html, novel_url)
        soup = BeautifulSoup(html, "html.parser")

        adapter = EasternWordsmithAdapter()
        AdapterRegistry.register(adapter)
        assert AdapterRegistry.find(novel_url) is adapter
        assert AdapterRegistry.find("https://www.easternwordsmith.com/novel/example") is adapter
        assert [ref.title for ref in refs] == ["Chapter 1", "Chapter 2", "Chapter 3"]
        assert refs[0].source_url == "https://easternwordsmith.com/chapter/10"
        assert _label_value(soup, "Author") == "Test Author"
        assert _label_value(soup, "Description") == "Novel summary."
        assert _extract_chapter_text(html) == "Story paragraph.\n\nSecond paragraph."


# ── Test: database init ────────────────────────────────────────────────────────
def test_db_init(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    # Re-import to pick up env var
    import importlib
    import backend.database as db_module
    importlib.reload(db_module)
    db_module.init_db()
    assert (tmp_path / "test.db").exists()


def test_interrupted_translation_is_retryable(tmp_path, monkeypatch):
    from backend.models import ChapterStatus
    import importlib
    import backend.database as db_module

    monkeypatch.setenv("DB_PATH", str(tmp_path / "recovery.db"))
    db_module = importlib.reload(db_module)
    db_module.init_db()
    novel = db_module.create_novel(
        title="Recovery Test", source_url="http://test.com/recovery", source_site="novelfire"
    )
    chapter = db_module.upsert_chapter(novel.id, 0, "Chapter 1", "http://test.com/1")
    db_module.update_chapter(
        chapter.id, raw_text="Source text", status=ChapterStatus.translating
    )

    assert [c.id for c in db_module.get_pending_translation_chapters(novel.id)] == [chapter.id]
    db_module.init_db()
    recovered = db_module.get_chapters(novel.id)[0]
    assert recovered.status == ChapterStatus.scraped
    assert recovered.raw_text == "Source text"


def test_gemini_uses_relaxed_safety_and_candidate_text():
    from types import SimpleNamespace
    from backend.translation.gemini_provider import _build_safety_settings, _response_text

    class FakeSafetySetting:
        def __init__(self, **kwargs):
            self.values = kwargs

    fake_types = SimpleNamespace(
        HarmCategory=SimpleNamespace(
            HARM_CATEGORY_HARASSMENT="harassment",
            HARM_CATEGORY_HATE_SPEECH="hate_speech",
            HARM_CATEGORY_SEXUALLY_EXPLICIT="sexually_explicit",
            HARM_CATEGORY_DANGEROUS_CONTENT="dangerous_content",
        ),
        HarmBlockThreshold=SimpleNamespace(BLOCK_ONLY_HIGH="block_only_high"),
        SafetySetting=FakeSafetySetting,
    )
    settings = _build_safety_settings(fake_types)
    assert len(settings) == 4
    assert all(setting.values["threshold"] == "block_only_high" for setting in settings)

    response = SimpleNamespace(
        text="",
        candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text="Translated chapter")]),
            finish_reason=None,
        )],
    )
    assert _response_text(response) == "Translated chapter"


# ── Test: adapter registry ─────────────────────────────────────────────────────
def test_adapter_registry():
    from backend.adapters.base import AdapterRegistry
    from backend.adapters.galaxynovels import GalaxyNovelsAdapter
    from backend.adapters.novelfire import NovelFireAdapter
    from backend.adapters.novelphoenix import NovelPhoenixAdapter
    from backend.adapters.ranovel import RanovelAdapter
    from backend.adapters.wtrlab import WTRLabAdapter

    AdapterRegistry.register(NovelFireAdapter())
    AdapterRegistry.register(WTRLabAdapter())
    AdapterRegistry.register(NovelPhoenixAdapter())
    AdapterRegistry.register(GalaxyNovelsAdapter())
    AdapterRegistry.register(RanovelAdapter())

    nf = AdapterRegistry.find("https://novelfire.net/book/test")
    assert nf is not None
    assert nf.site_id == "novelfire"

    wtr = AdapterRegistry.find(
        "https://wtr-lab.com/en/novel/8937/multifunctional-sword-cultivation-starts-from-taxi?tab=toc"
    )
    assert wtr is not None
    assert wtr.site_id == "wtrlab"

    np = AdapterRegistry.find("https://novelphoenix.com/novel/test")
    assert np is not None
    assert np.site_id == "novelphoenix"

    gn = AdapterRegistry.find("https://galaxynovels.com/novel/unscientific-beast-taming/")
    assert gn is not None
    assert gn.site_id == "galaxynovels"

    ranovel = AdapterRegistry.find("https://ranovel.com/novel/example/")
    assert ranovel is not None
    assert ranovel.site_id == "ranovel"

    unknown = AdapterRegistry.find("https://some-random-site.com")
    assert unknown is None


def test_ranovel_extracts_and_orders_chapters():
    from backend.adapters.ranovel import _extract_refs, RanovelAdapter

    html = """<div id="manga-chapters-holder">
        <li class="wp-manga-chapter"><a href="/novel/example/chapter-3/">Chapter 3</a></li>
        <li class="wp-manga-chapter"><a href="/novel/example/chapter-1/">Chapter 1</a></li>
        <li class="wp-manga-chapter"><a href="/novel/example/chapter-2/">Chapter 2</a></li>
    </div>"""
    refs = _extract_refs(html, "https://ranovel.com/novel/example/", set(), 0)

    ordered = RanovelAdapter._ordered(refs)
    assert [ref.title for ref in ordered] == ["Chapter 1", "Chapter 2", "Chapter 3"]


def test_wtrlab_removes_site_shell_from_chapter_text():
    from backend.adapters.wtrlab import WTRLabAdapter

    text = WTRLabAdapter()._clean_chapter_text(
        "Please disable your ad blocker to support our site and continue enjoying free content.\n"
        "A real chapter paragraph with enough text to be retained by the adapter.\n"
        "Read translated novels and track your reading progress.\n"
        "Copyright © 2022 - wtr-lab.com"
    )

    assert text == "A real chapter paragraph with enough text to be retained by the adapter."


def test_wtrlab_chooses_longest_content_block():
        from backend.adapters.wtrlab import WTRLabAdapter

        html = """
        <div class="tiptap-content"><p>Short sample review.</p></div>
        <div class="tiptap-content">
            <p>Chapter paragraph one with the actual story content.</p>
            <p>Chapter paragraph two continues the actual story content.</p>
        </div>
        """

        text = WTRLabAdapter()._best_chapter_candidate(html, [".tiptap-content"])

        assert "actual story content" in text
        assert "Short sample review" not in text


def test_wtrlab_extracts_wtr_lines_in_order():
        from backend.adapters.wtrlab import WTRLabAdapter

        html = """
        <div id="chapter-9"><span></span><span></span><div class="chapter-body">
            <div data-line="0" class="wtr-line pr-line-text">Chapter 9 Portable Space</div>
            <div data-line="1" class="wtr-line pr-line-text">The real chapter text is here.</div>
        </div></div>
        """

        text = WTRLabAdapter()._best_chapter_candidate(
                html,
                [".chapter-body .wtr-line.pr-line-text", ".chapter-body"],
        )

        assert text == "Chapter 9 Portable Space\n\nThe real chapter text is here."


def test_ranovel_honeypot_cleaning_removes_same_color_bg():
    """Hidden paragraphs where color == background-color must be removed."""
    from backend.adapters.ranovel import _clean_chapter_html
    from bs4 import BeautifulSoup

    html = """<div class="reading-content"><div class="text-left">
        <p style="color:#262626; background-color:#262626;">Only Ran(o)vel dot com</p>
        <p>Real chapter content here.</p>
    </div></div>"""

    cleaned = _clean_chapter_html(html)
    soup = BeautifulSoup(cleaned, "html.parser")
    paragraphs = [p.get_text().strip() for p in soup.find_all("p") if p.get_text().strip()]
    assert "Real chapter content here." in paragraphs
    assert not any("Ran" in p for p in paragraphs), "Honeypot text should be removed"


def test_ranovel_honeypot_cleaning_removes_code_block_divs():
    """<div class='code-block …'> watermark wrappers must be removed."""
    from backend.adapters.ranovel import _clean_chapter_html
    from bs4 import BeautifulSoup

    html = """<div class="reading-content"><div class="text-left">
        <div class="code-block code-block-3">
            <p style="color:#262626; background-color:#262626;">Read Novel ranovel com</p>
        </div>
        <p>Actual story paragraph.</p>
    </div></div>"""

    cleaned = _clean_chapter_html(html)
    soup = BeautifulSoup(cleaned, "html.parser")
    paragraphs = [p.get_text().strip() for p in soup.find_all("p") if p.get_text().strip()]
    assert "Actual story paragraph." in paragraphs
    assert not any("ranovel" in p.lower() for p in paragraphs), "Watermark div should be removed"


def test_ranovel_honeypot_cleaning_strips_watermark_lines_from_text():
    """Lines containing watermark patterns must be dropped from the final text output."""
    from backend.adapters.ranovel import _WATERMARK_RE

    lines = [
        "The weather is really crappy.",
        "Read Novel 𝒓𝒂𝒏𝒐𝒗𝒆𝒍 com",
        "A battlefield dyed in blood-red skies.",
        "Only Ran(o)vel dot com",
        "Standing before the corpse of a giant monster.",
        "* * * Ranovel dot com * * *",
    ]
    clean = [ln for ln in lines if not _WATERMARK_RE.search(ln)]
    assert clean == [
        "The weather is really crappy.",
        "A battlefield dyed in blood-red skies.",
        "Standing before the corpse of a giant monster.",
    ]




@pytest.mark.asyncio
async def test_galaxy_uses_full_manifest(monkeypatch):
    import backend.adapters.galaxynovels as galaxy_module

    novel_url = "https://galaxynovels.com/novel/example/"
    html = '<div class="wor-novel-chapters-wrap" data-manifest-url="/api/chapters.json"></div>'
    manifest = '{"chapters": [{"url": "/chapter-1/", "label": "Chapter 1"}, {"url": "/chapter-2/", "label": "Chapter 2"}]}'

    monkeypatch.setattr(galaxy_module, "_fetch_galaxy_html", AsyncMock(return_value=html))
    fetch_manifest = AsyncMock(return_value=manifest)
    monkeypatch.setattr(galaxy_module, "fetch_html", fetch_manifest)
    monkeypatch.setattr(
        galaxy_module,
        "fetch_html_playwright",
        AsyncMock(side_effect=AssertionError("fetch_html_playwright should not be called")),
    )

    refs = await galaxy_module.GalaxyNovelsAdapter().get_chapter_list(novel_url)

    assert [ref.title for ref in refs] == ["Chapter 1", "Chapter 2"]
    fetch_manifest.assert_awaited_once_with(
        "https://galaxynovels.com/api/chapters.json", "galaxynovels"
    )


@pytest.mark.asyncio
async def test_galaxy_uses_full_manifest_bs4_fallback(monkeypatch):
    import backend.adapters.galaxynovels as galaxy_module

    novel_url = "https://galaxynovels.com/novel/example/"
    html = '<div class="wor-novel-chapters-wrap" data-manifest-url="/api/chapters.json"></div>'
    manifest = '{"chapters": [{"url": "/chapter-1/", "label": "Chapter 1"}, {"url": "/chapter-2/", "label": "Chapter 2"}]}'

    monkeypatch.setattr(galaxy_module, "_SELECTOLAX_AVAILABLE", False)
    monkeypatch.setattr(galaxy_module, "_fetch_galaxy_html", AsyncMock(return_value=html))
    fetch_manifest = AsyncMock(return_value=manifest)
    monkeypatch.setattr(galaxy_module, "fetch_html", fetch_manifest)
    monkeypatch.setattr(
        galaxy_module,
        "fetch_html_playwright",
        AsyncMock(side_effect=AssertionError("fetch_html_playwright should not be called")),
    )

    refs = await galaxy_module.GalaxyNovelsAdapter().get_chapter_list(novel_url)

    assert [ref.title for ref in refs] == ["Chapter 1", "Chapter 2"]
    fetch_manifest.assert_awaited_once_with(
        "https://galaxynovels.com/api/chapters.json", "galaxynovels"
    )


@pytest.mark.asyncio
async def test_galaxy_expands_initial_chapter_batch(monkeypatch):
    import backend.adapters.galaxynovels as galaxy_module

    novel_url = "https://galaxynovels.com/novel/example/"
    initial = "".join(
        f'<a href="/chapter-{number}/">Chapter {number}</a>'
        for number in range(31, 1, -1)
    )
    expanded = "".join(
        f'<a href="/chapter-{number}/">Chapter {number}</a>'
        for number in range(61, 0, -1)
    )
    monkeypatch.setattr(
        galaxy_module,
        "_fetch_galaxy_html",
        AsyncMock(return_value=f'<div class="wor-novel-chapters-list">{initial}</div>'),
    )
    monkeypatch.setattr(
        galaxy_module,
        "_fetch_galaxy_html_with_all_chapters",
        AsyncMock(return_value=f'<div class="wor-novel-chapters-list">{expanded}</div>'),
    )

    refs = await galaxy_module.GalaxyNovelsAdapter().get_chapter_list(novel_url)

    assert len(refs) == 61
    assert refs[0].title == "Chapter 1"
    assert refs[-1].title == "Chapter 61"


@pytest.mark.asyncio
async def test_nodriver_starts_with_no_sandbox(monkeypatch, tmp_path):
    import asyncio
    import sys
    import types

    import backend.adapters.nodriver_fetch as nodriver_fetch

    captured = {}
    real_sleep = asyncio.sleep

    class FakeTab:
        async def evaluate(self, *args, **kwargs):
            return "unusual reading activity" not in args[0]

        async def get_content(self):
            if captured.get("delay_content"):
                await real_sleep(0.1)
            return "<html><body><h1>Chapter</h1></body></html>"

    class FakeProcess:
        wait_timeout = None

        def wait(self, timeout=None):
            self.wait_timeout = timeout
            return 0

    class FakeBrowser:
        def __init__(self):
            self._process = FakeProcess()
            self.stopped = False

        async def get(self, url):
            return FakeTab()

        def stop(self):
            self.stopped = True

    async def fake_start(**kwargs):
        captured.update(kwargs)
        captured["browser"] = FakeBrowser()
        return captured["browser"]

    monkeypatch.setitem(sys.modules, "nodriver", types.SimpleNamespace(start=fake_start))
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(nodriver_fetch.asyncio, "sleep", fake_sleep)

    await nodriver_fetch._run(
        "https://example.com",
        "h1",
        10,
        str(tmp_path / "chapter.html"),
        "",
    )

    assert captured["headless"] is False
    assert captured["sandbox"] is False
    assert captured["no_sandbox"] is True
    assert captured["browser"].stopped
    assert captured["browser"]._process.wait_timeout == 5
    assert 5 not in sleeps

    shutdown_signal = tmp_path / "stop-nodriver"
    shutdown_signal.touch()
    shutdown_output = tmp_path / "cancelled-chapter.html"
    captured["delay_content"] = True
    await nodriver_fetch._run_with_shutdown_signal(
        "https://example.com",
        "h1",
        10,
        str(shutdown_output),
        "",
        str(shutdown_signal),
    )

    assert captured["browser"].stopped
    assert not shutdown_output.exists()


def test_wtrlab_urls_are_case_insensitive():
    from backend.adapters.base import AdapterRegistry
    from backend.adapters.wtrlab import WTRLabAdapter

    wtr = WTRLabAdapter()
    AdapterRegistry.register(wtr)

    assert wtr.can_handle("https://WTR-LAB.com/en/novel/8937/multifunctional-sword-cultivation-starts-from-taxi?tab=toc")
    assert AdapterRegistry.find("https://WTR-LAB.com/en/novel/8937/multifunctional-sword-cultivation-starts-from-taxi?tab=toc") is wtr


@pytest.mark.asyncio
async def test_native_arabic_novel_cannot_be_queued_for_translation(monkeypatch):
    import backend.pipeline as pipeline_module
    from backend.adapters.galaxynovels import GalaxyNovelsAdapter
    from backend.models import Novel

    novel = Novel(
        id=3, title="Arabic novel", source_url="https://galaxynovels.com/novel/example/",
        source_site="galaxynovels",
    )
    monkeypatch.setattr(pipeline_module, "get_novel", lambda novel_id: novel)
    monkeypatch.setattr(pipeline_module, "get_provider", MagicMock())
    pipeline_module.AdapterRegistry.register(GalaxyNovelsAdapter())

    with pytest.raises(ValueError, match="already in Arabic"):
        await pipeline_module.run_translation_job(novel.id)


# ── Test: system prompt builder ────────────────────────────────────────────────
def test_system_prompt_builder():
    from backend.models import GlossaryRule
    from backend.translation.base import build_system_prompt

    rules = [
        GlossaryRule(source_term="god tier", target_term="مستوى مكرم"),
        GlossaryRule(source_term="cultivation", target_term="تنمية الطاقة"),
    ]
    prompt = build_system_prompt(rules)
    assert "god tier" in prompt
    assert "مستوى مكرم" in prompt
    assert "Arabic" in prompt


# ── Test: glossary post-pass ───────────────────────────────────────────────────
def test_glossary_postpass():
    from backend.models import GlossaryRule
    from backend.translation.base import apply_glossary_postpass

    rules = [GlossaryRule(source_term="god tier", target_term="مستوى مكرم")]
    result = apply_glossary_postpass("He reached the God Tier level.", rules)
    assert "مستوى مكرم" in result
    assert "God Tier" not in result


# ── Test: EPUB builder produces a file ────────────────────────────────────────
def test_epub_builder(tmp_path):
    from backend.models import Chapter, ChapterStatus, Novel, NovelStatus
    from backend.epub_builder import build_epub
    from datetime import datetime, timezone

    novel = Novel(
        id=1, title="Test Novel", source_url="http://test.com",
        source_site="novelfire", status=NovelStatus.scraped,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    chapter = Chapter(
        id=1, novel_id=1, index=0, title="Chapter 1",
        source_url="http://test.com/ch1",
        translated_text="هذا اختبار للفصل الأول.",
        status=ChapterStatus.translated,
        updated_at=datetime.now(timezone.utc),
    )

    output = build_epub(novel, [chapter], output_dir=tmp_path)
    assert output.exists()
    assert output.suffix == ".epub"
    assert output.stat().st_size > 100


def test_epub_builder_escapes_plain_text(tmp_path):
    from xml.etree import ElementTree
    from zipfile import ZipFile
    from backend.models import Chapter, ChapterStatus, Novel, NovelStatus
    from backend.epub_builder import build_epub

    novel = Novel(
        id=8, title="A & B <Novel>", author="Author & Co",
        description="Description with <angle brackets> & ampersands",
        source_url="https://example.com", source_site="novelfire",
        status=NovelStatus.scraped,
    )
    chapter = Chapter(
        id=8, novel_id=8, index=0, title="Chapter & <1>",
        source_url="https://example.com/chapter-1",
        translated_text="Text with <angle brackets> & ampersands.",
        status=ChapterStatus.translated,
    )

    output = build_epub(novel, [chapter], output_dir=tmp_path)

    with ZipFile(output) as archive:
        chapter_root = ElementTree.fromstring(archive.read("EPUB/text/chapter_0000.xhtml"))
        cover_root = ElementTree.fromstring(archive.read("EPUB/text/cover.xhtml"))

    namespace = {"x": "http://www.w3.org/1999/xhtml"}
    assert chapter_root.findtext(".//x:h2", namespaces=namespace) == chapter.title
    assert chapter_root.findtext(".//x:p", namespaces=namespace) == chapter.translated_text
    assert cover_root.findtext(".//x:h1", namespaces=namespace) == novel.title
    assert cover_root.findtext(".//x:p[@class='author']", namespaces=namespace) == "المؤلف: Author & Co"
    assert cover_root.findtext(".//x:div[@class='description']", namespaces=namespace) == novel.description


def test_epub_builder_preserves_scraped_paragraph_breaks(tmp_path):
    from xml.etree import ElementTree
    from zipfile import ZipFile
    from backend.models import Chapter, ChapterStatus, Novel, NovelStatus
    from backend.epub_builder import build_epub

    novel = Novel(
        id=9, title="Paragraph Test", source_url="https://example.com",
        source_site="lncrawl_test", status=NovelStatus.scraped,
    )
    chapter = Chapter(
        id=9, novel_id=9, index=0, title="Chapter One",
        source_url="https://example.com/chapter-1",
        translated_text="First paragraph.\n\nSecond paragraph.",
        status=ChapterStatus.translated,
    )

    output = build_epub(novel, [chapter], output_dir=tmp_path)

    with ZipFile(output) as archive:
        chapter_root = ElementTree.fromstring(
            archive.read("EPUB/text/chapter_0000.xhtml")
        )

    namespace = {"x": "http://www.w3.org/1999/xhtml"}
    assert [
        paragraph.text
        for paragraph in chapter_root.findall(".//x:p", namespaces=namespace)
    ] == ["First paragraph.", "Second paragraph."]


def test_epub_builder_preserves_cover_image_type(tmp_path, monkeypatch):
    from zipfile import ZipFile
    from backend.models import Novel, NovelStatus
    from backend.epub_builder import build_epub

    class FakeResponse:
        status_code = 200
        content = b"\x89PNG\r\n\x1a\nfake image data"
        headers = {"content-type": "image/png"}

    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: FakeResponse())
    novel = Novel(
        id=9, title="Cover type test", source_url="https://example.com",
        source_site="novelfire", status=NovelStatus.scraped,
        cover_url="https://example.com/cover.png",
    )

    output = build_epub(novel, [], output_dir=tmp_path)

    with ZipFile(output) as archive:
        assert "EPUB/images/cover.png" in archive.namelist()
        opf_path = next(name for name in archive.namelist() if name.endswith(".opf"))
        assert 'media-type="image/png"' in archive.read(opf_path).decode("utf-8")


def test_epub_import_creates_translatable_chapters(tmp_path, monkeypatch):
    from ebooklib import epub
    import importlib

    source = tmp_path / "Example Novel.epub"
    book = epub.EpubBook()
    book.set_identifier("test-novel")
    book.set_title("Example Novel")
    book.set_language("en")
    book.add_author("Test Author")

    cover = epub.EpubHtml(title="Cover", file_name="cover.xhtml", lang="en")
    cover.content = b"<html><body><img src='cover.jpg'></body></html>"
    title_page = epub.EpubHtml(title="Title", file_name="title.xhtml", lang="en")
    title_page.content = b"<html><body><p>Example Novel</p></body></html>"
    chapter = epub.EpubHtml(title="Chapter 1", file_name="text/chapter-1.xhtml", lang="en")
    chapter.content = (
        b"<html><body><h1>Chapter 1: A Beginning</h1>"
        b"<p>First source paragraph.</p><p>Second source paragraph.</p></body></html>"
    )
    for item in (cover, title_page, chapter):
        book.add_item(item)
    book.toc = [chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", cover, title_page, chapter]
    epub.write_epub(str(source), book)

    monkeypatch.setenv("DB_PATH", str(tmp_path / "library.db"))
    import backend.database as database
    database = importlib.reload(database)
    database.init_db()

    import backend.epub_importer as importer
    importer = importlib.reload(importer)
    from backend.models import ChapterStatus, NovelStatus

    novel = importer.import_epub_file(source)
    chapters = database.get_chapters(novel.id)

    assert novel.title == "Example Novel"
    assert novel.author == "Test Author"
    assert novel.source_site == "epub_import"
    assert novel.status == NovelStatus.scraped
    assert len(chapters) == 1
    assert chapters[0].title == "Chapter 1: A Beginning"
    assert chapters[0].raw_text == "First source paragraph.\n\nSecond source paragraph."
    assert chapters[0].status == ChapterStatus.scraped


# ── Test: Native Arabic novel EPUB build without translation ─────────────────
def test_native_arabic_epub_builder(tmp_path):
    from backend.models import Chapter, ChapterStatus, Novel, NovelStatus
    from backend.adapters.base import AdapterRegistry
    from backend.adapters.galaxynovels import GalaxyNovelsAdapter
    from backend.epub_builder import build_epub
    from datetime import datetime, timezone

    AdapterRegistry.register(GalaxyNovelsAdapter())

    novel = Novel(
        id=2, title="ترويض الوحوش", source_url="https://galaxynovels.com/novel/unscientific-beast-taming/",
        source_site="galaxynovels", status=NovelStatus.scraped,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    # Native Arabic chapters have raw_text and translated_text is None
    chapter = Chapter(
        id=2, novel_id=2, index=0, title="الفصل 1",
        source_url="https://galaxynovels.com/novel/unscientific-beast-taming/chapter-1/",
        raw_text="استيقظ شي يو ليجد نفسه في عالم جديد...",
        translated_text=None,
        status=ChapterStatus.scraped,
        updated_at=datetime.now(timezone.utc),
    )

    output = build_epub(novel, [chapter], output_dir=tmp_path)
    assert output.exists()
    assert output.suffix == ".epub"
    assert output.stat().st_size > 100


# ── Test: JobControl pause, resume, cancel ────────────────────────────────────
@pytest.mark.asyncio
async def test_job_control():
    import asyncio
    from backend.pipeline import JobControl

    jc = JobControl()
    assert not jc.is_paused
    assert not jc.is_cancelled

    # Check non-paused does not block
    await jc.check()

    # Test pause and resume
    jc.pause()
    assert jc.is_paused

    async def unpause_soon():
        await asyncio.sleep(0.1)
        jc.resume()

    asyncio.create_task(unpause_soon())
    await jc.check()  # should unblock after resume
    assert not jc.is_paused

    # Test cancel
    jc.cancel()
    assert jc.is_cancelled
    with pytest.raises(asyncio.CancelledError):
        await jc.check()


# ── Test: TokenRouter Provider & Provider Factory ─────────────────────────────
def test_tokenrouter_provider(monkeypatch):
    from backend.translation import TokenRouterProvider, get_provider, ProviderFailureError

    # Test unavailable without key
    monkeypatch.setenv("TOKENROUTER_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    tp = TokenRouterProvider()
    assert not tp.is_available()

    # Test available with valid key
    tp_keyed = TokenRouterProvider(api_key="sk-test-tokenrouter-valid-key-12345")
    assert tp_keyed.is_available()

    # Test factory instantiation
    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-tokenrouter-test-key-54321")
    prov = get_provider("tokenrouter", model="deepseek/deepseek-v4-pro-0813-free")
    assert prov.provider_name == "tokenrouter"
    assert prov._model == "deepseek/deepseek-v4-pro-0813-free"


@pytest.mark.asyncio
async def test_tokenrouter_translation_mock(monkeypatch):
    from backend.models import GlossaryRule
    from backend.translation.tokenrouter_provider import TokenRouterProvider

    tp = TokenRouterProvider(api_key="sk-mock-key-tokenrouter")
    
    # Mock internal _call_api
    def mock_call(system_prompt, text, model):
        assert "deepseek" in model or "qwen" in model
        return "هذا نص تجريبي مترجم."

    monkeypatch.setattr(tp, "_call_api", mock_call)
    glossary = [GlossaryRule(source_term="test", target_term="تجريبي")]
    result = await tp.translate_chapter("This is a test chapter.", glossary, model="deepseek/deepseek-v4-pro-0813-free")
    assert "هذا نص تجريبي مترجم." in result


# ── Test: Provider Failure Error and Suggestion ────────────────────────────────
def test_provider_failure_error():
    from backend.translation import ProviderFailureError

    err = ProviderFailureError("tokenrouter", "Rate limit exceeded (429)")
    assert "tokenrouter" in str(err)
    assert "Rate limit exceeded" in str(err)
    assert "API Keys" in str(err)
    assert "switch" in str(err).lower()


def test_rate_limit_error_detection():
    from backend.pipeline import _is_rate_limit_error

    class Http429Error(Exception):
        status_code = 429

    assert _is_rate_limit_error(Http429Error("provider rejected request"))
    assert _is_rate_limit_error(RuntimeError("HTTP 429 Too Many Requests"))
    assert not _is_rate_limit_error(RuntimeError("HTTP 401 Invalid API key"))
    assert not _is_rate_limit_error(RuntimeError("Model not found"))


def test_web_bridge_provider_management(monkeypatch, tmp_path):
    import gui.web_bridge as bridge_module
    import gui.widgets.api_keys as api_keys_module

    env_file = tmp_path / ".env"
    env_file.write_text(
        "TOKENROUTER_API_KEY=test-key\n"
        "TOKENROUTER_MODEL=old-model\n"
        "TOKENROUTER_MODELS=old-model,second-model\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge_module, "ENV_FILE", env_file)
    monkeypatch.setattr(api_keys_module, "ENV_FILE", env_file)
    channel = bridge_module.NovelBridgeWebChannel()

    assert channel.save_provider("TOKENROUTER", "https://api.tokenrouter.com/v1", '["renamed-model"]')
    saved = env_file.read_text(encoding="utf-8")
    assert "TOKENROUTER_MODEL=renamed-model" in saved
    assert "TOKENROUTER_MODELS=renamed-model" in saved

    class UnconfiguredProvider:
        def __init__(self, model=None):
            self._model = model

        def is_available(self):
            return False

    monkeypatch.setattr(bridge_module, "TokenRouterProvider", UnconfiguredProvider)
    result = channel.test_provider("TOKENROUTER", "renamed-model")
    assert '"ok": false' in result
    assert "not configured" in result.lower()


def test_gemini_provider_uses_instance_key(monkeypatch):
    import sys
    import types

    import backend.translation.gemini_provider as gemini_module

    captured = {}

    class FakeGenerateContentConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeSafetySetting:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeResponse:
        text = "translated"

    class FakeChat:
        @staticmethod
        def send_message(message):
            captured["message"] = message
            return FakeResponse()

    class FakeChats:
        @staticmethod
        def create(**kwargs):
            captured["chat_config"] = kwargs
            return FakeChat()

    class FakeClient:
        def __init__(self, api_key):
            captured["api_key"] = api_key

        chats = FakeChats()

    fake_google = types.ModuleType("google")
    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = FakeClient
    fake_genai.types = types.SimpleNamespace(
        GenerateContentConfig=FakeGenerateContentConfig,
        SafetySetting=FakeSafetySetting,
        HarmCategory=types.SimpleNamespace(
            HARM_CATEGORY_HARASSMENT="harassment",
            HARM_CATEGORY_HATE_SPEECH="hate_speech",
            HARM_CATEGORY_SEXUALLY_EXPLICIT="sexually_explicit",
            HARM_CATEGORY_DANGEROUS_CONTENT="dangerous_content",
        ),
        HarmBlockThreshold=types.SimpleNamespace(BLOCK_ONLY_HIGH="block_only_high"),
    )
    fake_google.genai = fake_genai

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setattr(gemini_module, "_client", None)
    monkeypatch.setattr(gemini_module, "_API_KEY", "stale-key")

    provider = gemini_module.GeminiProvider(api_key="fresh-key")
    monkeypatch.setattr(provider, "_client", None)
    result = provider._call_api("system", "text", "gemini-2.5-flash")

    assert result == "translated"
    assert captured["api_key"] == "fresh-key"
    assert captured["chat_config"]["model"] == "gemini-2.5-flash"
    assert captured["message"] == "text"


def test_grok_provider(monkeypatch):
    from backend.translation import GrokProvider, get_provider

    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_API_KEY", raising=False)
    assert not GrokProvider().is_available()

    provider = GrokProvider(api_key="xai-test-key", model="grok-3-mini")
    assert provider.is_available()
    assert provider._base_url == "https://api.x.ai/v1"

    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    selected = get_provider("grok", model="grok-3-mini")
    assert selected.provider_name == "grok"
    assert selected._model == "grok-3-mini"


def test_groq_base_url_normalization():
    from backend.translation import GroqProvider

    assert GroqProvider._normalize_base_url("https://api.groq.com/openai/v1") == "https://api.groq.com"
    assert GroqProvider._normalize_base_url("https://api.groq.com/openai/v1/") == "https://api.groq.com"
    assert GroqProvider._normalize_base_url("https://proxy.example/v1") == "https://proxy.example/v1"


def test_groq_response_text_extraction():
    from backend.translation import GroqProvider

    class Message:
        content = "  translated chapter  "

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    assert GroqProvider._response_text(Response()) == "translated chapter"


def test_groq_stream_text_preserves_chunk_whitespace():
    from backend.translation import GroqProvider

    class FirstDelta:
        content = "Hello"

    class SecondDelta:
        content = " world"

    class FirstChunk:
        choices = [type("Choice", (), {"delta": FirstDelta()})()]

    class SecondChunk:
        choices = [type("Choice", (), {"delta": SecondDelta()})()]

    result = GroqProvider._stream_text(FirstChunk()) + GroqProvider._stream_text(SecondChunk())
    assert result == "Hello world"


def test_groq_gpt_oss_request_options(monkeypatch):
    import sys
    import types
    from backend.translation import GroqProvider

    captured = {}

    class FakeDelta:
        content = "translated"

    class FakeChoice:
        delta = FakeDelta()

    class FakeChunk:
        choices = [FakeChoice()]

    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return [FakeChunk()]

    class FakeClient:
        chat = types.SimpleNamespace(completions=FakeCompletions())

        def __init__(self, **kwargs):
            captured["client"] = kwargs

    fake_groq = types.ModuleType("groq")
    fake_groq.Groq = FakeClient
    monkeypatch.setitem(sys.modules, "groq", fake_groq)

    result = GroqProvider(api_key="test-key")._call_api("system", "text", "openai/gpt-oss-20b")

    assert result == "translated"
    assert captured["reasoning_effort"] == "medium"
    assert captured["max_completion_tokens"] == 8192
    assert captured["temperature"] == 1
    assert captured["top_p"] == 1
    assert captured["stop"] is None


def test_groq_qwen_request_options():
    from backend.translation import GroqProvider

    options = GroqProvider._request_options("qwen/qwen3.8-27b", [{"role": "user", "content": "text"}])

    assert options["reasoning_effort"] == "default"
    assert options["max_completion_tokens"] == 8192
    assert options["temperature"] == 0.6
    assert options["top_p"] == 0.95
    assert options["stop"] is None


def test_groq_splits_long_chapters_at_paragraphs():
    from backend.translation import GroqProvider

    text = "A" * 3500 + "\n\n" + "B" * 3500
    chunks = GroqProvider._split_text(text, max_chars=4000)

    assert len(chunks) == 2
    assert "A" * 3500 in chunks[0]
    assert "B" * 3500 in chunks[1]


@pytest.mark.asyncio
async def test_gemini_sends_entire_chapter_in_one_request(monkeypatch):
    from backend.translation import GeminiProvider

    chapter = "paragraph one\n\n" + ("long chapter text " * 500)
    captured = []
    provider = GeminiProvider(api_key="test-gemini-key")

    def fake_call(system_prompt, text, model):
        captured.append(text)
        return "translated"

    monkeypatch.setattr(provider, "_call_api", fake_call)
    result = await provider.translate_chapter(chapter, [], model="gemini-2.5-flash")

    assert result == "translated"
    assert captured == [chapter]


@pytest.mark.asyncio
async def test_translation_pipeline_failure_stops_and_suggests(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from backend.models import Novel, Chapter, NovelStatus, ChapterStatus, JobStatus
    from backend.pipeline import run_translation_job
    from backend.translation import ProviderFailureError, TokenRouterProvider
    import backend.database as db_module
    import backend.pipeline as pipeline_module

    monkeypatch.setenv("DB_PATH", str(tmp_path / "pipeline_test.db"))
    import importlib
    importlib.reload(db_module)
    db_module.init_db()

    novel = db_module.create_novel(title="Fail Test Novel", source_url="http://test.com/fail", source_site="novelfire")
    db_module.upsert_chapter(novel.id, 0, "Ch 1", "http://test.com/1")
    db_module.upsert_chapter(novel.id, 1, "Ch 2", "http://test.com/2")
    db_module.upsert_chapter(novel.id, 2, "Ch 3", "http://test.com/3")
    for c in db_module.get_chapters(novel.id):
        db_module.update_chapter(c.id, raw_text="Hello world", status=ChapterStatus.scraped)

    # Monkeypatch TokenRouterProvider to always raise an API error
    async def failing_translate(self, text, glossary, model=None):
        raise RuntimeError("TokenRouter API 429 Too Many Requests")

    sleep_calls = []

    async def fast_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setenv("TOKENROUTER_API_KEY", "sk-test-valid-key")
    monkeypatch.setattr(TokenRouterProvider, "translate_chapter", failing_translate)
    monkeypatch.setattr(pipeline_module.asyncio, "sleep", fast_sleep)

    with pytest.raises(ProviderFailureError) as exc_info:
        await run_translation_job(novel.id, provider_name="tokenrouter")

    assert "TokenRouter" in str(exc_info.value) or "tokenrouter" in str(exc_info.value)
    assert "Suggestion" in str(exc_info.value) or "switch" in str(exc_info.value).lower()
    assert 80.0 in sleep_calls


# ── Test: OrcaRouter Provider ──────────────────────────────────────────────────
def test_orcarouter_provider(monkeypatch):
    from backend.translation import OrcaRouterProvider, get_provider

    monkeypatch.setenv("ORCAROUTER_API_KEY", "")
    op = OrcaRouterProvider()
    assert not op.is_available()

    op_keyed = OrcaRouterProvider(api_key="sk-test-orca-key-12345")
    assert op_keyed.is_available()

    monkeypatch.setenv("ORCAROUTER_API_KEY", "sk-orca-key-99999")
    prov = get_provider("orcarouter", model="deepseek/deepseek-v4-flash-free")
    assert prov.provider_name == "orcarouter"
    assert prov._model == "deepseek/deepseek-v4-flash-free"


@pytest.mark.asyncio
async def test_orcarouter_translation_mock(monkeypatch):
    from backend.models import GlossaryRule
    from backend.translation.orcarouter_provider import OrcaRouterProvider

    op = OrcaRouterProvider(api_key="sk-mock-key-orca")

    def mock_call(system_prompt, text, model):
        assert "deepseek" in model or "orcarouter" in model
        return "هذا نص أوركا المترجم."

    monkeypatch.setattr(op, "_call_api", mock_call)
    glossary = [GlossaryRule(source_term="test", target_term="تجريبي")]
    result = await op.translate_chapter("Test text.", glossary, model="orcarouter/free")
    assert "هذا نص أوركا المترجم." in result
