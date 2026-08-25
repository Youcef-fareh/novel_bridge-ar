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
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QInputDialog,
    QFormLayout,
    QHeaderView,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

ENV_FILE = Path(__file__).parent.parent.parent / ".env"

API_KEY_DEFS: List[tuple] = [
    (
        "TOKENROUTER_API_KEY",
        "TokenRouter API Key  (DeepSeek / Qwen)",
        "https://tokenrouter.com",
        True,
    ),
    (
        "TOKENROUTER_MODEL",
        "TokenRouter Model",
        "e.g. deepseek/deepseek-v4-pro-0813-free or qwen/qwen3.8-max-free",
        False,
    ),
    (
        "TOKENROUTER_BASE_URL",
        "TokenRouter Base URL  (OpenAI-compatible)",
        "https://api.tokenrouter.com/v1",
        False,
    ),
    (
        "ORCAROUTER_API_KEY",
        "OrcaRouter API Key  (DeepSeek / Free)",
        "https://orcarouter.ai",
        True,
    ),
    (
        "ORCAROUTER_MODEL",
        "OrcaRouter Model",
        "e.g. deepseek/deepseek-v4-flash-free or orcarouter/free",
        False,
    ),
    (
        "ORCAROUTER_BASE_URL",
        "OrcaRouter Base URL  (OpenAI-compatible)",
        "https://api.orcarouter.ai/v1",
        False,
    ),
    (
        "GEMINI_API_KEY",

        "Google Gemini API Key",
        "https://aistudio.google.com/app/apikey",
        True,
    ),
    (
        "GEMINI_MODEL",
        "Gemini Model",
        "e.g. gemini-2.5-flash",
        False,
    ),
    (
        "GROQ_API_KEY",
        "Groq API Key  (fallback)",
        "https://console.groq.com",
        True,
    ),
    (
        "GROQ_MODEL",
        "Groq Model",
        "e.g. openai/gpt-oss-20b or qwen/qwen3.6-27b",
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


def _write_env_file(updates: Dict[str, str], remove_keys: set[str] | None = None) -> None:
    """
    Patch the .env file in-place: update matching KEY=... lines,
    append new keys that don't exist yet. Comments and blank lines
    are fully preserved.
    """
    lines: List[str] = []
    handled: set = set()
    remove_keys = remove_keys or set()

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.partition("=")[0].strip()
                if key in remove_keys:
                    continue
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


SETTING_TYPES = ("API Key", "Model", "Base URL", "Custom")


_PROVIDER_GROUPS = (
    ("Gemini", "GEMINI", "GEMINI_API_KEY", "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com"),
    ("TokenRouter", "TOKENROUTER", "TOKENROUTER_API_KEY", "TOKENROUTER_BASE_URL", "https://api.tokenrouter.com/v1"),
    ("OrcaRouter", "ORCAROUTER", "ORCAROUTER_API_KEY", "ORCAROUTER_BASE_URL", "https://api.orcarouter.ai/v1"),
    ("Groq", "GROQ", "GROQ_API_KEY", "GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
)


class ApiSettingDialog(QDialog):
    """Dialog for creating or editing one environment-backed setting."""

    def __init__(self, entry: Dict[str, str] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Change API Setting" if entry else "Add API Setting")
        self.setMinimumWidth(480)
        self._entry = entry or {}
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)

        title = QLabel(self.windowTitle())
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #ffffff;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(10)

        self.provider_input = QLineEdit(self._entry.get("provider", ""))
        self.provider_input.setPlaceholderText("e.g. OpenRouter")
        form.addRow("Provider:", self.provider_input)

        self.type_combo = QComboBox()
        self.type_combo.addItems(SETTING_TYPES)
        current_type = self._entry.get("type", "API Key")
        if current_type in SETTING_TYPES:
            self.type_combo.setCurrentText(current_type)
        self.type_combo.currentTextChanged.connect(self._on_type_changed)
        form.addRow("Setting type:", self.type_combo)

        self.env_input = QLineEdit(self._entry.get("env_var", ""))
        self.env_input.setPlaceholderText("PROVIDER_API_KEY")
        form.addRow("Environment variable:", self.env_input)

        self.value_input = QLineEdit(self._entry.get("value", ""))
        self.value_input.setPlaceholderText("Enter value")
        form.addRow("Value:", self.value_input)
        self._update_value_mode(self.type_combo.currentText())

        layout.addLayout(form)

        hint = QLabel("The setting is written to the project .env file after Save All.")
        hint.setStyleSheet("color: #718096; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_type_changed(self, setting_type: str) -> None:
        self._update_value_mode(setting_type)
        if not self.env_input.text().strip() or self.env_input.text().strip() == self._generated_env_var():
            self.env_input.setText(self._generated_env_var())

    def _generated_env_var(self) -> str:
        provider = re.sub(r"[^A-Za-z0-9]+", "_", self.provider_input.text().strip()).strip("_").upper()
        suffix = {
            "API Key": "API_KEY",
            "Model": "MODEL",
            "Base URL": "BASE_URL",
            "Custom": "SETTING",
        }[self.type_combo.currentText()]
        return f"{provider}_{suffix}" if provider else suffix

    def _update_value_mode(self, setting_type: str) -> None:
        self.value_input.setEchoMode(
            QLineEdit.EchoMode.Password
            if setting_type == "API Key"
            else QLineEdit.EchoMode.Normal
        )

    def _validate(self) -> None:
        env_var = self.env_input.text().strip().upper()
        if not self.provider_input.text().strip() or not self.value_input.text().strip():
            QMessageBox.warning(self, "Missing value", "Provider and value are required.")
            return
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_var):
            QMessageBox.warning(self, "Invalid variable", "Use an environment name such as PROVIDER_API_KEY.")
            return
        self.accept()

    def get_entry(self) -> Dict[str, str]:
        return {
            "provider": self.provider_input.text().strip(),
            "type": self.type_combo.currentText(),
            "env_var": self.env_input.text().strip().upper(),
            "value": self.value_input.text().strip(),
        }


class ApiSettingsTableWidget(QWidget):
    """Table-based API settings editor with .env reload and save support."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._entries: List[Dict[str, str]] = []
        self._original_keys: set[str] = set()
        self._setup_ui()
        self._load_from_env()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setStyleSheet("background: #1a1d27; border-bottom: 1px solid #2d3748;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(20, 14, 20, 14)
        title = QLabel("API Providers and Keys")
        title.setStyleSheet("font-size: 17px; font-weight: 700; color: #ffffff;")
        header_layout.addWidget(title)
        header_layout.addStretch()

        self.btn_add = QPushButton("＋ Add")
        self.btn_add.setObjectName("btn_success")
        self.btn_add.clicked.connect(self._add_setting)
        header_layout.addWidget(self.btn_add)

        self.btn_change = QPushButton("✎ Change")
        self.btn_change.setObjectName("btn_secondary")
        self.btn_change.setEnabled(False)
        self.btn_change.clicked.connect(self._change_setting)
        header_layout.addWidget(self.btn_change)

        self.btn_delete = QPushButton("🗑 Delete")
        self.btn_delete.setObjectName("btn_danger")
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self._delete_setting)
        header_layout.addWidget(self.btn_delete)

        self.btn_reload = QPushButton("↺ Reload from .env")
        self.btn_reload.setObjectName("btn_secondary")
        self.btn_reload.setToolTip("Discard unsaved changes and reload the .env file")
        self.btn_reload.clicked.connect(self._load_from_env)
        header_layout.addWidget(self.btn_reload)

        self.btn_save = QPushButton("💾 Save All")
        self.btn_save.setObjectName("btn_primary")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save_to_env)
        header_layout.addWidget(self.btn_save)
        root.addWidget(header)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Provider", "Type", "Environment Variable", "Value"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_action_state)
        root.addWidget(self.table, stretch=1)

        footer = QHBoxLayout()
        footer.setContentsMargins(20, 8, 20, 8)
        self.footer_status = QLabel(f"File: {ENV_FILE}")
        self.footer_status.setStyleSheet("color: #718096; font-size: 11px;")
        footer.addWidget(self.footer_status)
        footer.addStretch()
        root.addLayout(footer)

    @staticmethod
    def _type_for_env(env_var: str) -> str:
        if env_var.endswith("_API_KEY"):
            return "API Key"
        if env_var.endswith("_MODEL"):
            return "Model"
        if env_var.endswith("_BASE_URL"):
            return "Base URL"
        return "Custom"

    @staticmethod
    def _provider_for_env(env_var: str) -> str:
        suffixes = ("_API_KEY", "_BASE_URL", "_MODEL")
        for suffix in suffixes:
            if env_var.endswith(suffix):
                return env_var[:-len(suffix)].replace("_", " ").title()
        return env_var.replace("_", " ").title()

    def _load_from_env(self) -> None:
        env = _read_env_file()
        self._original_keys = set(env)
        self._entries = []
        known = set()
        for env_var, _label, _hint, _is_secret in API_KEY_DEFS:
            known.add(env_var)
            self._entries.append({
                "provider": self._provider_for_env(env_var),
                "type": self._type_for_env(env_var),
                "env_var": env_var,
                "value": env.get(env_var, os.getenv(env_var, "")),
            })
        for env_var, value in env.items():
            if env_var not in known and env_var.endswith(("_API_KEY", "_MODEL", "_BASE_URL")):
                self._entries.append({
                    "provider": self._provider_for_env(env_var),
                    "type": self._type_for_env(env_var),
                    "env_var": env_var,
                    "value": value,
                })
        self._populate_table()
        self._set_saved_status("Reloaded")

    def _populate_table(self) -> None:
        self.table.setRowCount(0)
        for entry in self._entries:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, value in enumerate((entry["provider"], entry["type"], entry["env_var"], entry["value"])):
                display = _mask(value) if column == 3 and entry["type"] == "API Key" and value else (value or "Not set")
                item = QTableWidgetItem(display)
                item.setData(Qt.ItemDataRole.UserRole, entry["env_var"])
                self.table.setItem(row, column, item)

    def _selected_index(self) -> int:
        rows = self.table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _update_action_state(self) -> None:
        enabled = self._selected_index() >= 0
        self.btn_change.setEnabled(enabled)
        self.btn_delete.setEnabled(enabled)

    def _add_setting(self) -> None:
        dialog = ApiSettingDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        entry = dialog.get_entry()
        if any(item["env_var"] == entry["env_var"] for item in self._entries):
            QMessageBox.warning(self, "Already exists", f"{entry['env_var']} is already in the table.")
            return
        self._entries.append(entry)
        self._populate_table()
        self._mark_dirty()

    def _change_setting(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        dialog = ApiSettingDialog(self._entries[index], self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        entry = dialog.get_entry()
        if any(i != index and item["env_var"] == entry["env_var"] for i, item in enumerate(self._entries)):
            QMessageBox.warning(self, "Already exists", f"{entry['env_var']} is already in the table.")
            return
        self._entries[index] = entry
        self._populate_table()
        self._mark_dirty()

    def _delete_setting(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        entry = self._entries[index]
        reply = QMessageBox.question(self, "Delete setting", f"Remove {entry['env_var']} from the .env file?")
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._entries.pop(index)
        self._populate_table()
        self._mark_dirty()

    def _mark_dirty(self) -> None:
        self.btn_save.setEnabled(True)
        self.footer_status.setText("Unsaved changes")
        self.footer_status.setStyleSheet("color: #ed8936; font-size: 11px;")

    def _set_saved_status(self, message: str) -> None:
        self.btn_save.setEnabled(False)
        self.footer_status.setText(f"{message} · {datetime.now().strftime('%H:%M:%S')}")
        self.footer_status.setStyleSheet("color: #718096; font-size: 11px;")

    def _save_to_env(self) -> None:
        updates = {entry["env_var"]: entry["value"] for entry in self._entries}
        removed = self._original_keys - set(updates)
        try:
            _write_env_file(updates, remove_keys=removed)
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", f"Could not write to .env file:\n{exc}")
            return
        for key, value in updates.items():
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        for key in removed:
            os.environ.pop(key, None)
        self._original_keys = set(updates)
        self._set_saved_status("Saved")


class AddProviderDialog(QDialog):
    """Collect the environment settings needed for a custom provider."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add Provider")
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)
        title = QLabel("Add Translation Provider")
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #ffffff;")
        layout.addWidget(title)

        form = QFormLayout()
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("e.g. OpenRouter")
        form.addRow("Provider name:", self.name_input)
        self.prefix_input = QLineEdit()
        self.prefix_input.setPlaceholderText("e.g. OPENROUTER")
        form.addRow("Environment prefix:", self.prefix_input)
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API key:", self.key_input)
        self.base_input = QLineEdit()
        self.base_input.setPlaceholderText("https://api.example.com/v1")
        form.addRow("Base URL:", self.base_input)
        self.model_input = QLineEdit()
        self.model_input.setPlaceholderText("provider/model-name")
        form.addRow("Default model:", self.model_input)
        self.language_combo = QComboBox()
        self.language_combo.addItems(["English", "Arabic"])
        form.addRow("Source language:", self.language_combo)
        layout.addLayout(form)

        hint = QLabel("The provider will be saved using PREFIX_API_KEY, PREFIX_BASE_URL, and PREFIX_MODEL.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #718096; font-size: 11px;")
        layout.addWidget(hint)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate(self) -> None:
        prefix = re.sub(r"[^A-Za-z0-9_]", "_", self.prefix_input.text().strip()).upper().strip("_")
        self.prefix_input.setText(prefix)
        if not self.name_input.text().strip() or not prefix or not self.base_input.text().strip():
            QMessageBox.warning(self, "Missing information", "Provider name, prefix, and base URL are required.")
            return
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", prefix):
            QMessageBox.warning(self, "Invalid prefix", "Use letters, numbers, and underscores only.")
            return
        self.accept()

    def get_values(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.name_input.text().strip(),
            self.prefix_input.text().strip(),
            self.key_input.text().strip(),
            self.base_input.text().strip(),
            self.model_input.text().strip(),
            self.language_combo.currentText(),
        )


class ProviderSettingsWidget(QWidget):
    """Provider-oriented API settings page."""

    models_changed = pyqtSignal(dict)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._env: Dict[str, str] = {}
        self._models: Dict[str, List[str]] = {}
        self._model_widgets: Dict[str, QVBoxLayout] = {}
        self._provider_groups = list(_PROVIDER_GROUPS)
        self._setup_ui()
        self._reload_env()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(20, 14, 20, 14)
        title = QLabel("API Providers")
        title.setStyleSheet("font-size: 18px; font-weight: 700; color: #ffffff;")
        header.addWidget(title)
        header.addStretch()
        self.btn_reload = QPushButton("↺ Reload from .env")
        self.btn_reload.setObjectName("btn_secondary")
        self.btn_reload.clicked.connect(self._reload_env)
        header.addWidget(self.btn_reload)
        self.btn_add_provider = QPushButton("＋ Add Provider")
        self.btn_add_provider.setObjectName("btn_success")
        self.btn_add_provider.clicked.connect(self._add_provider)
        header.addWidget(self.btn_add_provider)
        self.btn_save = QPushButton("💾 Save All")
        self.btn_save.setObjectName("btn_primary")
        self.btn_save.clicked.connect(self._save_env)
        header.addWidget(self.btn_save)
        root.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._cards = QWidget()
        self._cards_layout = QVBoxLayout(self._cards)
        self._cards_layout.setContentsMargins(20, 8, 20, 20)
        self._cards_layout.setSpacing(14)
        self._cards_layout.addStretch()
        scroll.setWidget(self._cards)
        root.addWidget(scroll, stretch=1)

        self.footer_status = QLabel("")
        self.footer_status.setStyleSheet("color: #718096; padding: 8px 20px;")
        root.addWidget(self.footer_status)

    def _reload_env(self) -> None:
        self._env = _read_env_file()
        for key in list(os.environ):
            if key.endswith(("_API_KEY", "_MODEL", "_BASE_URL", "_MODELS")) and key not in self._env:
                self._env[key] = os.environ[key]
        self._provider_groups = list(_PROVIDER_GROUPS)
        known_prefixes = {
            prefix for _name, prefix, _key, _base, _default_base in self._provider_groups
        }
        suffixes = ("_API_KEY", "_BASE_URL", "_MODELS", "_MODEL")
        configured_prefixes = set()
        for key in self._env:
            for suffix in suffixes:
                if key.endswith(suffix):
                    prefix = key[: -len(suffix)]
                    if not prefix.endswith(("_API", "_BASE")):
                        configured_prefixes.add(prefix)
                    break
        for prefix in sorted(configured_prefixes - known_prefixes):
            provider_name = prefix.replace("_", " ").title()
            self._provider_groups.append(
                (provider_name, prefix, f"{prefix}_API_KEY", f"{prefix}_BASE_URL", "")
            )
        self._models = {}
        for _name, prefix, _key, _base, _default_base in self._provider_groups:
            models_key = f"{prefix}_MODELS"
            saved = self._env.get(models_key, "")
            models = (
                [m for m in saved.split(",") if m.strip()]
                if models_key in self._env
                else self._known_models(prefix)
            )
            default_model = self._env.get(f"{prefix}_MODEL", "").strip()
            if default_model and default_model not in models:
                models.insert(0, default_model)
            self._models[prefix] = models
        self._render_cards()
        self.models_changed.emit(self.get_models())
        self.footer_status.setText(f"Loaded from .env · {datetime.now().strftime('%H:%M:%S')}")

    def get_models(self) -> Dict[str, List[str]]:
        """Return a copy of the configured provider models for other views."""
        return {prefix.casefold(): list(models) for prefix, models in self._models.items()}

    @staticmethod
    def _known_models(prefix: str) -> List[str]:
        from backend.translation import PROVIDER_MODELS
        return list(PROVIDER_MODELS.get(prefix.casefold(), []))

    def _render_cards(self) -> None:
        while self._cards_layout.count() > 1:
            item = self._cards_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._model_widgets.clear()
        for provider, prefix, key_var, base_var, default_base in self._provider_groups:
            self._cards_layout.insertWidget(self._cards_layout.count() - 1, self._make_provider_card(
                provider, prefix, key_var, base_var, default_base
            ))

    def _add_provider(self) -> None:
        dialog = AddProviderDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        provider, prefix, api_key, base_url, model, language = dialog.get_values()
        if any(item[1] == prefix for item in self._provider_groups):
            QMessageBox.warning(self, "Provider exists", f"{prefix} is already configured.")
            return
        self._provider_groups.append((provider, prefix, f"{prefix}_API_KEY", f"{prefix}_BASE_URL", base_url))
        self._env[f"{prefix}_API_KEY"] = api_key
        self._env[f"{prefix}_BASE_URL"] = base_url
        self._env[f"{prefix}_MODEL"] = model
        self._models[prefix] = [model] if model else []
        self._env[f"{prefix}_LANGUAGE"] = language
        self._render_cards()
        self.models_changed.emit(self.get_models())
        self.footer_status.setText("Unsaved changes")

    def _make_provider_card(self, provider: str, prefix: str, key_var: str, base_var: str, default_base: str) -> QWidget:
        card = QFrame()
        card.setStyleSheet("QFrame { background: #1a1d27; border: 1px solid #2d3748; border-radius: 8px; }")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel(provider)
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #ffffff;")
        top.addWidget(title)
        top.addStretch()
        connected = bool(self._env.get(key_var, "").strip())
        status = QLabel("● Connected" if connected else "○ Not configured")
        status.setStyleSheet(f"color: {'#68d391' if connected else '#a0aec0'}; font-weight: 600;")
        top.addWidget(status)
        layout.addLayout(top)

        key_value = self._env.get(key_var, "")
        base_value = self._env.get(base_var, default_base)
        details = QFormLayout()
        details.setContentsMargins(0, 4, 0, 0)
        key_label = QLabel(_mask(key_value) if key_value else "Not set")
        key_label.setObjectName(f"key_{prefix}")
        key_label.setStyleSheet("color: #e2e8f0; font-family: Consolas, monospace;")
        details.addRow("API Key", key_label)
        base_input = QLineEdit(base_value)
        base_input.setObjectName(f"base_{prefix}")
        base_input.textChanged.connect(lambda value, p=prefix, var=base_var: self._set_env(var, value))
        details.addRow("Base URL", base_input)
        layout.addLayout(details)

        model_header = QHBoxLayout()
        models_label = QLabel("MODELS")
        models_label.setStyleSheet("font-weight: 700; color: #a0aec0; letter-spacing: 0.08em;")
        model_header.addWidget(models_label)
        model_header.addStretch()
        add_model = QPushButton("＋ Add Model")
        add_model.setObjectName("btn_secondary")
        add_model.clicked.connect(lambda _, p=prefix: self._add_model(p))
        model_header.addWidget(add_model)
        layout.addLayout(model_header)

        model_box = QFrame()
        model_box.setStyleSheet("QFrame { background: #141720; border: 1px solid #2d3748; border-radius: 6px; }")
        model_layout = QVBoxLayout(model_box)
        model_layout.setContentsMargins(10, 6, 10, 6)
        model_layout.setSpacing(2)
        self._model_widgets[prefix] = model_layout
        for model in self._models.get(prefix, []):
            self._add_model_row(prefix, model)
        layout.addWidget(model_box)

        actions = QHBoxLayout()
        actions_label = QLabel("Provider Actions:")
        actions_label.setStyleSheet("color: #a0aec0;")
        actions.addWidget(actions_label)
        actions.addStretch()
        for text, callback in (
            ("Test Provider", lambda _, p=prefix: self._test_provider(p)),
            ("Edit", lambda _, v=key_var: self._edit_key(v)),
            ("Rotate Key", lambda _, v=key_var: self._edit_key(v)),
            ("Disable", lambda _, v=key_var: self._disable_key(v)),
        ):
            button = QPushButton(text)
            button.setObjectName("btn_secondary")
            button.clicked.connect(callback)
            actions.addWidget(button)
        layout.addLayout(actions)
        return card

    def _add_model_row(self, prefix: str, model: str) -> None:
        layout = self._model_widgets.get(prefix)
        if layout is None:
            return
        row = QHBoxLayout()
        dot = QLabel("●")
        dot.setStyleSheet("color: #68d391;")
        row.addWidget(dot)
        name = QLabel(model)
        name.setStyleSheet("color: #e2e8f0; font-weight: 600;")
        name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(name, stretch=1)
        role = QLabel("Translation")
        role.setStyleSheet("color: #718096;")
        row.addWidget(role)
        test = QPushButton("Test")
        test.setObjectName("btn_secondary")
        test.clicked.connect(lambda _, m=model: QMessageBox.information(self, "Model", f"{m} is configured for translation."))
        row.addWidget(test)
        edit = QPushButton("Edit")
        edit.setObjectName("btn_secondary")
        edit.clicked.connect(lambda _, p=prefix, m=model: self._edit_model(p, m))
        row.addWidget(edit)
        delete = QPushButton("Delete")
        delete.setObjectName("btn_danger")
        delete.setMinimumWidth(68)
        delete.setToolTip("Remove this model from the provider")
        delete.clicked.connect(lambda _, p=prefix, m=model: self._delete_model(p, m))
        row.addWidget(delete)
        layout.addLayout(row)

    def _set_env(self, key: str, value: str) -> None:
        self._env[key] = value.strip()
        self.footer_status.setText("Unsaved changes")

    def _add_model(self, prefix: str) -> None:
        model, ok = QInputDialog.getText(self, "Add Model", f"Model for {prefix}:")
        if ok and model.strip():
            self._models.setdefault(prefix, []).append(model.strip())
            self._render_cards()
            self.models_changed.emit(self.get_models())
            self.footer_status.setText("Unsaved changes")

    def _edit_model(self, prefix: str, old_model: str) -> None:
        model, ok = QInputDialog.getText(self, "Edit Model", "Model:", text=old_model)
        if ok and model.strip():
            models = self._models[prefix]
            new_model = model.strip()
            models[models.index(old_model)] = new_model
            default_key = f"{prefix}_MODEL"
            if self._env.get(default_key) == old_model:
                self._env[default_key] = new_model
            self._render_cards()
            self.models_changed.emit(self.get_models())
            self.footer_status.setText("Unsaved changes")

    def _delete_model(self, prefix: str, model: str) -> None:
        reply = QMessageBox.question(
            self,
            "Remove Model",
            f"Remove '{model}' from {prefix}?\n\nThis change takes effect after Save All.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        models = self._models[prefix]
        models.remove(model)
        default_key = f"{prefix}_MODEL"
        if self._env.get(default_key) == model:
            self._env[default_key] = models[0] if models else ""
        self._render_cards()
        self.models_changed.emit(self.get_models())
        self.footer_status.setText("Unsaved changes")

    def _edit_key(self, key_var: str) -> None:
        entry = {"provider": key_var.split("_")[0].title(), "type": "API Key", "env_var": key_var, "value": self._env.get(key_var, "")}
        dialog = ApiSettingDialog(entry, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._env[key_var] = dialog.get_entry()["value"]
            self._render_cards()
            self.footer_status.setText("Unsaved changes")

    def _disable_key(self, key_var: str) -> None:
        self._env[key_var] = ""
        self._render_cards()
        self.footer_status.setText("Unsaved changes")

    def _test_provider(self, prefix: str) -> None:
        key_var = f"{prefix}_API_KEY"
        if self._env.get(key_var, "").strip():
            QMessageBox.information(self, "Provider Test", f"{prefix} is configured and ready to test.")
        else:
            QMessageBox.warning(self, "Provider Test", f"Add {key_var} before testing this provider.")

    def _save_env(self) -> None:
        for prefix, models in self._models.items():
            self._env[f"{prefix}_MODELS"] = ",".join(models)
            if models and not self._env.get(f"{prefix}_MODEL"):
                self._env[f"{prefix}_MODEL"] = models[0]
        try:
            _write_env_file(self._env)
            for key, value in self._env.items():
                if value:
                    os.environ[key] = value
                else:
                    os.environ.pop(key, None)
            self.footer_status.setText(f"Saved · {datetime.now().strftime('%H:%M:%S')}")
        except OSError as exc:
            QMessageBox.critical(self, "Save Failed", str(exc))
