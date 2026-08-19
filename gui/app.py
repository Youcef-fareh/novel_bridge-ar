"""
NovelBridge — PyQt6 main application window.
Calls backend functions directly (in-process, no HTTP needed).
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import (
    QObject, QRunnable, QSettings, Qt, QThread, QThreadPool,
    pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import QAction, QFont, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QSplitter,
    QStatusBar, QTabWidget, QVBoxLayout, QWidget,
)

from backend.adapters.base import AdapterRegistry
from backend.adapters.galaxynovels import GalaxyNovelsAdapter
from backend.adapters.novelfire import NovelFireAdapter
from backend.adapters.novelphoenix import NovelPhoenixAdapter
from backend.adapters.wtrlab import WTRLabAdapter
from backend.database import (
    create_novel, delete_novel, get_all_novels, get_chapters,
    get_novel, init_db, update_chapter, update_novel,
)
from backend.models import Chapter, ChapterStatus, Novel, NovelStatus
from backend.pipeline import (
    JobControl, run_epub_job, run_scrape_job, run_translation_job,
)
from backend.translation import (
    PROVIDER_DISPLAY_NAMES,
    PROVIDER_MODELS,
    ProviderFailureError,
)
from gui.widgets.api_keys import ApiSettingsTableWidget
from gui.widgets.chapter_table import ChapterTableWidget
from gui.widgets.glossary_editor import GlossaryEditorWidget
from gui.widgets.novel_list import NovelListWidget



# ── Async worker ───────────────────────────────────────────────────────────────

class WorkerSignals(QObject):
    progress   = pyqtSignal(int, int, str)   # done, total, message
    finished   = pyqtSignal(object)           # result
    cancelled  = pyqtSignal()                 # cancelled by user
    error      = pyqtSignal(str)              # error message


class AsyncWorker(QRunnable):
    """Runs an async coroutine in a background thread with its own event loop."""

    def __init__(self, coro_factory):
        super().__init__()
        self.signals = WorkerSignals()
        self._coro_factory = coro_factory

    @pyqtSlot()
    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(self._coro_factory())
            self.signals.finished.emit(result)
        except asyncio.CancelledError:
            self.signals.cancelled.emit()
        except Exception as e:
            self.signals.error.emit(str(e))
        finally:
            loop.close()


# ── Add Novel Dialog ───────────────────────────────────────────────────────────

class AddNovelDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Novel")
        self.setFixedSize(540, 220)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(16)

        title = QLabel("Add Novel from URL")
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: #ffffff;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(10)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(
            "e.g. https://novelfire.net/book/some-novel-title"
        )
        form.addRow("Novel URL:", self.url_input)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_btn:
            cancel_btn.setObjectName("btn_secondary")
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate(self):
        url = self.url_input.text().strip()
        if not url.startswith("http"):
            QMessageBox.warning(self, "Invalid URL", "Please enter a valid URL starting with http.")
            return
        adapter = AdapterRegistry.find(url)
        if not adapter:
            sites = ", ".join(a["site_id"] for a in AdapterRegistry.list_all())
            QMessageBox.warning(
                self, "Unsupported Site",
                f"No adapter found for this URL.\nSupported sites: {sites}",
            )
            return
        self.accept()

    def get_url(self) -> str:
        return self.url_input.text().strip()


# ── Novel Detail Panel ─────────────────────────────────────────────────────────

class NovelDetailPanel(QWidget):
    """Right-side panel: novel info + chapter table + action buttons."""

    delete_requested = pyqtSignal(int)
    open_api_keys_requested = pyqtSignal()
    progress_requested = pyqtSignal(int, int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._novel: Optional[Novel] = None
        self._chapters: List[Chapter] = []
        self._thread_pool = QThreadPool.globalInstance()
        self._job_control: Optional[JobControl] = None
        self._setup_ui()
        self.progress_requested.connect(self._on_progress)
        self._load_provider_settings()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 8, 8)
        layout.setSpacing(12)

        # Novel info header
        self.novel_title_label = QLabel("Select a novel from the library")
        self.novel_title_label.setObjectName("label_heading")
        self.novel_title_label.setStyleSheet(
            "font-size: 18px; font-weight: 700; color: #ffffff; margin-bottom: 2px;"
        )
        self.novel_title_label.setWordWrap(True)
        layout.addWidget(self.novel_title_label)

        self.novel_meta_label = QLabel("")
        self.novel_meta_label.setStyleSheet("color: #718096; font-size: 12px;")
        layout.addWidget(self.novel_meta_label)

        # Stats bar
        stats_row = QHBoxLayout()
        self.stat_total    = self._make_stat("Total", "0")
        self.stat_scraped  = self._make_stat("Scraped", "0")
        self.stat_trans    = self._make_stat("Translated", "0")
        self.stat_failed   = self._make_stat("Failed", "0")
        for stat in [self.stat_total, self.stat_scraped, self.stat_trans, self.stat_failed]:
            stats_row.addWidget(stat)
        stats_row.addStretch()
        layout.addLayout(stats_row)

        # Chapter table
        self.chapter_table = ChapterTableWidget()
        layout.addWidget(self.chapter_table, stretch=1)

        # Provider & Model Settings Row
        self.provider_card = QFrame()
        self.provider_card.setObjectName("providerCard")
        self.provider_card.setStyleSheet("""
            #providerCard {
                background: #151824;
                border: 1px solid #2d3748;
                border-radius: 8px;
                padding: 4px 8px;
            }
        """)
        prov_layout = QHBoxLayout(self.provider_card)
        prov_layout.setContentsMargins(10, 5, 10, 5)
        prov_layout.setSpacing(8)

        prov_lbl = QLabel("🤖 Provider:")
        prov_lbl.setStyleSheet("font-weight: 600; color: #e2e8f0; font-size: 12px;")
        prov_layout.addWidget(prov_lbl)

        self.combo_provider = QComboBox()
        self.combo_provider.setObjectName("combo_provider")
        self.combo_provider.setStyleSheet("padding: 4px 8px; font-size: 12px;")
        for key, name in PROVIDER_DISPLAY_NAMES:
            self.combo_provider.addItem(name, key)
        self.combo_provider.currentIndexChanged.connect(self._on_provider_changed)
        prov_layout.addWidget(self.combo_provider)

        model_lbl = QLabel("🏷️ Model:")
        model_lbl.setStyleSheet("font-weight: 600; color: #e2e8f0; font-size: 12px; margin-left: 4px;")
        prov_layout.addWidget(model_lbl)

        self.combo_model = QComboBox()
        self.combo_model.setObjectName("combo_model")
        self.combo_model.setEditable(True)
        self.combo_model.setMinimumWidth(220)
        self.combo_model.setStyleSheet("padding: 4px 8px; font-size: 12px;")
        self.combo_model.currentTextChanged.connect(self._on_model_changed)
        prov_layout.addWidget(self.combo_model, stretch=1)

        self.btn_open_keys = QPushButton("🔑 API Keys")
        self.btn_open_keys.setObjectName("btn_secondary")
        self.btn_open_keys.setToolTip("Configure API keys and credentials")
        self.btn_open_keys.setStyleSheet("padding: 4px 12px; font-size: 11px;")
        self.btn_open_keys.clicked.connect(self.open_api_keys_requested.emit)
        prov_layout.addWidget(self.btn_open_keys)

        layout.addWidget(self.provider_card)

        # Progress bar + status
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #a0aec0; font-size: 12px;")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_scrape = QPushButton("🔍 Scrape Chapters")
        self.btn_scrape.setObjectName("btn_secondary")
        self.btn_scrape.clicked.connect(self._on_scrape)
        btn_row.addWidget(self.btn_scrape)

        self.btn_translate = QPushButton("🌐 Translate All")
        self.btn_translate.clicked.connect(self._on_translate_all)
        btn_row.addWidget(self.btn_translate)

        self.btn_translate_sel = QPushButton("🌐 Translate Selected")
        self.btn_translate_sel.setObjectName("btn_secondary")
        self.btn_translate_sel.clicked.connect(self._on_translate_selected)
        btn_row.addWidget(self.btn_translate_sel)

        self.btn_delete_translation = QPushButton("🗑 Delete Translation")
        self.btn_delete_translation.setObjectName("btn_danger")
        self.btn_delete_translation.setToolTip("Delete translations for checked chapters; scraped source text is kept")
        self.btn_delete_translation.clicked.connect(self._on_delete_translation)
        btn_row.addWidget(self.btn_delete_translation)

        self.btn_epub = QPushButton("📕 Build EPUB")
        self.btn_epub.setObjectName("btn_success")
        self.btn_epub.clicked.connect(self._on_build_epub)
        btn_row.addWidget(self.btn_epub)

        self.btn_delete_novel = QPushButton("🗑 Delete Novel")
        self.btn_delete_novel.setObjectName("btn_danger")
        self.btn_delete_novel.setToolTip("Delete this novel and its chapters from library")
        self.btn_delete_novel.clicked.connect(self._on_delete_novel)
        self.btn_delete_novel.setEnabled(False)
        btn_row.addWidget(self.btn_delete_novel)

        self.btn_pause = QPushButton("⏸ Pause")
        self.btn_pause.setObjectName("btn_secondary")
        self.btn_pause.setToolTip("Pause or resume the running job")
        self.btn_pause.clicked.connect(self._on_pause_clicked)
        self.btn_pause.setVisible(False)
        btn_row.addWidget(self.btn_pause)

        self.btn_cancel = QPushButton("⏹ Cancel")
        self.btn_cancel.setObjectName("btn_danger")
        self.btn_cancel.setToolTip("Stop the running job and keep completed progress")
        self.btn_cancel.clicked.connect(self._on_cancel_clicked)
        self.btn_cancel.setVisible(False)
        btn_row.addWidget(self.btn_cancel)

        layout.addLayout(btn_row)


    def _on_delete_novel(self):
        if self._novel and self._novel.id:
            self.delete_requested.emit(self._novel.id)

    def _on_pause_clicked(self):
        if not self._job_control:
            return
        if self._job_control.is_paused:
            self._job_control.resume()
            self.btn_pause.setText("⏸ Pause")
            self.btn_pause.setObjectName("btn_secondary")
            self.btn_pause.style().unpolish(self.btn_pause)
            self.btn_pause.style().polish(self.btn_pause)
            self.status_label.setText("▶ Resuming job…")
        else:
            self._job_control.pause()
            self.btn_pause.setText("▶ Continue")
            self.btn_pause.setObjectName("btn_success")
            self.btn_pause.style().unpolish(self.btn_pause)
            self.btn_pause.style().polish(self.btn_pause)
            self.status_label.setText("⏸ Job paused. Click '▶ Continue' to resume or '⏹ Cancel' to stop.")

    def _on_cancel_clicked(self):
        if not self._job_control:
            return
        reply = QMessageBox.question(
            self,
            "Cancel Job",
            "Are you sure you want to stop the current job?\nChapters completed so far have been saved.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.status_label.setText("Stopping job…")
            self.btn_cancel.setEnabled(False)
            self.btn_pause.setEnabled(False)
            self._job_control.cancel()

    def _on_job_cancelled(self):
        self._set_busy(False)
        self.status_label.setText("⏹ Job stopped. Completed chapters are saved.")
        if self._novel:
            self.load_novel(self._novel.id)

    def _make_stat(self, label: str, value: str) -> QWidget:
        w = QWidget()
        w.setStyleSheet("background: #1a1d27; border-radius: 8px;")
        vl = QVBoxLayout(w)
        vl.setContentsMargins(14, 8, 14, 8)
        vl.setSpacing(2)
        v_label = QLabel(value)
        v_label.setStyleSheet("font-size: 18px; font-weight: 700; color: #6c63ff;")
        v_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        k_label = QLabel(label)
        k_label.setStyleSheet("font-size: 12px; color: #718096;")
        k_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vl.addWidget(v_label)
        vl.addWidget(k_label)
        w.setMinimumWidth(110)
        w.setFixedHeight(62)
        # Store references so we can update them
        w._value_label = v_label
        return w

    def _update_stats(self):
        is_native_ar = False
        if self._novel and self._novel.source_site:
            adapter = AdapterRegistry._adapters.get(self._novel.source_site)
            if adapter and getattr(adapter, "is_native_arabic", False):
                is_native_ar = True

        stats = self.chapter_table.get_stats(self._chapters, is_native_arabic=is_native_ar)
        self.stat_total._value_label.setText(str(stats["total"]))
        self.stat_scraped._value_label.setText(str(stats["scraped"]))
        self.stat_trans._value_label.setText(str(stats["translated"]))
        self.stat_failed._value_label.setText(str(stats["failed"]))

    def clear(self) -> None:
        self._novel = None
        self._chapters = []
        self.novel_title_label.setText("Select a novel from the library")
        self.novel_meta_label.setText("")
        self.chapter_table.set_chapters([])
        self._update_stats()
        self.status_label.setText("")
        self.progress_bar.setVisible(False)
        self.btn_translate.setVisible(True)
        self.btn_translate_sel.setVisible(True)
        self.btn_delete_novel.setEnabled(False)

    def load_novel(self, novel_id: int) -> None:
        self._novel = get_novel(novel_id)
        self._chapters = get_chapters(novel_id)
        if not self._novel:
            self.clear()
            return
        self.btn_delete_novel.setEnabled(True)
        self.novel_title_label.setText(self._novel.title)
        meta_parts = []
        if self._novel.author:
            meta_parts.append(f"Author: {self._novel.author}")
        if self._novel.source_site:
            meta_parts.append(f"Site: {self._novel.source_site}")
        
        # Check if native Arabic site
        is_native_ar = False
        if self._novel.source_site:
            adapter = AdapterRegistry._adapters.get(self._novel.source_site)
            if adapter and getattr(adapter, "is_native_arabic", False):
                is_native_ar = True

        if is_native_ar:
            meta_parts.append("Language: Arabic (Native)")
            # Hide translation buttons since content is already in Arabic
            self.btn_translate.setVisible(False)
            self.btn_translate_sel.setVisible(False)
            self.btn_delete_translation.setVisible(False)
            self.stat_trans.findChild(QLabel, "").setText("Ready") if hasattr(self.stat_trans, "findChild") else None
        else:
            self.btn_translate.setVisible(True)
            self.btn_translate_sel.setVisible(True)
            self.btn_delete_translation.setVisible(True)

        # Clean status display (e.g. "scraped" instead of "NovelStatus.scraped")
        status_val = self._novel.status.value if hasattr(self._novel.status, "value") else str(self._novel.status)
        meta_parts.append(f"Status: {status_val.capitalize()}")
        
        self.novel_meta_label.setText("  ·  ".join(meta_parts))
        self.chapter_table.set_chapters(self._chapters, is_native_arabic=is_native_ar)
        self._update_stats()

    def _load_provider_settings(self) -> None:
        settings = QSettings("NovelBridge", "Settings")
        saved_provider = settings.value("selected_provider", "tokenrouter")
        saved_model = settings.value("selected_model", "")

        idx = self.combo_provider.findData(saved_provider)
        if idx >= 0:
            self.combo_provider.setCurrentIndex(idx)
        else:
            self.combo_provider.setCurrentIndex(0)

        self._populate_models_for_current_provider(saved_model)

    def _on_provider_changed(self, index: int) -> None:
        provider_key = self.combo_provider.currentData()
        settings = QSettings("NovelBridge", "Settings")
        settings.setValue("selected_provider", provider_key)
        self._populate_models_for_current_provider()

    def _on_model_changed(self, text: str) -> None:
        settings = QSettings("NovelBridge", "Settings")
        settings.setValue("selected_model", text.strip())

    def _populate_models_for_current_provider(self, custom_model: Optional[str] = None) -> None:
        provider_key = self.combo_provider.currentData() or "tokenrouter"
        self.combo_model.blockSignals(True)
        self.combo_model.clear()

        models = PROVIDER_MODELS.get(provider_key, [])
        for m in models:
            self.combo_model.addItem(m)

        if custom_model:
            self.combo_model.setEditText(custom_model)
        elif models:
            self.combo_model.setCurrentIndex(0)
        self.combo_model.blockSignals(False)

    def get_selected_provider(self) -> str:
        return self.combo_provider.currentData() or "tokenrouter"

    def get_selected_model(self) -> str:
        return self.combo_model.currentText().strip()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.progress_bar.setVisible(busy)
        self.btn_scrape.setEnabled(not busy)
        self.btn_translate.setEnabled(not busy)
        self.btn_translate_sel.setEnabled(not busy)
        self.btn_epub.setEnabled(not busy)
        self.btn_delete_novel.setEnabled(not busy and self._novel is not None)
        self.provider_card.setEnabled(not busy)

        self.btn_pause.setVisible(busy)
        self.btn_cancel.setVisible(busy)
        self.btn_pause.setEnabled(busy)
        self.btn_cancel.setEnabled(busy)
        if busy:
            self.btn_pause.setText("⏸ Pause")
            self.btn_pause.setObjectName("btn_secondary")
            self.btn_pause.style().unpolish(self.btn_pause)
            self.btn_pause.style().polish(self.btn_pause)
            self.status_label.setText(message)
        else:
            self._job_control = None

    def _on_progress(self, done: int, total: int, message: str) -> None:
        if total > 0:
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(done)
        self.status_label.setText(message)

    def _on_scrape(self) -> None:
        if not self._novel:
            return
        self._set_busy(True, "Starting scrape…")
        self._job_control = JobControl()
        job_ctrl = self._job_control
        novel_id = self._novel.id

        async def coro():
            return await run_scrape_job(
                novel_id,
                progress_cb=lambda d, t, m: self.progress_requested.emit(d, t, m),
                job_control=job_ctrl,
            )

        worker = AsyncWorker(coro)
        worker.signals.finished.connect(self._on_scrape_done)
        worker.signals.cancelled.connect(self._on_job_cancelled)
        worker.signals.error.connect(self._on_error)
        worker.signals.progress.connect(self._on_progress)
        self._thread_pool.start(worker)

    def _on_scrape_done(self, _) -> None:
        self._set_busy(False)
        self.status_label.setText("✅ Scraping complete!")
        if self._novel:
            self.load_novel(self._novel.id)

    def _on_translate_all(self) -> None:
        if not self._novel:
            return
        self._set_busy(True, "Starting translation…")
        self._job_control = JobControl()
        job_ctrl = self._job_control
        novel_id = self._novel.id
        provider_name = self.get_selected_provider()
        model_name = self.get_selected_model()

        async def coro():
            return await run_translation_job(
                novel_id,
                provider_name=provider_name,
                model_name=model_name,
                progress_cb=lambda d, t, m: self.progress_requested.emit(d, t, m),
                job_control=job_ctrl,
            )

        worker = AsyncWorker(coro)
        worker.signals.finished.connect(self._on_translate_done)
        worker.signals.cancelled.connect(self._on_job_cancelled)
        worker.signals.error.connect(self._on_error)
        self._thread_pool.start(worker)

    def _on_translate_selected(self) -> None:
        if not self._novel:
            return
        chapter_ids = self.chapter_table.get_checked_chapter_ids()
        if not chapter_ids:
            QMessageBox.information(self, "Selection", "Please check the chapters you want to translate.")
            return
        self._set_busy(True, f"Translating {len(chapter_ids)} selected chapters…")
        self._job_control = JobControl()
        job_ctrl = self._job_control
        novel_id = self._novel.id
        provider_name = self.get_selected_provider()
        model_name = self.get_selected_model()

        async def coro():
            return await run_translation_job(
                novel_id,
                chapter_ids=chapter_ids,
                provider_name=provider_name,
                model_name=model_name,
                progress_cb=lambda d, t, m: self.progress_requested.emit(d, t, m),
                job_control=job_ctrl,
            )

        worker = AsyncWorker(coro)
        worker.signals.finished.connect(self._on_translate_done)
        worker.signals.cancelled.connect(self._on_job_cancelled)
        worker.signals.error.connect(self._on_error)
        self._thread_pool.start(worker)

    def _on_delete_translation(self) -> None:
        if not self._novel:
            return
        chapter_ids = self.chapter_table.get_checked_chapter_ids()
        if not chapter_ids:
            QMessageBox.information(self, "Selection", "Please check chapters whose translation you want to delete.")
            return

        selected = [chapter for chapter in self._chapters if chapter.id in chapter_ids]
        translated = [chapter for chapter in selected if chapter.translated_text]
        if not translated:
            QMessageBox.information(self, "Delete Translation", "None of the checked chapters has a translation.")
            return

        reply = QMessageBox.question(
            self,
            "Delete Translation",
            f"Delete translations for {len(translated)} checked chapter(s)?\n\n"
            "The scraped source chapters will be kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for chapter in translated:
            status = ChapterStatus.scraped if chapter.raw_text and chapter.raw_text.strip() else ChapterStatus.pending
            update_chapter(chapter.id, translated_text=None, status=status)

        self.load_novel(self._novel.id)
        self.status_label.setText(f"🗑 Deleted {len(translated)} translation(s). Source chapters kept.")

    def _on_translate_done(self, _) -> None:
        self._set_busy(False)
        self.status_label.setText("✅ Translation complete!")
        if self._novel:
            self.load_novel(self._novel.id)

    def _on_build_epub(self) -> None:
        if not self._novel:
            return
        self._set_busy(True, "Building EPUB…")
        novel_id = self._novel.id

        async def coro():
            job, path = await run_epub_job(
                novel_id,
                progress_cb=lambda d, t, m: self.progress_requested.emit(d, t, m),
            )
            return str(path)

        worker = AsyncWorker(coro)
        worker.signals.finished.connect(self._on_epub_done)
        worker.signals.error.connect(self._on_error)
        self._thread_pool.start(worker)

    def _on_epub_done(self, epub_path: str) -> None:
        self._set_busy(False)
        self.status_label.setText(f"✅ EPUB saved: {Path(epub_path).name}")
        reply = QMessageBox.question(
            self, "EPUB Ready",
            f"EPUB saved to:\n{epub_path}\n\nOpen output folder?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            folder = str(Path(epub_path).parent)
            if sys.platform == "win32":
                os.startfile(folder)

    def _on_error(self, error_msg: str) -> None:
        self._set_busy(False)
        self.status_label.setText(f"❌ Error: {error_msg[:100]}")
        if self._novel:
            self.load_novel(self._novel.id)

        # Check if error is provider/quota/key related to provide quick actions
        err_lower = error_msg.lower()
        is_provider_issue = any(
            kw in err_lower
            for kw in ["provider", "tokenrouter", "gemini", "groq", "api key", "suggestion", "quota", "rate limit", "429", "401", "503"]
        )

        if is_provider_issue:
            msg_box = QMessageBox(self)
            msg_box.setIcon(QMessageBox.Icon.Warning)
            msg_box.setWindowTitle("Translation Error — Provider Failed")
            msg_box.setText("❌ Translation stopped due to provider error.")
            msg_box.setInformativeText(
                f"{error_msg}\n\n"
                "Would you like to switch to a different translation provider/model or check your API Keys?"
            )
            btn_switch = msg_box.addButton("🔄 Change Provider", QMessageBox.ButtonRole.ActionRole)
            btn_keys = msg_box.addButton("🔑 Open API Keys", QMessageBox.ButtonRole.ActionRole)
            btn_close = msg_box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
            msg_box.setDefaultButton(btn_switch)
            msg_box.exec()

            if msg_box.clickedButton() == btn_switch:
                self.combo_provider.setFocus()
                self.combo_provider.showPopup()
            elif msg_box.clickedButton() == btn_keys:
                self.open_api_keys_requested.emit()
        else:
            QMessageBox.critical(self, "Error", error_msg)



# ── Main Window ────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NovelBridge — Arabic Web Novel Translator")
        self.setMinimumSize(1100, 700)
        self.resize(1280, 780)
        self._apply_icon()
        self._setup_ui()
        self._setup_menu()
        self._refresh_novels()

    @staticmethod
    def _resolve_icon() -> Path:
        """Find icon.ico whether running from source or a PyInstaller bundle."""
        import sys as _sys
        if getattr(_sys, "frozen", False):
            # PyInstaller extracts files to sys._MEIPASS
            return Path(_sys._MEIPASS) / "icon.ico"
        return Path(__file__).parent.parent / "icon.ico"

    def _apply_icon(self) -> None:
        icon_path = self._resolve_icon()
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Top header bar ─────────────────────────────────────────────────
        header = QWidget()
        header.setStyleSheet("background: #0a0d14; border-bottom: 1px solid #2d3748;")
        header.setFixedHeight(56)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(16, 0, 16, 0)

        logo = QLabel("🌉 NovelBridge")
        logo.setStyleSheet("font-size: 18px; font-weight: 800; color: #6c63ff; letter-spacing: 0.03em;")
        h_layout.addWidget(logo)

        tagline = QLabel("English Web Novels → Arabic EPUB")
        tagline.setStyleSheet("color: #4a5568; font-size: 12px; margin-left: 12px;")
        h_layout.addWidget(tagline)
        h_layout.addStretch()

        self.btn_add_novel = QPushButton("＋ Add Novel")
        self.btn_add_novel.setObjectName("")
        self.btn_add_novel.setStyleSheet("""
            QPushButton { background: #6c63ff; color: #fff; border-radius: 8px;
                          padding: 8px 18px; font-weight: 600; }
            QPushButton:hover { background: #7c74ff; }
        """)
        self.btn_add_novel.clicked.connect(self._on_add_novel)
        h_layout.addWidget(self.btn_add_novel)

        self.btn_api_server = QPushButton("🖧 Start API Server")
        self.btn_api_server.setStyleSheet("""
            QPushButton { background: #252a3a; color: #e2e8f0; border-radius: 8px;
                          padding: 8px 18px; font-weight: 600; margin-left: 8px; }
            QPushButton:hover { background: #2d3748; }
        """)
        self.btn_api_server.clicked.connect(self._on_start_api_server)
        h_layout.addWidget(self.btn_api_server)

        root.addWidget(header)

        # ── Tab widget ─────────────────────────────────────────────────────
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        root.addWidget(self.tabs)

        # Tab 1: Library
        library_tab = QWidget()
        lib_layout = QHBoxLayout(library_tab)
        lib_layout.setContentsMargins(0, 0, 0, 0)
        lib_layout.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: novel list
        left_panel = QWidget()
        left_panel.setMinimumWidth(220)
        left_panel.setMaximumWidth(280)
        left_panel.setStyleSheet("background: #0f1117; border-right: 1px solid #2d3748;")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(12, 12, 12, 12)

        self.novel_list = NovelListWidget()
        self.novel_list.novel_selected.connect(self._on_novel_selected)
        self.novel_list.novel_deleted.connect(self._on_novel_deleted)
        left_layout.addWidget(self.novel_list)

        sidebar_btn_row = QHBoxLayout()
        sidebar_btn_row.setSpacing(6)

        self.btn_delete = QPushButton("🗑 Delete")
        self.btn_delete.setObjectName("btn_danger")
        self.btn_delete.setToolTip("Delete selected novel from library")
        self.btn_delete.clicked.connect(self._on_sidebar_delete)
        sidebar_btn_row.addWidget(self.btn_delete)

        self.btn_refresh = QPushButton("↻ Refresh")
        self.btn_refresh.setObjectName("btn_secondary")
        self.btn_refresh.clicked.connect(self._refresh_novels)
        sidebar_btn_row.addWidget(self.btn_refresh)

        left_layout.addLayout(sidebar_btn_row)

        splitter.addWidget(left_panel)

        # Right: novel detail
        self.detail_panel = NovelDetailPanel()
        self.detail_panel.delete_requested.connect(self._on_novel_deleted)
        self.detail_panel.open_api_keys_requested.connect(lambda: self.tabs.setCurrentIndex(2))
        splitter.addWidget(self.detail_panel)
        splitter.setSizes([240, 860])
        lib_layout.addWidget(splitter)


        self.tabs.addTab(library_tab, "📚 Library")

        # Tab 2: Glossary
        glossary_tab = QWidget()
        g_layout = QVBoxLayout(glossary_tab)
        g_layout.setContentsMargins(16, 16, 16, 16)
        self.glossary_editor = GlossaryEditorWidget()
        g_layout.addWidget(self.glossary_editor)
        self.tabs.addTab(glossary_tab, "📖 Glossary")

        # Tab 3: API Key Management / Settings
        settings_tab = self._build_settings_tab()
        self.tabs.addTab(settings_tab, "🔑 API Keys")

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready — Add a novel URL to get started.")

    def _build_settings_tab(self) -> QWidget:
        """
        Settings tab — contains the full ApiKeysWidget plus
        a collapsed info section for concurrency and supported sites.
        """
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── API Key Management (takes the bulk of the space) ─────────────────
        self.api_keys_widget = ApiSettingsTableWidget()
        layout.addWidget(self.api_keys_widget, stretch=1)

        # ── Collapsed info section ────────────────────────────────────────────
        info_bar = QWidget()
        info_bar.setStyleSheet(
            "background: #141720; border-top: 1px solid #2d3748;"
        )
        info_layout = QHBoxLayout(info_bar)
        info_layout.setContentsMargins(20, 8, 20, 8)
        info_layout.setSpacing(24)

        conc_lbl = QLabel(
            f"⚡ Max concurrent jobs: "
            f"<b>{os.getenv('MAX_CONCURRENT_JOBS', '3')}</b>"
        )
        conc_lbl.setStyleSheet("color: #718096; font-size: 11px;")
        info_layout.addWidget(conc_lbl)

        sites = "  ·  ".join(s["site_id"] for s in AdapterRegistry.list_all())
        sites_lbl = QLabel(f"🌐 Sites: <b>{sites}</b>")
        sites_lbl.setStyleSheet("color: #718096; font-size: 11px;")
        info_layout.addWidget(sites_lbl)
        info_layout.addStretch()

        layout.addWidget(info_bar)
        return w

    def _setup_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")

        add_action = QAction("Add Novel…", self)
        add_action.triggered.connect(self._on_add_novel)
        file_menu.addAction(add_action)

        delete_action = QAction("Delete Selected Novel", self)
        delete_action.triggered.connect(self._on_sidebar_delete)
        file_menu.addAction(delete_action)

        file_menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.quit)
        file_menu.addAction(quit_action)

        help_menu = menubar.addMenu("Help")
        about_action = QAction("About NovelBridge", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

    def _on_sidebar_delete(self) -> None:
        novel_id = self.novel_list.get_selected_novel_id()
        if not novel_id and self.detail_panel._novel:
            novel_id = self.detail_panel._novel.id
        if novel_id:
            self._on_novel_deleted(novel_id)
        else:
            QMessageBox.information(self, "Delete Novel", "Please select a novel from the library first.")

    def _refresh_novels(self) -> None:
        novels = get_all_novels()
        self.novel_list.set_novels(novels)
        self.status_bar.showMessage(f"Library: {len(novels)} novel(s).")

    def _on_novel_selected(self, novel_id: int) -> None:
        self.detail_panel.load_novel(novel_id)
        self.tabs.setCurrentIndex(0)

    def _on_novel_deleted(self, novel_id: int) -> None:
        novel = get_novel(novel_id)
        name = novel.title if novel else "this novel"
        reply = QMessageBox.question(
            self,
            "Delete Novel",
            f"Are you sure you want to delete '{name}' and all its chapters?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            delete_novel(novel_id)
            self._refresh_novels()
            if self.detail_panel._novel and self.detail_panel._novel.id == novel_id:
                self.detail_panel.clear()
            self.status_bar.showMessage(f"🗑️ Deleted '{name}' from library.")

    def _on_add_novel(self) -> None:
        dialog = AddNovelDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            url = dialog.get_url()
            adapter = AdapterRegistry.find(url)
            novel = create_novel(source_url=url, source_site=adapter.site_id)
            self._refresh_novels()
            self.novel_list.select_novel(novel.id)
            self.detail_panel.load_novel(novel.id)
            # Auto-start scrape
            QMessageBox.information(
                self,
                "Novel Added",
                f"Novel added to library!\n\nClick '🔍 Scrape Chapters' to fetch the chapter list.\n\nURL: {url}",
            )

    def _on_start_api_server(self) -> None:
        try:
            port = os.getenv("API_PORT", "8000")
            subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", port],
                creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
            )
            self.status_bar.showMessage(f"✅ API server started on http://0.0.0.0:{port}")
            QMessageBox.information(
                self,
                "API Server",
                f"FastAPI server started!\n\nAPI available at: http://localhost:{port}\nDocs at: http://localhost:{port}/docs",
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start API server:\n{e}")

    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            "About NovelBridge",
            "NovelBridge v0.1 — MVP\n\n"
            "Web novel scraper + Arabic translator + EPUB generator.\n\n"
            "Supported sites: NovelFire, WTR-Lab\n"
            "Translation: Google Gemini (primary) → Groq (fallback)\n"
            "EPUB: RTL Arabic with ebooklib\n\n"
            "Built with Python · FastAPI · PyQt6",
        )


# ── App bootstrap ──────────────────────────────────────────────────────────────

def load_stylesheet() -> str:
    qss_path = Path(__file__).parent / "resources" / "styles.qss"
    if qss_path.exists():
        return qss_path.read_text(encoding="utf-8")
    return ""


def run_app() -> None:
    # Initialise database + register adapters before Qt starts
    init_db()
    AdapterRegistry.register(NovelFireAdapter())
    AdapterRegistry.register(WTRLabAdapter())
    AdapterRegistry.register(NovelPhoenixAdapter())
    AdapterRegistry.register(GalaxyNovelsAdapter())

    app = QApplication(sys.argv)
    app.setApplicationName("NovelBridge")
    app.setOrganizationName("NovelBridge")

    # Set app-level icon (taskbar, alt-tab, etc.)
    _icon_path = Path(__file__).parent.parent / "icon.ico"
    if _icon_path.exists():
        app.setWindowIcon(QIcon(str(_icon_path)))

    # Apply dark stylesheet
    stylesheet = load_stylesheet()
    if stylesheet:
        app.setStyleSheet(stylesheet)

    # High-DPI support
    app.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    window = MainWindow()
    window.show()
    sys.exit(app.exec())
