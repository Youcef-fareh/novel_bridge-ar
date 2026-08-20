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


def test_gemini_provider_uses_instance_key(monkeypatch):
    import sys
    import types

    import backend.translation.gemini_provider as gemini_module

    captured = {}

    class FakeGenerateContentConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeResponse:
        text = "translated"

    class FakeModels:
        @staticmethod
        def generate_content(**kwargs):
            return FakeResponse()

    class FakeClient:
        def __init__(self, api_key):
            captured["api_key"] = api_key

        models = FakeModels()

    fake_google = types.ModuleType("google")
    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = FakeClient
    fake_genai.types = types.SimpleNamespace(GenerateContentConfig=FakeGenerateContentConfig)
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



