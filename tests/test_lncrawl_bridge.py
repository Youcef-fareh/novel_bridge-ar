"""Tests for the optional lightnovel-crawler compatibility bridge."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class FakeNovel:
    def __init__(self, url: str):
        self.url = url
        self.title = ""
        self.author = ""
        self.cover_url = ""
        self.synopsis = ""
        self.chapters = []


class FakeChapter:
    def __init__(self, id: int, url: str, title: str):
        self.id = id
        self.url = url
        self.title = title
        self.body = None


class ModernCrawler:
    base_url = ["https://example.org/"]
    instances = []

    def __init__(self):
        self.closed = False
        self.downloaded_novel_url = None
        self.initialized = False
        self.instances.append(self)

    def initialize(self):
        self.initialized = True

    def read_novel(self, novel):
        assert self.initialized
        novel.title = "Example Novel"
        novel.author = "A. Writer"
        novel.cover_url = "/cover.jpg"
        novel.synopsis = "A description"
        novel.chapters = [
            SimpleNamespace(url="/chapter-1", title="Chapter 1"),
            SimpleNamespace(url="/chapter-2", title="Chapter 2"),
        ]

    def download_chapter(self, chapter):
        assert self.initialized
        self.downloaded_novel_url = self.novel_url
        chapter.body = (
            "<div><p>One <em>formatted</em> paragraph.</p>"
            "<p>Another paragraph.</p></div>"
        )

    def close(self):
        self.closed = True


class LegacyCrawler:
    base_url = ["https://legacy.example.org/"]
    instances = []

    def __init__(self):
        self.closed = False
        self.instances.append(self)
        self.novel = SimpleNamespace()

    def read_novel(self):
        self.novel = SimpleNamespace(
            title="Legacy Novel",
            authors=["First Author", "Second Author"],
            cover_url="https://legacy.example.org/cover.jpg",
            synopsis="Legacy description",
            chapters=[{"url": "/chapter", "title": "Chapter One"}],
        )

    def download_chapter_body(self, chapter):
        return "<p>Legacy chapter text.</p>"

    def close(self):
        self.closed = True


def test_default_lncrawl_root_finds_pyinstaller_internal_data(tmp_path, monkeypatch):
    from backend.adapters import lncrawl_bridge

    frozen_data_dir = tmp_path / "_internal"
    checkout = frozen_data_dir / "lightnovel-crawler-dev"
    (checkout / "lncrawl").mkdir(parents=True)
    monkeypatch.setattr(
        lncrawl_bridge,
        "__file__",
        str(tmp_path / "backend" / "adapters" / "lncrawl_bridge.py"),
    )
    monkeypatch.setattr(sys, "_MEIPASS", str(frozen_data_dir), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "NovelBridgeAR.exe"))

    assert lncrawl_bridge._default_lncrawl_root() == checkout


@pytest.mark.asyncio
async def test_lncrawl_bridge_adapts_modern_crawler_and_isolates_instances():
    from backend.adapters.lncrawl_bridge import LNCrawlBridgeAdapter

    ModernCrawler.instances.clear()
    adapter = LNCrawlBridgeAdapter(
        ModernCrawler, "lncrawl_test_modern", FakeNovel, FakeChapter
    )

    assert adapter.can_handle("https://example.org/novel/one")
    assert adapter.can_handle("https://sub.example.org/novel/one")
    assert not adapter.can_handle("https://example.org.evil.test/novel/one")

    metadata = await adapter.get_novel_metadata("https://example.org/novel/one")
    chapters = await adapter.get_chapter_list("https://example.org/novel/one")
    body = await adapter.get_chapter_text(chapters[0].source_url)

    assert metadata.title == "Example Novel"
    assert metadata.author == "A. Writer"
    assert metadata.cover_url == "https://example.org/cover.jpg"
    assert [chapter.title for chapter in chapters] == ["Chapter 1", "Chapter 2"]
    assert chapters[0].source_url == "https://example.org/chapter-1"
    assert body == "One formatted paragraph.\n\nAnother paragraph."
    assert len(ModernCrawler.instances) == 2
    assert ModernCrawler.instances[1].downloaded_novel_url == "https://example.org/novel/one"
    assert all(
        instance.initialized and instance.closed for instance in ModernCrawler.instances
    )


@pytest.mark.asyncio
async def test_lncrawl_bridge_search_returns_only_source_urls():
    from backend.adapters.lncrawl_bridge import LNCrawlBridgeAdapter

    class SearchCrawler:
        base_url = ["https://search.example.org"]

        def initialize(self):
            pass

        def search(self, query):
            return [
                SimpleNamespace(title=f"{query} novel", url="/book/one"),
                SimpleNamespace(title="Outside result", url="https://other.example/book"),
            ]

        def close(self):
            pass

    adapter = LNCrawlBridgeAdapter(
        SearchCrawler, "lncrawl_test_search", FakeNovel, FakeChapter
    )
    adapter.can_search = True

    results = await adapter.search("Sample", limit=5)

    assert [(item.title, item.url, item.source_site) for item in results] == [
        ("Sample novel", "https://search.example.org/book/one", "lncrawl_test_search")
    ]


@pytest.mark.asyncio
async def test_lncrawl_bridge_preserves_source_chapter_fields_for_download():
    from backend.adapters.lncrawl_bridge import LNCrawlBridgeAdapter

    class WuxiaWorldLikeCrawler:
        base_url = ["https://wuxia.example/"]

        def __init__(self):
            self.closed = False

        def initialize(self):
            pass

        def read_novel(self, novel):
            novel.title = "A Novel"
            novel.chapters = [
                SimpleNamespace(
                    id=1,
                    url="https://wuxia.example/chapter/one",
                    title="Chapter One",
                    chapterId="chapter-entity-id",
                    body=None,
                )
            ]

        def download_chapter(self, chapter):
            assert chapter.chapterId == "chapter-entity-id"
            chapter.body = "<p>Chapter content</p>"

        def close(self):
            self.closed = True

    adapter = LNCrawlBridgeAdapter(
        WuxiaWorldLikeCrawler, "lncrawl_test_wuxia", FakeNovel, FakeChapter
    )

    await adapter.get_novel_metadata("https://wuxia.example/novel")
    chapters = await adapter.get_chapter_list("https://wuxia.example/novel")
    content = await adapter.get_chapter_text(chapters[0].source_url)

    assert content == "Chapter content"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_language", ["English", "Arabic"])
async def test_lncrawl_bridge_preserves_breaks_and_inline_text(source_language):
    from backend.adapters.lncrawl_bridge import LNCrawlBridgeAdapter

    class ParagraphCrawler(LegacyCrawler):
        base_url = ["https://paragraphs.example/"]

        def download_chapter_body(self, chapter):
            return (
                "<div><p>First <span>complete</span> paragraph.</p>"
                "<p>Second paragraph<br/>continued on a new line.</p></div>"
            )

    adapter = LNCrawlBridgeAdapter(
        ParagraphCrawler,
        "lncrawl_test_paragraphs",
        FakeNovel,
        FakeChapter,
        source_language=source_language,
    )

    content = await adapter.get_chapter_text("https://paragraphs.example/chapter")

    assert content == (
        "First complete paragraph.\n\n"
        "Second paragraph\n\ncontinued on a new line."
    )


@pytest.mark.asyncio
async def test_lncrawl_bridge_supports_legacy_no_argument_crawler():
    from backend.adapters.lncrawl_bridge import LNCrawlBridgeAdapter

    LegacyCrawler.instances.clear()
    adapter = LNCrawlBridgeAdapter(
        LegacyCrawler, "lncrawl_test_legacy", FakeNovel, FakeChapter
    )

    metadata = await adapter.get_novel_metadata("https://legacy.example.org/novel")
    chapters = await adapter.get_chapter_list("https://legacy.example.org/novel")
    body = await adapter.get_chapter_text(chapters[0].source_url)

    assert metadata.title == "Legacy Novel"
    assert metadata.author == "First Author, Second Author"
    assert chapters[0].source_url == "https://legacy.example.org/chapter"
    assert body == "Legacy chapter text."
    assert all(instance.closed for instance in LegacyCrawler.instances)


def test_pyease_grpc_message_decoder_supports_installed_protobuf():
    from backend.adapters.lncrawl_bridge import (
        _configure_pyease_grpc_protobuf_compat,
    )

    protocol = pytest.importorskip("pyease_grpc._protocol")
    struct_pb2 = pytest.importorskip("google.protobuf.struct_pb2")

    _configure_pyease_grpc_protobuf_compat()
    assert protocol.message_to_dict(struct_pb2.Struct()) == {}


def test_novellive_com_mirror_is_registered_and_canonicalized():
    from backend.adapters import register_standard_adapters
    from backend.adapters.base import AdapterRegistry

    register_standard_adapters()
    url = "https://novellive.com/book/the-world-after-the-fall"
    adapter = AdapterRegistry.find(url)
    if adapter is None or adapter.site_id != "lncrawl_en_n_novellive":
        pytest.skip("The optional LightNovelCrawler sources are unavailable")

    crawler = adapter.crawler_cls.__new__(adapter.crawler_cls)
    assert crawler.canonical_url(url) == (
        "https://novellive.app/book/the-world-after-the-fall"
    )


@pytest.mark.asyncio
async def test_kolnovel_bridge_skips_pdf_links_for_duplicate_chapters():
    from backend.adapters.lncrawl_bridge import LNCrawlBridgeAdapter

    class KolnovelCrawler(LegacyCrawler):
        base_url = ["https://kolnovel.com/"]

        def read_novel(self):
            self.novel = SimpleNamespace(
                title="Novel",
                chapters=[
                    {"url": "/book/chapter-1/pdf", "title": "Chapter 1 PDF"},
                    {"url": "/book/chapter-1", "title": "الفصل الأول"},
                    {"url": "/book/chapter-2/pdf", "title": "Chapter 2 PDF"},
                    {"url": "/book/chapter-2", "title": "الفصل الثاني"},
                ],
            )

    adapter = LNCrawlBridgeAdapter(
        KolnovelCrawler,
        "lncrawl_ar_kolnovel",
        FakeNovel,
        FakeChapter,
        source_language="Arabic",
    )

    chapters = await adapter.get_chapter_list("https://kolnovel.com/novel")

    assert [chapter.source_url for chapter in chapters] == [
        "https://kolnovel.com/book/chapter-1",
        "https://kolnovel.com/book/chapter-2",
    ]
    assert [chapter.index for chapter in chapters] == [0, 1]


def test_arabic_lncrawl_source_is_marked_native():
    from backend.adapters.lncrawl_bridge import LNCrawlBridgeAdapter

    class ArabicSourceCrawler(LegacyCrawler):
        language = "English"

    adapter = LNCrawlBridgeAdapter(
        ArabicSourceCrawler,
        "lncrawl_ar_test",
        FakeNovel,
        FakeChapter,
        source_language="Arabic",
    )

    assert adapter.source_language == "Arabic"
    assert adapter.is_native_arabic is True


def test_lncrawl_bridge_uses_declared_language_when_source_language_is_unknown():
    from backend.adapters.lncrawl_bridge import LNCrawlBridgeAdapter

    class ArabicSourceCrawler(LegacyCrawler):
        language = "Arabic"

    adapter = LNCrawlBridgeAdapter(
        ArabicSourceCrawler,
        "lncrawl_ar_test",
        FakeNovel,
        FakeChapter,
    )

    assert adapter.source_language == "Arabic"
    assert adapter.is_native_arabic is True


def test_declared_arabic_language_overrides_english_source_folder():
    from backend.adapters.base import AdapterRegistry
    from backend.adapters.lncrawl_bridge import LNCrawlBridgeAdapter

    class ArabicSourceCrawler(LegacyCrawler):
        language = "Arabic"

    adapter = LNCrawlBridgeAdapter(
        ArabicSourceCrawler,
        "lncrawl_ar_test",
        FakeNovel,
        FakeChapter,
        source_language="English",
    )

    assert adapter.source_language == "Arabic"
    assert adapter.is_native_arabic is True
    previous = AdapterRegistry._adapters.get(adapter.site_id)
    try:
        AdapterRegistry.register(adapter)
        public_entry = next(
            entry
            for entry in AdapterRegistry.list_all()
            if entry["site_id"] == adapter.site_id
        )
        assert public_entry["source_language"] == "Arabic"
        assert public_entry["is_native_arabic"] is True
    finally:
        if previous is None:
            AdapterRegistry._adapters.pop(adapter.site_id, None)
        else:
            AdapterRegistry._adapters[adapter.site_id] = previous


def test_web_bridge_exposes_native_arabic_adapter_and_novel_language(monkeypatch):
    from backend.adapters.base import AdapterRegistry
    from backend.adapters.lncrawl_bridge import LNCrawlBridgeAdapter
    import gui.web_bridge as web_bridge

    class ArabicSourceCrawler(LegacyCrawler):
        language = "Arabic"

    adapter = LNCrawlBridgeAdapter(
        ArabicSourceCrawler,
        "lncrawl_ar_test_snapshot",
        FakeNovel,
        FakeChapter,
        source_language="Arabic",
    )
    previous = AdapterRegistry._adapters.get(adapter.site_id)
    AdapterRegistry.register(adapter)
    monkeypatch.setattr(web_bridge, "get_all_novels", lambda: [])
    monkeypatch.setattr(web_bridge, "get_all_glossary_rules", lambda: [])
    monkeypatch.setattr(web_bridge, "get_chapters", lambda _novel_id: [])
    novel = SimpleNamespace(
        id=1,
        title="Arabic Novel",
        author="",
        source_url="https://arabic.example/novel",
        source_site=adapter.site_id,
        cover_url="",
        description="",
        status="scraped",
        created_at=SimpleNamespace(isoformat=lambda: "2026-01-01T00:00:00"),
    )
    try:
        novel_data = web_bridge.NovelBridgeWebChannel._novel_data(novel)
        snapshot_adapter = next(
            item
            for item in web_bridge.NovelBridgeWebChannel().snapshot()["adapters"]
            if item["site_id"] == adapter.site_id
        )
        assert novel_data["is_native_arabic"] is True
        assert snapshot_adapter["source_language"] == "Arabic"
        assert snapshot_adapter["is_native_arabic"] is True
    finally:
        if previous is None:
            AdapterRegistry._adapters.pop(adapter.site_id, None)
        else:
            AdapterRegistry._adapters[adapter.site_id] = previous


def test_register_lncrawl_adapters_discovers_source_modules(tmp_path, monkeypatch):
    from backend.adapters.base import AdapterRegistry
    from backend.adapters.lncrawl_bridge import register_lncrawl_adapters

    checkout = tmp_path / "lncrawl-checkout"
    core = checkout / "lncrawl" / "core"
    source_dir = checkout / "sources" / "en" / "f"
    arabic_source_dir = checkout / "sources" / "ar"
    french_source_dir = checkout / "sources" / "fr"
    core.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    arabic_source_dir.mkdir(parents=True)
    french_source_dir.mkdir(parents=True)
    (checkout / "lncrawl" / "__init__.py").write_text("", encoding="utf-8")
    (core / "__init__.py").write_text("", encoding="utf-8")
    (core / "crawler.py").write_text(
        "class Crawler:\n"
        "    def read_novel(self, novel): pass\n"
        "    def download_chapter(self, chapter): pass\n",
        encoding="utf-8",
    )
    (core / "models.py").write_text(
        "class Novel:\n"
        "    def __init__(self, url): self.url = url\n"
        "class Chapter:\n"
        "    def __init__(self, id, url, title): "
        "self.id, self.url, self.title, self.body = id, url, title, None\n",
        encoding="utf-8",
    )
    (source_dir / "free.fullnovel.py").write_text(
        "from lncrawl.core.crawler import Crawler\n"
        "class FreeFullNovelCrawler(Crawler):\n"
        "    base_url = ['https://freefullnovel.com/']\n",
        encoding="utf-8",
    )
    for source in ("kolnovel", "riwaytclub", "arnovel"):
        (arabic_source_dir / f"{source}.py").write_text(
            "from lncrawl.core.crawler import Crawler\n"
            "class ArabicCrawler(Crawler):\n"
            "    language = 'English'\n"
            f"    base_url = ['https://{source}.example.org/']\n",
            encoding="utf-8",
        )
    (french_source_dir / "not_registered.py").write_text(
        "from lncrawl.core.crawler import Crawler\n"
        "class FrenchCrawler(Crawler):\n"
        "    base_url = ['https://french.example.org/']\n",
        encoding="utf-8",
    )

    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "lncrawl" or name.startswith("lncrawl.") or name == "sources" or name.startswith("sources.")
    }
    for name in saved_modules:
        sys.modules.pop(name, None)
    original_adapters = AdapterRegistry._adapters
    monkeypatch.setattr(AdapterRegistry, "_adapters", {})
    checkout_str = str(checkout.resolve())
    try:
        assert register_lncrawl_adapters(checkout) == 4
        adapter = AdapterRegistry.find("https://freefullnovel.com/book/test")
        assert adapter is not None
        assert adapter.site_id == "lncrawl_en_f_free_fullnovel"
        for source in ("kolnovel", "riwaytclub", "arnovel"):
            adapter = AdapterRegistry.find(f"https://{source}.example.org/book/test")
            assert adapter is not None
            assert adapter.source_language == "Arabic"
            assert adapter.is_native_arabic is True
        assert AdapterRegistry.find("https://french.example.org/book/test") is None
    finally:
        sys.path.remove(checkout_str)
        for name in list(sys.modules):
            if name == "lncrawl" or name.startswith("lncrawl.") or name == "sources" or name.startswith("sources."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        AdapterRegistry._adapters = original_adapters


def test_register_lncrawl_adapters_loads_sources_from_separate_directory(
    tmp_path, monkeypatch
):
    from backend.adapters.base import AdapterRegistry
    from backend.adapters.lncrawl_bridge import register_lncrawl_adapters

    checkout = tmp_path / "lncrawl-checkout"
    core = checkout / "lncrawl" / "core"
    sources = tmp_path / "other sources" / "lightnovel-crawler-sources"
    source_dir = sources / "en"
    core.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    (checkout / "lncrawl" / "__init__.py").write_text("", encoding="utf-8")
    (core / "__init__.py").write_text("", encoding="utf-8")
    (core / "crawler.py").write_text(
        "from lncrawl.config import Config\n"
        "class Crawler:\n"
        "    def __init__(self): self.sources = Config().crawler.local_sources\n"
        "    def read_novel(self, novel): pass\n"
        "    def download_chapter(self, chapter): pass\n",
        encoding="utf-8",
    )
    (checkout / "lncrawl" / "config.py").write_text(
        "from functools import cached_property\n"
        "from pathlib import Path\n"
        "ROOT_DIR = Path(__file__).parent\n"
        "class CrawlerConfig:\n"
        "    @cached_property\n"
        "    def local_sources(self): raise ValueError('No local sources')\n"
        "    @cached_property\n"
        "    def local_index_file(self): return self.local_sources / '_index.json'\n"
        "class Config:\n"
        "    def __init__(self):\n"
        "        self.crawler = CrawlerConfig()\n"
        "        self.crawler.local_index_file\n",
        encoding="utf-8",
    )
    (core / "models.py").write_text(
        "class Novel:\n"
        "    def __init__(self, url): self.url = url\n"
        "class Chapter:\n"
        "    def __init__(self, id, url, title): "
        "self.id, self.url, self.title, self.body = id, url, title, None\n",
        encoding="utf-8",
    )
    (source_dir / "example.py").write_text(
        "from lncrawl.core.crawler import Crawler\n"
        "class ExampleCrawler(Crawler):\n"
        "    base_url = [\n"
        "        'https://separate.example.org/',\n"
        "        'https://novellive.app/',\n"
        "        'https://wuxiaworld.com/',\n"
        "    ]\n",
        encoding="utf-8",
    )

    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "lncrawl" or name.startswith("lncrawl.")
    }
    for name in saved_modules:
        sys.modules.pop(name, None)
    original_adapters = AdapterRegistry._adapters
    monkeypatch.setattr(AdapterRegistry, "_adapters", {})
    monkeypatch.delenv("LNCRAWL_SOURCES_ROOT", raising=False)
    checkout_str = str(checkout.resolve())
    try:
        assert register_lncrawl_adapters(checkout) == 1
        adapter = AdapterRegistry.find("https://separate.example.org/book/test")
        assert adapter is not None
        assert adapter.site_id == "lncrawl_en_example"
        assert AdapterRegistry.find("https://novellive.app/book/test") is adapter
        assert AdapterRegistry.find("https://wuxiaworld.com/novel/test") is adapter
        registered = next(
            item
            for item in AdapterRegistry.list_all()
            if item["site_id"] == adapter.site_id
        )
        assert "https://novellive.app/" in registered["base_urls"]
        assert adapter.crawler_cls().sources == sources.resolve()
    finally:
        if checkout_str in sys.path:
            sys.path.remove(checkout_str)
        for name in list(sys.modules):
            if name == "lncrawl" or name.startswith("lncrawl."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        AdapterRegistry._adapters = original_adapters
