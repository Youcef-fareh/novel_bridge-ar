"""Regression coverage for Phase 4 hardening and diagnostics."""
from __future__ import annotations

import hashlib
import io
import logging
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

def test_scrubber_counts_all_artifact_types_but_preserves_fresh_files(tmp_path, monkeypatch):
    from backend import scrubber

    old_epub = tmp_path / "old.epub"
    old_pdf = tmp_path / "old.pdf"
    fresh_txt = tmp_path / "fresh.txt"
    old_epub.write_bytes(b"epub")
    old_pdf.write_bytes(b"pdf!")
    fresh_txt.write_bytes(b"fresh")
    old_timestamp = 1_600_000_000
    import os

    os.utime(old_epub, (old_timestamp, old_timestamp))
    os.utime(old_pdf, (old_timestamp, old_timestamp))
    monkeypatch.setattr(scrubber, "get_config", lambda: SimpleNamespace(output_max_mb=0))
    monkeypatch.setattr(scrubber, "output_path", lambda: tmp_path)

    removed, removed_bytes = scrubber.scrub_output(min_age_seconds=1)

    assert removed == 2
    assert removed_bytes == 8
    assert fresh_txt.exists()
    assert not old_epub.exists()
    assert not old_pdf.exists()


def test_env_writer_updates_duplicate_keys(tmp_path, monkeypatch):
    from gui.widgets import api_keys

    env_file = tmp_path / ".env"
    monkeypatch.setattr(api_keys, "ENV_FILE", env_file)
    env_file.write_text("GROQ_API_KEY=old\n# keep\nGROQ_API_KEY=older\n", encoding="utf-8")

    api_keys._write_env_file({"GROQ_API_KEY": "new"})

    assert api_keys._read_env_file()["GROQ_API_KEY"] == "new"
    assert env_file.read_text(encoding="utf-8").count("GROQ_API_KEY=new") == 2


def test_checksum_parser_selects_asset_line_or_digest_only():
    from backend.update_manager import _checksum_for_asset

    digest = hashlib.sha256(b"archive").hexdigest()
    checksums = (
        f"{'0' * 64}  other.exe\n{digest} *NovelBridgeAR_Setup.exe\n"
    )

    assert _checksum_for_asset(checksums, "NovelBridgeAR_Setup.exe") == digest
    assert _checksum_for_asset(digest + "\n", "NovelBridgeAR_Setup.exe") == digest
    assert _checksum_for_asset(f"{digest} other.exe\n", "NovelBridgeAR_Setup.exe") is None


def test_jpg_cover_mime_alias_is_supported():
    from backend.epub_builder import _cover_extension

    assert _cover_extension("image/jpg", b"\xff\xd8\xffdata") == ".jpg"


def test_logging_rotates_and_diagnostics_archive_contains_system_files(tmp_path, monkeypatch):
    from backend import logging_setup
    from backend.config import Config, DiagnosticsConfig, PathsConfig, CrawlerConfig, TranslatorConfig, CalibreConfig

    config = Config(
        crawler=CrawlerConfig(3, 2, 5, 30.0, 50),
        translator=TranslatorConfig(900.0, True),
        calibre=CalibreConfig("ebook-convert", False, "", True),
        paths=PathsConfig(tmp_path / "db.sqlite", tmp_path / "output", tmp_path / "data"),
        diagnostics=DiagnosticsConfig(True, "DEBUG"),
        output_max_mb=10,
        api_port=8000,
    )
    monkeypatch.setattr(logging_setup, "get_config", lambda: config)
    monkeypatch.setattr(logging_setup, "_LOG_MAX_BYTES", 128)
    monkeypatch.setattr(logging_setup, "_LOG_BACKUP_COUNT", 2)
    logging_setup.setup_logging()
    test_logger = logging.getLogger("novelbridge.test.rotate")
    for index in range(20):
        test_logger.error("diagnostic line %s %s", index, "x" * 40)
    for handler in logging.getLogger().handlers:
        if getattr(handler, "_novelbridge_handler", False):
            handler.flush()

    assert (tmp_path / "data" / "logs" / "novelbridge.log").exists()
    assert (tmp_path / "data" / "logs" / "novelbridge.log.1").exists()
    archive = zipfile.ZipFile(io.BytesIO(logging_setup.diagnostics_archive()))
    assert "VERSION" in archive.namelist()
    assert "system-info.json" in archive.namelist()
    assert any(name.startswith("logs/novelbridge.log") for name in archive.namelist())

    for handler in list(logging.getLogger().handlers):
        if getattr(handler, "_novelbridge_handler", False) or getattr(
            handler, "_novelbridge_console", False
        ):
            logging.getLogger().removeHandler(handler)
            handler.close()


def test_diagnostics_export_is_gated_by_setting(monkeypatch):
    from fastapi.testclient import TestClient
    import backend.main as main

    monkeypatch.setattr(
        main,
        "get_config",
        lambda: SimpleNamespace(diagnostics=SimpleNamespace(allow_export=False)),
    )
    client = TestClient(main.app)

    response = client.get("/diagnostics/logs/export")

    assert response.status_code == 403
    assert response.json()["detail"] == "Export disabled in settings"


def test_diagnostics_export_returns_a_zip_when_enabled(monkeypatch):
    from fastapi.testclient import TestClient
    import backend.main as main

    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as bundle:
        bundle.writestr("VERSION", "test")
    monkeypatch.setattr(
        main,
        "get_config",
        lambda: SimpleNamespace(diagnostics=SimpleNamespace(allow_export=True)),
    )
    monkeypatch.setattr(main, "diagnostics_archive", lambda: payload.getvalue())

    response = TestClient(main.app).get("/diagnostics/logs/export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert zipfile.ZipFile(io.BytesIO(response.content)).namelist() == ["VERSION"]


def test_diagnostics_settings_persist_and_apply_to_environment(tmp_path, monkeypatch):
    from backend import config

    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "settings.json"))
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("DIAGNOSTICS_ALLOW_EXPORT", raising=False)
    monkeypatch.setattr(config, "dotenv_values", lambda *_args, **_kwargs: {})

    result = config.update_diagnostics_settings(False, "DEBUG")

    assert result.diagnostics.allow_export is False
    assert result.diagnostics.log_level == "DEBUG"
    assert config.get_config().diagnostics == result.diagnostics


def test_fastapi_lifespan_initializes_and_stops_services(monkeypatch):
    import asyncio
    import backend.main as main

    lifecycle: list[str] = []
    monkeypatch.setattr(main, "setup_logging", lambda: lifecycle.append("logging"))
    monkeypatch.setattr(main, "get_config", lambda: SimpleNamespace())
    monkeypatch.setattr(main, "init_db", lambda: lifecycle.append("database"))
    monkeypatch.setattr(
        main,
        "register_standard_adapters",
        lambda: lifecycle.append("adapters"),
    )
    monkeypatch.setattr(
        main,
        "scheduler",
        SimpleNamespace(
            start=lambda: lifecycle.append("start"),
            stop=lambda _timeout: lifecycle.append("stop"),
        ),
    )

    async def run_lifespan():
        async with main.lifespan(main.app):
            lifecycle.append("running")

    asyncio.run(run_lifespan())

    assert lifecycle == ["logging", "database", "adapters", "start", "running", "stop"]


def test_concurrent_image_replacement_never_unlinks_current_image(tmp_path, monkeypatch):
    import asyncio
    import hashlib
    from backend import chapter_images

    records: list[SimpleNamespace] = []
    monkeypatch.setattr(
        chapter_images,
        "get_config",
        lambda: SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path)),
    )
    monkeypatch.setattr(
        chapter_images,
        "get_chapter_images",
        lambda _chapter_id: list(records),
    )

    def replace(_chapter_id, new_records):
        records[:] = [SimpleNamespace(**record) for record in new_records]
        return list(records)

    async def download(_client, url):
        await asyncio.sleep(0.01 if url.endswith("first") else 0)
        data = url.rsplit("/", 1)[-1].encode()
        return data, "image/png"

    monkeypatch.setattr(chapter_images, "replace_chapter_images", replace)
    monkeypatch.setattr(chapter_images, "_download_image", download)

    async def replace_concurrently():
        await asyncio.gather(
            chapter_images.store_chapter_images(
                1, 901, [{"url": "https://images.test/first"}]
            ),
            chapter_images.store_chapter_images(
                1, 901, [{"url": "https://images.test/second"}]
            ),
        )

    asyncio.run(replace_concurrently())

    assert len(records) == 1
    current_path = Path(records[0].file_path)
    assert current_path.exists()
    assert current_path.name.startswith(hashlib.sha256(b"first").hexdigest())
