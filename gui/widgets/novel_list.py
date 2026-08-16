"""
NovelBridge GUI — Novel list sidebar widget.
"""
from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QVBoxLayout, QWidget,
)

from backend.models import Novel, NovelStatus

_STATUS_EMOJI = {
    NovelStatus.pending:   "⏳",
    NovelStatus.scraping:  "🔄",
    NovelStatus.scraped:   "📥",
    NovelStatus.failed:    "❌",
}


class NovelListWidget(QWidget):
    """Left-sidebar novel list with status indicators and right-click delete."""

    novel_selected = pyqtSignal(int)  # emits novel_id
    novel_deleted = pyqtSignal(int)   # emits novel_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._novels: List[Novel] = []
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header = QLabel("📚 Library")
        header.setObjectName("label_heading")
        header.setStyleSheet("font-size: 15px; font-weight: 700; color: #e2e8f0; padding: 8px 4px 4px 4px;")
        layout.addWidget(header)

        self.list_widget = QListWidget()
        self.list_widget.setAlternatingRowColors(False)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.list_widget)

    def set_novels(self, novels: List[Novel]) -> None:
        self._novels = novels
        self.list_widget.clear()
        for novel in novels:
            emoji = _STATUS_EMOJI.get(novel.status, "📖")
            item = QListWidgetItem(f"{emoji}  {novel.title}")
            item.setData(Qt.ItemDataRole.UserRole, novel.id)
            status_str = novel.status.value if hasattr(novel.status, "value") else str(novel.status)
            item.setToolTip(f"Title: {novel.title}\nSource: {novel.source_url}\nStatus: {status_str.capitalize()}")
            self.list_widget.addItem(item)

    def _on_item_clicked(self, item: QListWidgetItem):
        novel_id = item.data(Qt.ItemDataRole.UserRole)
        if novel_id:
            self.novel_selected.emit(novel_id)

    def _on_context_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if not item:
            return
        novel_id = item.data(Qt.ItemDataRole.UserRole)
        from PyQt6.QtWidgets import QMenu
        from PyQt6.QtGui import QAction
        menu = QMenu(self)
        delete_action = QAction("🗑️ Delete Novel", self)
        delete_action.triggered.connect(lambda: self.novel_deleted.emit(novel_id))
        menu.addAction(delete_action)
        menu.exec(self.list_widget.mapToGlobal(pos))

    def get_selected_novel_id(self) -> Optional[int]:
        item = self.list_widget.currentItem()
        if item:
            return item.data(Qt.ItemDataRole.UserRole)
        return None

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            novel_id = self.get_selected_novel_id()
            if novel_id:
                self.novel_deleted.emit(novel_id)
                return
        super().keyPressEvent(event)

    def select_novel(self, novel_id: int) -> None:
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == novel_id:
                self.list_widget.setCurrentItem(item)
                break
