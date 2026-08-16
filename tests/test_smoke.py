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


# ── Test: database init ────────────────────────────────────────────────────────
def test_db_init(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    # Re-import to pick up env var
    import importlib
    import backend.database as db_module
    importlib.reload(db_module)
    db_module.init_db()
    assert (tmp_path / "test.db").exists()


# ── Test: adapter registry ─────────────────────────────────────────────────────
def test_adapter_registry():
    from backend.adapters.base import AdapterRegistry
    from backend.adapters.galaxynovels import GalaxyNovelsAdapter
    from backend.adapters.novelfire import NovelFireAdapter
    from backend.adapters.novelphoenix import NovelPhoenixAdapter
    from backend.adapters.wtrlab import WTRLabAdapter

    AdapterRegistry.register(NovelFireAdapter())
    AdapterRegistry.register(WTRLabAdapter())
    AdapterRegistry.register(NovelPhoenixAdapter())
    AdapterRegistry.register(GalaxyNovelsAdapter())

    nf = AdapterRegistry.find("https://novelfire.net/book/test")
    assert nf is not None
    assert nf.site_id == "novelfire"

    wtr = AdapterRegistry.find("https://wtr-lab.com/series/test")
    assert wtr is not None
    assert wtr.site_id == "wtrlab"

    np = AdapterRegistry.find("https://novelphoenix.com/novel/test")
    assert np is not None
    assert np.site_id == "novelphoenix"

    gn = AdapterRegistry.find("https://galaxynovels.com/novel/unscientific-beast-taming/")
    assert gn is not None
    assert gn.site_id == "galaxynovels"

    unknown = AdapterRegistry.find("https://some-random-site.com")
    assert unknown is None


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
