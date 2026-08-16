"""
NovelBridge GUI — Glossary editor widget.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from backend.database import (
    add_glossary_rule, delete_glossary_rule, get_all_glossary_rules,
)
from backend.models import GlossaryRule


class AddRuleDialog(QDialog):
    """Modal dialog for adding a new glossary rule."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add Glossary Rule")
        self.setFixedSize(420, 220)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("Add Translation Rule")
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: #ffffff;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(10)

        self.source_input = QLineEdit()
        self.source_input.setPlaceholderText("e.g. god tier")
        form.addRow("English term:", self.source_input)

        self.target_input = QLineEdit()
        self.target_input.setPlaceholderText("e.g. مستوى مكرم")
        form.addRow("Arabic translation:", self.target_input)

        self.notes_input = QLineEdit()
        self.notes_input.setPlaceholderText("Optional note")
        form.addRow("Notes:", self.notes_input)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate_and_accept(self):
        if not self.source_input.text().strip():
            QMessageBox.warning(self, "Validation", "English term cannot be empty.")
            return
        if not self.target_input.text().strip():
            QMessageBox.warning(self, "Validation", "Arabic translation cannot be empty.")
            return
        self.accept()

    def get_values(self) -> tuple[str, str, str]:
        return (
            self.source_input.text().strip(),
            self.target_input.text().strip(),
            self.notes_input.text().strip(),
        )


class GlossaryEditorWidget(QWidget):
    """Full glossary editor with add/delete support."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rules: list[GlossaryRule] = []
        self._setup_ui()
        self.refresh()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Header row
        header_row = QHBoxLayout()
        title = QLabel("📖 Translation Glossary")
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: #e2e8f0;")
        header_row.addWidget(title)
        header_row.addStretch()

        self.btn_add = QPushButton("＋ Add Rule")
        self.btn_add.setObjectName("btn_success")
        self.btn_add.clicked.connect(self._on_add)
        header_row.addWidget(self.btn_add)

        self.btn_delete = QPushButton("🗑 Delete Selected")
        self.btn_delete.setObjectName("btn_danger")
        self.btn_delete.clicked.connect(self._on_delete)
        header_row.addWidget(self.btn_delete)

        layout.addLayout(header_row)

        info = QLabel("These rules are injected into every translation prompt and applied via regex post-pass.")
        info.setStyleSheet("color: #718096; font-size: 12px;")
        layout.addWidget(info)

        # Table
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["ID", "English Term", "Arabic Translation", "Notes"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)

    def refresh(self) -> None:
        self._rules = get_all_glossary_rules()
        self.table.setRowCount(0)
        for rule in self._rules:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(rule.id)))
            self.table.setItem(row, 1, QTableWidgetItem(rule.source_term))
            arabic = QTableWidgetItem(rule.target_term)
            arabic.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 2, arabic)
            self.table.setItem(row, 3, QTableWidgetItem(rule.notes or ""))

    def _on_add(self) -> None:
        dialog = AddRuleDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            source, target, notes = dialog.get_values()
            add_glossary_rule(source, target, notes)
            self.refresh()

    def _on_delete(self) -> None:
        rows = {idx.row() for idx in self.table.selectedIndexes()}
        if not rows:
            QMessageBox.information(self, "Delete", "Please select one or more rules to delete.")
            return
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Delete {len(rows)} rule(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            for row in rows:
                if 0 <= row < len(self._rules):
                    delete_glossary_rule(self._rules[row].id)
            self.refresh()
