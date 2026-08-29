"""QWebChannel bridge for the HTML NovelBridge frontend."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QDialog, QInputDialog, QLineEdit, QMessageBox

from backend.adapters.base import AdapterRegistry
from backend.database import (
    add_glossary_rule,
    delete_glossary_rule,
    delete_novel,
    get_all_glossary_rules,
    get_all_novels,
    get_chapters,
    get_pending_translation_chapters,
    get_novel,
    update_novel,
)
from backend.models import ChapterStatus
from backend.pipeline import JobControl, run_epub_job, run_scrape_job, run_translation_job
from backend.translation import GeminiProvider, GroqProvider, OrcaRouterProvider, TokenRouterProvider
from gui.widgets.api_keys import ENV_FILE, _PROVIDER_GROUPS, _read_env_file, _write_env_file


class _AsyncWorkerSignals(QObject):
    finished = pyqtSignal(object)
    cancelled = pyqtSignal()
    error = pyqtSignal(str)


class _AsyncWorker(QRunnable):
    def __init__(self, coroutine_factory):
        super().__init__()
        self.signals = _AsyncWorkerSignals()
        self._coroutine_factory = coroutine_factory

    @pyqtSlot()
    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(self._coroutine_factory())
            self.signals.finished.emit(result)
        except asyncio.CancelledError:
            self.signals.cancelled.emit()
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            loop.close()


class NovelBridgeWebChannel(QObject):
    """Expose backend-backed operations to the embedded HTML frontend."""

    state_changed = pyqtSignal(str)
    job_progress = pyqtSignal(int, int, str)
    job_finished = pyqtSignal(str)
    job_error = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread_pool = QThreadPool.globalInstance()
        self._job_control: Optional[JobControl] = None

    @staticmethod
    def _novel_data(novel) -> dict:
        chapters = get_chapters(novel.id)
        return {
            "id": novel.id,
            "title": novel.title,
            "author": novel.author or "",
            "source_url": novel.source_url,
            "source_site": novel.source_site,
            "cover_url": novel.cover_url or "",
            "description": novel.description or "",
            "status": novel.status.value if hasattr(novel.status, "value") else str(novel.status),
            "chapters": [
                {
                    "id": chapter.id,
                    "index": chapter.index,
                    "title": chapter.title,
                    "source_url": chapter.source_url,
                    "status": chapter.status.value if hasattr(chapter.status, "value") else str(chapter.status),
                    "has_translation": bool(chapter.translated_text),
                }
                for chapter in chapters
            ],
        }

    @staticmethod
    def _provider_data() -> list[dict]:
        env = _read_env_file()
        groups = list(_PROVIDER_GROUPS)
        known = {prefix for _name, prefix, *_rest in groups}
        suffixes = ("_API_KEY", "_BASE_URL", "_MODELS", "_MODEL")
        custom = set()
        for key in env:
            for suffix in suffixes:
                if key.endswith(suffix):
                    prefix = key[: -len(suffix)]
                    if prefix and not prefix.endswith(("_API", "_BASE")):
                        custom.add(prefix)
                    break
        for prefix in sorted(custom - known):
            groups.append((prefix.replace("_", " ").title(), prefix, f"{prefix}_API_KEY", f"{prefix}_BASE_URL", ""))

        providers = []
        for name, prefix, key_var, base_var, default_base in groups:
            model_values = [value.strip() for value in env.get(f"{prefix}_MODELS", "").split(",") if value.strip()]
            default_model = env.get(f"{prefix}_MODEL", "").strip()
            if default_model and default_model not in model_values:
                model_values.insert(0, default_model)
            providers.append({
                "name": name,
                "prefix": prefix,
                "key": env.get(key_var, ""),
                "configured": bool(env.get(key_var, "").strip()),
                "base_url": env.get(base_var, default_base),
                "models": model_values,
            })
        return providers

    def snapshot(self) -> dict:
        return {
            "novels": [self._novel_data(novel) for novel in get_all_novels()],
            "glossary": [
                {"id": rule.id, "source_term": rule.source_term, "target_term": rule.target_term, "notes": rule.notes or ""}
                for rule in get_all_glossary_rules()
            ],
            "providers": self._provider_data(),
            "adapters": [
                {"site_id": item["site_id"], "class_name": item["class"], "source": "Built-in"}
                for item in AdapterRegistry.list_all()
                if item["site_id"] != "galaxynovels"
            ],
        }

    @pyqtSlot(result=str)
    def get_snapshot(self) -> str:
        return json.dumps(self.snapshot(), ensure_ascii=False)

    @pyqtSlot(str, result=str)
    def add_novel(self, url: str) -> str:
        adapter = AdapterRegistry.find(url.strip())
        if not adapter:
            return json.dumps({"ok": False, "error": "No adapter recognizes this URL."})
        from backend.database import create_novel
        novel = create_novel(url.strip(), adapter.site_id)
        self.state_changed.emit("novels")
        return json.dumps({"ok": True, "id": novel.id})

    @pyqtSlot(int, result=bool)
    def remove_novel(self, novel_id: int) -> bool:
        result = delete_novel(novel_id)
        if result:
            self.state_changed.emit("novels")
        return result

    @pyqtSlot(str, str, str, result=str)
    def add_glossary(self, source_term: str, target_term: str, notes: str = "") -> str:
        if not source_term.strip() or not target_term.strip():
            return json.dumps({"ok": False, "error": "Both terms are required."})
        rule = add_glossary_rule(source_term.strip(), target_term.strip(), notes.strip())
        self.state_changed.emit("glossary")
        return json.dumps({"ok": True, "id": rule.id})

    @pyqtSlot(int, result=bool)
    def remove_glossary(self, rule_id: int) -> bool:
        result = delete_glossary_rule(rule_id)
        if result:
            self.state_changed.emit("glossary")
        return result

    @pyqtSlot(int, result=bool)
    def start_scrape(self, novel_id: int) -> bool:
        return self._start_job("scrape", novel_id)

    @pyqtSlot(int, str, str, result=bool)
    def start_translation(self, novel_id: int, provider: str, model: str) -> bool:
        return self._start_job("translate", novel_id, provider, model)

    @pyqtSlot(int, str, str, str, result=bool)
    def start_selected_translation(self, novel_id: int, chapter_ids_json: str, provider: str, model: str) -> bool:
        if self._job_control is not None or not get_novel(novel_id):
            return False
        try:
            chapter_ids = [int(value) for value in json.loads(chapter_ids_json)]
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        self._job_control = JobControl()
        control = self._job_control

        async def work():
            return await run_translation_job(
                novel_id,
                chapter_ids=chapter_ids,
                provider_name=provider or None,
                model_name=model or None,
                progress_cb=self._progress,
                job_control=control,
            )

        worker = _AsyncWorker(work)
        worker.signals.finished.connect(lambda _result: self._job_done("translate"))
        worker.signals.cancelled.connect(lambda: self._job_done("cancelled"))
        worker.signals.error.connect(self._job_failed)
        self._thread_pool.start(worker)
        return True

    @pyqtSlot(int, str, result=bool)
    def delete_translations(self, novel_id: int, chapter_ids_json: str) -> bool:
        try:
            chapter_ids = {int(value) for value in json.loads(chapter_ids_json)}
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        changed = False
        for chapter in get_chapters(novel_id):
            if chapter.id in chapter_ids and chapter.translated_text:
                status = ChapterStatus.scraped if chapter.raw_text and chapter.raw_text.strip() else ChapterStatus.pending
                from backend.database import update_chapter
                update_chapter(chapter.id, translated_text=None, status=status)
                changed = True
        if changed:
            self.state_changed.emit("novels")
        return changed

    @pyqtSlot(int, result=bool)
    def build_epub(self, novel_id: int) -> bool:
        return self._start_job("epub", novel_id)

    @pyqtSlot(result=bool)
    def upload_epubs_to_drive(self) -> bool:
        if self._job_control is not None:
            return False
        from backend.google_drive import upload_epubs

        self._job_control = JobControl()
        control = self._job_control

        async def work():
            if control.is_cancelled:
                return 0
            return await asyncio.get_event_loop().run_in_executor(
                None,
                upload_epubs,
                Path(os.getenv("OUTPUT_DIR", "output")),
                self._progress,
            )

        worker = _AsyncWorker(work)
        worker.signals.finished.connect(lambda count: self._job_done("drive", count))
        worker.signals.cancelled.connect(lambda: self._job_done("cancelled"))
        worker.signals.error.connect(self._job_failed)
        self._thread_pool.start(worker)
        return True

    @pyqtSlot(str, int, str, str, result=bool)
    def start_schedule(self, novel_ids_json: str, chapter_limit: int, provider: str, model: str) -> bool:
        if self._job_control is not None:
            return False
        try:
            novel_ids = [int(value) for value in json.loads(novel_ids_json)]
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not novel_ids:
            return False
        self._job_control = JobControl()
        control = self._job_control

        async def work():
            for novel_id in novel_ids:
                pending = get_pending_translation_chapters(novel_id)
                if chapter_limit > 0:
                    pending = pending[:chapter_limit]
                if pending:
                    await run_translation_job(
                        novel_id,
                        chapter_ids=[chapter.id for chapter in pending],
                        provider_name=provider or None,
                        model_name=model or None,
                        progress_cb=self._progress,
                        job_control=control,
                    )
                    await run_epub_job(novel_id, progress_cb=self._progress)
            return True

        worker = _AsyncWorker(work)
        worker.signals.finished.connect(lambda _result: self._job_done("schedule"))
        worker.signals.cancelled.connect(lambda: self._job_done("cancelled"))
        worker.signals.error.connect(self._job_failed)
        self._thread_pool.start(worker)
        return True

    def _start_job(self, kind: str, novel_id: int, provider: str = "", model: str = "") -> bool:
        if self._job_control is not None:
            return False
        if not get_novel(novel_id):
            return False
        self._job_control = JobControl()
        control = self._job_control

        async def work():
            if kind == "scrape":
                return await run_scrape_job(novel_id, progress_cb=self._progress, job_control=control)
            if kind == "translate":
                return await run_translation_job(
                    novel_id,
                    provider_name=provider or None,
                    model_name=model or None,
                    progress_cb=self._progress,
                    job_control=control,
                )
            return await run_epub_job(novel_id, progress_cb=self._progress)

        worker = _AsyncWorker(work)
        worker.signals.finished.connect(lambda _result, k=kind: self._job_done(k))
        worker.signals.cancelled.connect(lambda: self._job_done("cancelled"))
        worker.signals.error.connect(self._job_failed)
        self._thread_pool.start(worker)
        return True

    def _progress(self, done: int, total: int, message: str) -> None:
        self.job_progress.emit(done, total, message)

    def _job_done(self, kind: str, result=None) -> None:
        self._job_control = None
        self.job_finished.emit(kind)
        self.state_changed.emit("novels")

    def _job_failed(self, message: str) -> None:
        self._job_control = None
        self.job_error.emit(message)
        self.state_changed.emit("novels")

    @pyqtSlot(result=bool)
    def cancel_job(self) -> bool:
        if not self._job_control:
            return False
        self._job_control.cancel()
        return True

    @pyqtSlot(result=bool)
    def pause_job(self) -> bool:
        if not self._job_control:
            return False
        self._job_control.pause()
        return True

    @pyqtSlot(result=bool)
    def resume_job(self) -> bool:
        if not self._job_control:
            return False
        self._job_control.resume()
        return True

    @pyqtSlot(result=bool)
    def start_api_server(self) -> bool:
        try:
            port = os.getenv("API_PORT", "8000")
            subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", port],
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
            )
            return True
        except OSError:
            return False

    @pyqtSlot(str, result=bool)
    def edit_provider_key(self, prefix: str) -> bool:
        value, ok = QInputDialog.getText(
            self.parent(),
            f"API Key - {prefix}",
            "API key:",
            QLineEdit.EchoMode.Normal,
        )
        if not ok:
            return False
        env = _read_env_file()
        env[f"{prefix}_API_KEY"] = value.strip()
        _write_env_file(env)
        for key, item in env.items():
            if item:
                os.environ[key] = item
        self.state_changed.emit("providers")
        return True

    @pyqtSlot(str, str, result=bool)
    def save_provider_key(self, prefix: str, value: str) -> bool:
        env = _read_env_file()
        env[f"{prefix}_API_KEY"] = value.strip()
        try:
            _write_env_file(env)
        except OSError:
            return False
        for key, item in env.items():
            if item:
                os.environ[key] = item
            else:
                os.environ.pop(key, None)
        self.state_changed.emit("providers")
        return True

    @pyqtSlot(str, str, str, result=bool)
    def save_provider(self, prefix: str, base_url: str, models_json: str) -> bool:
        try:
            models = [str(value).strip() for value in json.loads(models_json) if str(value).strip()]
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        env = _read_env_file()
        env[f"{prefix}_BASE_URL"] = base_url.strip()
        env[f"{prefix}_MODELS"] = ",".join(models)
        if models and env.get(f"{prefix}_MODEL") not in models:
            env[f"{prefix}_MODEL"] = models[0]
        elif not models:
            env.pop(f"{prefix}_MODEL", None)
        try:
            _write_env_file(env)
        except OSError:
            return False
        for key, value in env.items():
            if value:
                os.environ[key] = value
        self.state_changed.emit("providers")
        return True

    @pyqtSlot(str, str, result=str)
    def test_provider(self, prefix: str, model: str) -> str:
        """Make a minimal live request to verify provider credentials and model."""
        provider_classes = {
            "gemini": GeminiProvider,
            "groq": GroqProvider,
            "tokenrouter": TokenRouterProvider,
            "orcarouter": OrcaRouterProvider,
        }
        provider_class = provider_classes.get(prefix.casefold().strip())
        if provider_class is None:
            return json.dumps({"ok": False, "error": f"Unsupported provider: {prefix}."})

        provider = provider_class(model=model.strip() or None)
        if not provider.is_available():
            return json.dumps({"ok": False, "error": f"{prefix} API key is not configured."})

        selected_model = model.strip() or getattr(provider, "_model", "")
        try:
            response = provider._call_api(
                "Reply with the single word OK.",
                "Reply with the single word OK.",
                selected_model,
            )
            return json.dumps({"ok": bool(response.strip()), "model": selected_model})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})

    @pyqtSlot(result=bool)
    def add_provider(self) -> bool:
        from gui.widgets.api_keys import AddProviderDialog
        dialog = AddProviderDialog(self.parent())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        provider, prefix, api_key, base_url, model, language = dialog.get_values()
        env = _read_env_file()
        env[f"{prefix}_API_KEY"] = api_key
        env[f"{prefix}_BASE_URL"] = base_url
        env[f"{prefix}_MODEL"] = model
        env[f"{prefix}_MODELS"] = model
        env[f"{prefix}_LANGUAGE"] = language
        try:
            _write_env_file(env)
        except OSError:
            return False
        for key, value in env.items():
            if value:
                os.environ[key] = value
        self.state_changed.emit("providers")
        return True

    @pyqtSlot(str, str, str, str, str, str, result=str)
    def create_provider(self, name: str, prefix: str, api_key: str, base_url: str, model: str, language: str) -> str:
        import re
        name, prefix, base_url = name.strip(), prefix.strip().upper(), base_url.strip()
        if not name or not re.fullmatch(r"[A-Z][A-Z0-9_]*", prefix) or not base_url.startswith(("http://", "https://")):
            return json.dumps({"ok": False, "error": "Provider name, valid prefix, and HTTP(S) base URL are required."})
        env = _read_env_file()
        env[f"{prefix}_API_KEY"] = api_key.strip()
        env[f"{prefix}_BASE_URL"] = base_url
        env[f"{prefix}_MODEL"] = model.strip()
        env[f"{prefix}_MODELS"] = model.strip()
        env[f"{prefix}_LANGUAGE"] = language.strip() or "English"
        try:
            _write_env_file(env)
        except OSError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        for key, value in env.items():
            if value:
                os.environ[key] = value
        self.state_changed.emit("providers")
        return json.dumps({"ok": True})

    @pyqtSlot(str, result=str)
    def check_adapter(self, url: str) -> str:
        adapter = AdapterRegistry.find(url.strip())
        if not adapter:
            return json.dumps({"ok": False, "error": "No installed adapter recognizes this URL."})
        return json.dumps({
            "ok": True,
            "site_id": adapter.site_id,
            "class_name": type(adapter).__name__,
            "language": getattr(adapter, "source_language", "English"),
        })

    @pyqtSlot(str, str, str, result=str)
    def generate_adapter(self, url: str, method: str, language: str) -> str:
        from gui.widgets.website_setup import _CUSTOM_DIR, _adapter_template, load_custom_adapters
        from urllib.parse import urlparse
        import re

        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return json.dumps({"ok": False, "error": "Enter a complete HTTP or HTTPS URL."})
        if AdapterRegistry.find(url.strip()):
            return json.dumps({"ok": False, "error": "This website already has an installed adapter."})
        domain = parsed.netloc.lower().split(":", 1)[0]
        site_id = re.sub(r"[^a-z0-9]+", "_", domain.removeprefix("www.")).strip("_") or "custom_site"
        class_name = "".join(part.title() for part in site_id.split("_")) + "Adapter"
        target = _CUSTOM_DIR / f"{site_id}_adapter.py"
        try:
            _CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
            target.write_text(_adapter_template(site_id, class_name, domain, method, language), encoding="utf-8")
            load_custom_adapters()
        except OSError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        self.state_changed.emit("adapters")
        return json.dumps({"ok": True, "site_id": site_id, "class_name": class_name})
