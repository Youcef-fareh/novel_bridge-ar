"""
NovelBridge GUI — Chapter status table widget.
"""
from __future__ import annotations

from typing import List

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from backend.models import Chapter, ChapterStatus

_STATUS_COLORS = {
    ChapterStatus.pending:     "#718096",
    ChapterStatus.scraped:     "#63b3ed",
    ChapterStatus.translating: "#f6e05e",
    ChapterStatus.translated:  "#68d391",
    ChapterStatus.failed:      "#fc8181",
}

_STATUS_LABELS = {
    ChapterStatus.pending:     "⏳ Pending",
    ChapterStatus.scraped:     "📥 Scraped",
    ChapterStatus.translating: "🔄 Translating",
    ChapterStatus.translated:  "✅ Translated",
    ChapterStatus.failed:      "❌ Failed",
}


class ChapterTableWidget(QWidget):
    """Displays chapter list with status colour coding."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Select", "#", "Title", "Status", "Translation"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(False)
        self.table.verticalHeader().setVisible(False)
        self.table.cellClicked.connect(self._on_cell_clicked)
        layout.addWidget(self.table)

    def _on_cell_clicked(self, row: int, column: int) -> None:
        """Toggle selection when the user clicks anywhere in a chapter row."""
        if column == 0:
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )

    def set_chapters(self, chapters: List[Chapter], is_native_arabic: bool = False) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        self._is_native_arabic = is_native_arabic
        if is_native_arabic:
            self.table.setHorizontalHeaderLabels(["Select", "#", "Title", "Status", "Content"])
        else:
            self.table.setHorizontalHeaderLabels(["Select", "#", "Title", "Status", "Translation"])

        for chapter in chapters:
            row = self.table.rowCount()
            self.table.insertRow(row)

            # Checkbox selection is independent from row highlighting and survives sorting.
            select_item = QTableWidgetItem()
            select_item.setCheckState(Qt.CheckState.Unchecked)
            select_item.setData(Qt.ItemDataRole.UserRole, chapter.id)
            select_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 0, select_item)

            # Index
            idx_item = QTableWidgetItem(str(chapter.index + 1))
            idx_item.setData(Qt.ItemDataRole.UserRole, chapter.id)
            idx_item.setData(Qt.ItemDataRole.DisplayRole, chapter.index + 1)
            idx_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 1, idx_item)

            # Title
            title_item = QTableWidgetItem(chapter.title)
            title_item.setData(Qt.ItemDataRole.UserRole, chapter.id)
            self.table.setItem(row, 2, title_item)

            # Status with colour
            status_label = _STATUS_LABELS.get(chapter.status, chapter.status)
            status_item = QTableWidgetItem(status_label)
            status_item.setData(Qt.ItemDataRole.UserRole, chapter.id)
            status_color = _STATUS_COLORS.get(chapter.status, "#718096")
            status_item.setForeground(QColor(status_color))
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 3, status_item)

            # Has translation / content
            if is_native_arabic:
                has_content = "✅ Ready (Arabic)" if (chapter.raw_text and chapter.raw_text.strip()) else "—"
                trans_item = QTableWidgetItem(has_content)
                trans_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if chapter.raw_text and chapter.raw_text.strip():
                    trans_item.setForeground(QColor("#68d391"))
            else:
                has_trans = "✅ Yes" if chapter.translated_text else "—"
                trans_item = QTableWidgetItem(has_trans)
                trans_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if chapter.translated_text:
                    trans_item.setForeground(QColor("#68d391"))
            trans_item.setData(Qt.ItemDataRole.UserRole, chapter.id)
            self.table.setItem(row, 4, trans_item)

        self.table.setSortingEnabled(True)

    def get_selected_chapter_ids(self, chapters: List[Chapter]) -> List[int]:
        """Return IDs of highlighted rows for backwards compatibility."""
        rows = {idx.row() for idx in self.table.selectedIndexes()}
        result = []
        for row in rows:
            item = self.table.item(row, 1)
            if item:
                chapter_id = item.data(Qt.ItemDataRole.UserRole)
                if chapter_id is not None:
                    result.append(chapter_id)
        return sorted(result)

    def get_checked_chapter_ids(self) -> List[int]:
        """Return IDs of chapters selected with their checkboxes."""
        result = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                chapter_id = item.data(Qt.ItemDataRole.UserRole)
                if chapter_id is not None:
                    result.append(chapter_id)
        return sorted(result)

    def get_stats(self, chapters: List[Chapter], is_native_arabic: bool = False) -> dict:
        total = len(chapters)
        if is_native_arabic:
            ready = sum(1 for c in chapters if c.raw_text and c.raw_text.strip())
            scraped = ready
            failed = sum(1 for c in chapters if c.status == ChapterStatus.failed)
            return {"total": total, "translated": ready, "scraped": scraped, "failed": failed}
        else:
            translated = sum(1 for c in chapters if c.status == ChapterStatus.translated)
            scraped = sum(1 for c in chapters if c.status == ChapterStatus.scraped)
            failed = sum(1 for c in chapters if c.status == ChapterStatus.failed)
            return {"total": total, "translated": translated, "scraped": scraped, "failed": failed}
