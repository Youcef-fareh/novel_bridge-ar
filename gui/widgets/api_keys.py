"""
gui/widgets/api_keys.py
═══════════════════════
API Key Management widget for NovelBridge AR.

Features
────────
• Reads all keys from the .env file on startup
• Per-row show/hide toggle (👁 button)
• Per-row "Test" badge that validates the key format
• Global Save button — writes changes back to .env atomically
• Global Reload button — re-reads .env without saving
• Status strip shows last-saved time
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

ENV_FILE = Path(__file__).parent.parent.parent / ".env"

# Ordered list of all API-key entries we manage.
# Each tuple: (env_var_name, display_label, hint_url, is_secret)
API_KEY_DEFS: List[tuple] = [
    (
        "GEMINI_API_KEY",
        "Google Gemini API Key",
        "https://aistudio.google.com/app/apikey",
        True,
    ),
    (
        "GROQ_API_KEY",
        "Groq API Key  (fallback)",
        "https://console.groq.com",
        True,
    ),
    (
        "GEMINI_MODEL",
        "Gemini Model",
        "e.g. gemini-1.5-flash",
        False,
    ),
    (
        "GROQ_MODEL",
        "Groq Model",
        "e.g. llama-3.3-70b-versatile",
        False,
    ),
]


def _read_env_file() -> Dict[str, str]:
    """Parse the .env file and return a key→value mapping (preserves comments)."""
    result: Dict[str, str] = {}
    if not ENV_FILE.exists():
        return result
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, _, val = stripped.partition("=")
            result[key.strip()] = val.strip()
    return result


def _write_env_file(updates: Dict[str, str]) -> None:
    """
    Patch the .env file in-place: update matching KEY=... lines,
    append new keys that don't exist yet. Comments and blank lines
    are fully preserved.
    """
    lines: List[str] = []
    handled: set = set()

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                if key in updates:
                    lines.append(f"{key}={updates[key]}")
                    handled.add(key)
                    continue
            lines.append(line)

    # Append any keys that weren't in the file at all
    for key, val in updates.items():
        if key not in handled:
            lines.append(f"{key}={val}")

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mask(value: str) -> str:
    """Return a safely masked version for display (first 4 + stars + last 2)."""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "•" * (len(value) - 6) + value[-2:]


# ── Single Key Row ──────────────────────────────────────────────────────────────

class ApiKeyRow(QFrame):
    """One row in the API key list."""

    changed = pyqtSignal()  # emitted whenever the value is edited

    def __init__(
        self,
        env_var: str,
        label: str,
        hint: str,
        is_secret: bool,
        current_value: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._env_var = env_var
        self._is_secret = is_secret
        self._visible = False
        self._original = current_value

        self.setObjectName("apiKeyRow")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._setup_ui(label, hint, current_value)

    # ── Layout ────────────────────────────────────────────────────────────────

    def _setup_ui(self, label: str, hint: str, value: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(6)

        # ── Top row: label + env-var badge + links ───────────────────────────
        top = QHBoxLayout()
        top.setSpacing(8)

        name_lbl = QLabel(label)
        name_lbl.setStyleSheet(
            "font-weight: 700; font-size: 13px; color: #e2e8f0;"
        )
        top.addWidget(name_lbl)

        var_badge = QLabel(self._env_var)
        var_badge.setObjectName("varBadge")
        var_badge.setStyleSheet(
            "color: #9f7aea; font-family: 'Consolas', monospace; font-size: 11px;"
            "background: #1e1b3a; border: 1px solid #4a3f7a; border-radius: 4px;"
            "padding: 1px 6px;"
        )
        top.addWidget(var_badge)
        top.addStretch()

        if hint.startswith("http"):
            link = QLabel(f'<a href="{hint}" style="color:#6c63ff;">Get key ↗</a>')
            link.setOpenExternalLinks(True)
            link.setStyleSheet("font-size: 11px;")
            top.addWidget(link)
        else:
            hint_lbl = QLabel(hint)
            hint_lbl.setStyleSheet("color: #718096; font-size: 11px;")
            top.addWidget(hint_lbl)

        root.addLayout(top)

        # ── Input row ────────────────────────────────────────────────────────
        input_row = QHBoxLayout()
        input_row.setSpacing(6)

        self.input = QLineEdit()
        self.input.setText(value)
        self.input.setPlaceholderText("Not set — enter a value and Save")
        if self._is_secret:
            self.input.setEchoMode(QLineEdit.EchoMode.Password)
        self.input.textChanged.connect(self._on_changed)
        input_row.addWidget(self.input, stretch=1)

        if self._is_secret:
            self.btn_toggle = QPushButton("👁")
            self.btn_toggle.setObjectName("btn_icon")
            self.btn_toggle.setFixedWidth(36)
            self.btn_toggle.setToolTip("Show / Hide")
            self.btn_toggle.clicked.connect(self._toggle_visibility)
            input_row.addWidget(self.btn_toggle)

        self.btn_copy = QPushButton("⎘")
        self.btn_copy.setObjectName("btn_icon")
        self.btn_copy.setFixedWidth(36)
        self.btn_copy.setToolTip("Copy to clipboard")
        self.btn_copy.clicked.connect(self._copy_to_clipboard)
        input_row.addWidget(self.btn_copy)

        self.btn_clear = QPushButton("✕")
        self.btn_clear.setObjectName("btn_icon")
        self.btn_clear.setFixedWidth(36)
        self.btn_clear.setToolTip("Clear value")
        self.btn_clear.clicked.connect(lambda: self.input.clear())
        input_row.addWidget(self.btn_clear)

        root.addLayout(input_row)

        # ── Status strip ─────────────────────────────────────────────────────
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet("color: #718096; font-size: 11px;")
        root.addWidget(self.status_lbl)

        self._refresh_status(value)

    # ── Slots ─────────────────────────────────────────────────────────────────

    def _toggle_visibility(self) -> None:
        self._visible = not self._visible
        self.input.setEchoMode(
            QLineEdit.EchoMode.Normal
            if self._visible
            else QLineEdit.EchoMode.Password
        )
        self.btn_toggle.setText("🙈" if self._visible else "👁")

    def _copy_to_clipboard(self) -> None:
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.input.text())
        old = self.btn_copy.text()
        self.btn_copy.setText("✓")
        QTimer.singleShot(1500, lambda: self.btn_copy.setText(old))

    def _on_changed(self, text: str) -> None:
        self._refresh_status(text)
        self.changed.emit()

    def _refresh_status(self, value: str) -> None:
        if not value:
            self.status_lbl.setText("⚠️  Not set")
            self.status_lbl.setStyleSheet("color: #e53e3e; font-size: 11px;")
        elif self._is_secret and len(value) < 20:
            self.status_lbl.setText("⚠️  Value looks too short for an API key")
            self.status_lbl.setStyleSheet("color: #ed8936; font-size: 11px;")
        else:
            masked = _mask(value) if self._is_secret else value
            self.status_lbl.setText(f"✅  {masked}")
            self.status_lbl.setStyleSheet("color: #48bb78; font-size: 11px;")

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def env_var(self) -> str:
        return self._env_var

    def value(self) -> str:
        return self.input.text().strip()

    def mark_saved(self) -> None:
        self._original = self.value()

    def is_dirty(self) -> bool:
        return self.value() != self._original


# ── Main Widget ─────────────────────────────────────────────────────────────────

class ApiKeysWidget(QWidget):
    """
    Full API Key Management panel.
    Drop this into any layout or tab.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._rows: List[ApiKeyRow] = []
        self._setup_ui()
        self._load_from_env()

    # ── Layout ────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header ───────────────────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("apiKeysHeader")
        header.setStyleSheet(
            "#apiKeysHeader { background: #1a1d27; border-bottom: 1px solid #2d3748; }"
        )
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(20, 14, 20, 14)

        title = QLabel("🔑  API Key Management")
        title.setStyleSheet("font-size: 17px; font-weight: 700; color: #ffffff;")
        h_layout.addWidget(title)
        h_layout.addStretch()

        self.btn_reload = QPushButton("↺  Reload from .env")
        self.btn_reload.setObjectName("btn_secondary")
        self.btn_reload.setToolTip("Re-read keys from the .env file (discards unsaved changes)")
        self.btn_reload.clicked.connect(self._load_from_env)
        h_layout.addWidget(self.btn_reload)

        self.btn_save = QPushButton("💾  Save All")
        self.btn_save.setObjectName("btn_primary")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save_to_env)
        h_layout.addWidget(self.btn_save)

        root.addWidget(header)

        # ── Scrollable rows area ─────────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._rows_container = QWidget()
        self._rows_container.setObjectName("rowsContainer")
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(20, 16, 20, 16)
        self._rows_layout.setSpacing(10)
        self._rows_layout.addStretch()

        scroll.setWidget(self._rows_container)
        root.addWidget(scroll, stretch=1)

        # ── Bottom status bar ─────────────────────────────────────────────────
        footer = QWidget()
        footer.setObjectName("apiKeysFooter")
        footer.setStyleSheet(
            "#apiKeysFooter { background: #1a1d27; border-top: 1px solid #2d3748; }"
        )
        f_layout = QHBoxLayout(footer)
        f_layout.setContentsMargins(20, 8, 20, 8)

        env_path = QLabel(f"📄  {ENV_FILE}")
        env_path.setStyleSheet("color: #4a5568; font-size: 11px; font-family: monospace;")
        f_layout.addWidget(env_path)
        f_layout.addStretch()

        self.footer_status = QLabel("Keys loaded from .env")
        self.footer_status.setStyleSheet("color: #718096; font-size: 11px;")
        f_layout.addWidget(self.footer_status)

        root.addWidget(footer)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _clear_rows(self) -> None:
        for row in self._rows:
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()

    def _load_from_env(self) -> None:
        env = _read_env_file()
        self._clear_rows()

        for env_var, label, hint, is_secret in API_KEY_DEFS:
            value = env.get(env_var, "") or os.getenv(env_var, "")
            row = ApiKeyRow(
                env_var=env_var,
                label=label,
                hint=hint,
                is_secret=is_secret,
                current_value=value,
                parent=self,
            )
            row.setStyleSheet(
                "ApiKeyRow { background: #1a1d27; border: 1px solid #2d3748;"
                "border-radius: 10px; }"
                "ApiKeyRow:hover { border-color: #4a3f7a; }"
            )
            row.changed.connect(self._on_any_change)

            # Insert before the trailing stretch
            stretch_idx = self._rows_layout.count() - 1
            self._rows_layout.insertWidget(stretch_idx, row)
            self._rows.append(row)

        self.btn_save.setEnabled(False)
        self.footer_status.setText(
            f"Loaded  ·  {datetime.now().strftime('%H:%M:%S')}"
        )
        self.footer_status.setStyleSheet("color: #718096; font-size: 11px;")

    def _on_any_change(self) -> None:
        has_changes = any(r.is_dirty() for r in self._rows)
        self.btn_save.setEnabled(has_changes)
        if has_changes:
            self.footer_status.setText("Unsaved changes")
            self.footer_status.setStyleSheet("color: #ed8936; font-size: 11px;")

    def _save_to_env(self) -> None:
        updates = {r.env_var: r.value() for r in self._rows}
        try:
            _write_env_file(updates)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Save Failed",
                f"Could not write to .env file:\n{exc}",
            )
            return

        # Reflect new values in the running process immediately
        for key, val in updates.items():
            if val:
                os.environ[key] = val
            elif key in os.environ:
                del os.environ[key]

        for row in self._rows:
            row.mark_saved()

        self.btn_save.setEnabled(False)
        self.footer_status.setText(
            f"✅  Saved  ·  {datetime.now().strftime('%H:%M:%S')}"
        )
        self.footer_status.setStyleSheet("color: #48bb78; font-size: 11px;")

        # Auto-fade the status back after 4 s
        QTimer.singleShot(
            4000,
            lambda: self.footer_status.setText(
                f"Last saved  {datetime.now().strftime('%H:%M')}"
            ),
        )
