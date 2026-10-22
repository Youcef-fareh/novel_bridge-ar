"""Regression tests for Phase 1 bug fixes (data corruption)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from backend.models import GlossaryRule
from backend.translation.base import apply_glossary_postpass


# ── 1.1 Empty glossary terms must not corrupt text ─────────────────────────────

def test_glossary_postpass_skips_empty_source_term():
    rule = GlossaryRule(source_term="", target_term="تدمير", notes="", is_global=True)
    assert apply_glossary_postpass("hello world", [rule]) == "hello world"


def test_glossary_postpass_skips_whitespace_source_term():
    rule = GlossaryRule(source_term="   ", target_term="تدمير", notes="", is_global=True)
    assert apply_glossary_postpass("hello world", [rule]) == "hello world"


def test_glossary_postpass_skips_noop_rule():
    rule = GlossaryRule(source_term="same", target_term="same", notes="", is_global=True)
    assert apply_glossary_postpass("same word same", [rule]) == "same word same"


def test_glossary_api_rejects_empty_terms():
    from fastapi.testclient import TestClient
    from backend.main import app

    client = TestClient(app)
    response = client.post("/glossary", json={"source_term": "", "target_term": "x"})
    assert response.status_code == 422
    response = client.post("/glossary", json={"source_term": "x", "target_term": "   "})
    assert response.status_code == 422


def test_add_glossary_rule_rejects_empty_source():
    from backend.database import add_glossary_rule

    with pytest.raises(ValueError):
        add_glossary_rule("", "x")
    with pytest.raises(ValueError):
        add_glossary_rule("x", "  ")


# ── 1.2 Target term is always literal, never a regex template ─────────────────

@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (r"\1", r"\1"),
        (r"\n", r"\n"),
        (r"\g<0>", r"\g<0>"),
        (r"\\", r"\\"),
        (r"bad\qescape", r"bad\qescape"),
    ],
)
def test_glossary_postpass_treats_target_as_literal(target, expected):
    rule = GlossaryRule(source_term="John", target_term=target, notes="", is_global=True)
    result = apply_glossary_postpass("John met John again.", [rule])
    assert result == f"{expected} met {expected} again."


def test_glossary_postpass_normal_replacement_still_works():
    rule = GlossaryRule(source_term="John", target_term="جون", notes="", is_global=True)
    result = apply_glossary_postpass("John met john again.", [rule])
    assert result == "جون met جون again."


# ── 1.3 Output directory comes from config, single source of truth ────────────

def test_output_path_prefers_explicit_env(monkeypatch):
    from backend.config import output_path

    monkeypatch.setenv("OUTPUT_DIR", str(Path(__file__).parent))
    assert output_path() == Path(__file__).parent.resolve()


def test_output_path_relative_is_base_dir_anchored(monkeypatch):
    from backend import config as config_module

    monkeypatch.setenv("OUTPUT_DIR", "rel-output")
    monkeypatch.setattr(config_module, "dotenv_values", lambda *_args, **_kwargs: {})
    resolved = config_module.output_path()
    assert resolved.is_absolute()
    assert resolved == (config_module.base_dir() / "rel-output").resolve()


def test_epub_build_and_download_validation_agree(tmp_path, monkeypatch):
    """pipeline writer and download validation must use the same directory."""
    from backend.config import output_path, resolve_path

    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    assert output_path() == tmp_path.resolve()
    # resolve_path on an absolute path is a no-op resolve
    assert resolve_path(tmp_path) == tmp_path.resolve()


# ── 1.4 <br> handling preserves line breaks, identical across backends ────────

_BR_HTML = (
    '<div id="c">'
    "<p>Para one line a<br>line b</p>"
    "<p>Second para</p>"
    "<br>Line two<br><br>Line three"
    "</div>"
)


@pytest.mark.parametrize("selectolax_on", [True, False])
def test_parse_chapter_body_br_newlines(selectolax_on, monkeypatch):
    import backend.adapters.fetcher as fetcher

    monkeypatch.setattr(fetcher, "_SELECTOLAX_AVAILABLE", selectolax_on)
    result = fetcher.parse_chapter_body(_BR_HTML, ["#c"])
    assert "line a\nline b" in result
    # No glued words anywhere
    assert "aline" not in result


@pytest.mark.parametrize("selectolax_on", [True, False])
def test_parse_chapter_body_br_without_paragraphs(selectolax_on, monkeypatch):
    import backend.adapters.fetcher as fetcher

    monkeypatch.setattr(fetcher, "_SELECTOLAX_AVAILABLE", selectolax_on)
    html = '<div id="c">No paragraph here<br>second line<br><br>third line</div>'
    result = fetcher.parse_chapter_body(html, ["#c"])
    assert result == "No paragraph here\nsecond line\n\nthird line"


def test_parse_chapter_body_backends_agree(monkeypatch):
    import backend.adapters.fetcher as fetcher

    monkeypatch.setattr(fetcher, "_SELECTOLAX_AVAILABLE", True)
    on = fetcher.parse_chapter_body(_BR_HTML, ["#c"])
    monkeypatch.setattr(fetcher, "_SELECTOLAX_AVAILABLE", False)
    off = fetcher.parse_chapter_body(_BR_HTML, ["#c"])
    assert on == off


# ── 1.5 WTRLab redirect guard validates the FINAL url, not the input ──────────

@pytest.mark.asyncio
async def test_wtrlab_guard_rejects_silent_redirect(monkeypatch):
    import backend.adapters.wtrlab as wtr

    adapter = wtr.WTRLabAdapter()

    async def fake_fetch(url, **kwargs):
        # Browser landed on a different novel entirely.
        return "<html></html>", "https://wtrlab.com/novel/99999/some-other-novel/chapter-1"

    monkeypatch.setattr(wtr, "fetch_html_nodriver", fake_fetch)

    with pytest.raises(RuntimeError, match="another novel"):
        await adapter._fetch_rendered_chapter_html(
            "https://wtrlab.com/novel/12345/expected-novel/chapter-1",
            "12345",
            1,
        )


@pytest.mark.asyncio
async def test_wtrlab_guard_accepts_matching_final_url(monkeypatch):
    import backend.adapters.wtrlab as wtr

    adapter = wtr.WTRLabAdapter()

    async def fake_fetch(url, **kwargs):
        return "<html>ok</html>", url

    monkeypatch.setattr(wtr, "fetch_html_nodriver", fake_fetch)

    html = await adapter._fetch_rendered_chapter_html(
        "https://wtrlab.com/novel/12345/expected-novel/chapter-3",
        "12345",
        3,
    )
    assert html == "<html>ok</html>"


@pytest.mark.asyncio
async def test_wtrlab_guard_rejects_chapter_redirect(monkeypatch):
    import backend.adapters.wtrlab as wtr

    adapter = wtr.WTRLabAdapter()

    async def fake_fetch(url, **kwargs):
        return "<html></html>", "https://wtrlab.com/novel/12345/expected-novel/chapter-99"

    monkeypatch.setattr(wtr, "fetch_html_nodriver", fake_fetch)

    with pytest.raises(RuntimeError, match="another chapter"):
        await adapter._fetch_rendered_chapter_html(
            "https://wtrlab.com/novel/12345/expected-novel/chapter-1",
            "12345",
            1,
        )
