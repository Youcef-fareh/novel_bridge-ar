"""GUI workspace for adding and testing custom scraping adapters."""
from __future__ import annotations

import importlib.util
import inspect
import re
import shutil
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QFileDialog,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from backend.adapters.base import AdapterRegistry, SiteAdapter


_CUSTOM_DIR = Path(__file__).parent.parent.parent / "backend" / "adapters" / "custom"
_BUILTIN_SITE_IDS = {"novelfire", "novelphoenix", "galaxynovels", "lightnovelpub", "ranovel", "wuxiaspot"}
_HIDDEN_SITE_IDS = {"galaxynovels"}


def _adapter_template(site_id: str, class_name: str, domain: str, method: str, language: str) -> str:
    fetch_code = {
        "http": 'return await fetch_html(url, SITE_ID)',
        "curl": 'return await fetch_html(url, SITE_ID)',
        "playwright": 'return await fetch_html_playwright(url, wait_for)',
    }[method]
    return f'''"""Generated adapter template for {domain}."""
from __future__ import annotations

from typing import List
from urllib.parse import urljoin

from backend.adapters.base import ChapterRef, NovelMeta, SiteAdapter
from backend.adapters.fetcher import (
    fetch_html,
    fetch_html_playwright,
    parse_chapter_body,
    parse_links,
    parse_text,
)

SITE_ID = "{site_id}"
BASE_URL = "https://{domain}"


class {class_name}(SiteAdapter):
    site_id = SITE_ID
    is_native_arabic = {language == "Arabic"!r}
    source_language = "{language}"
    scraping_method = "{method}"

    def can_handle(self, url: str) -> bool:
        return "{domain}" in url.lower()

    async def _fetch(self, url: str, wait_for: str = "") -> str:
        {fetch_code}

    async def get_novel_metadata(self, novel_url: str) -> NovelMeta:
        html = await self._fetch(novel_url, "h1")
        title = parse_text(html, ["h1", "title"]) or "Unknown Title"
        return NovelMeta(
            title=title,
            author=parse_text(html, [".author", "[rel=author]"]),
            description=parse_text(html, [".description", ".summary", "article"]),
            source_url=novel_url,
            source_site=SITE_ID,
        )

    async def get_chapter_list(self, novel_url: str) -> List[ChapterRef]:
        html = await self._fetch(novel_url, ".chapter-list, .chapters, article")
        refs = []
        for index, (title, href) in enumerate(parse_links(
            html,
            [".chapter-list a", ".chapters a", "a[href*=chapter]"],
            base_url=BASE_URL,
        )):
            refs.append(ChapterRef(
                index=index,
                title=title or f"Chapter {{index + 1}}",
                source_url=urljoin(BASE_URL, href),
            ))
        return refs

    async def get_chapter_text(self, chapter_url: str) -> str:
        html = await self._fetch(chapter_url, ".chapter-content, article, main")
        return parse_chapter_body(
            html,
            [".chapter-content", ".chapter-body", "article", "main"],
        ) or parse_text(html, [".chapter-content", "article", "main"]) or ""
'''


def load_custom_adapters() -> list[str]:
    """Load adapter plugins saved by the Add Website page."""
    loaded = []
    if not _CUSTOM_DIR.exists():
        return loaded
    for path in sorted(_CUSTOM_DIR.glob("*.py")):
        try:
            module_name = f"novelbridge_custom_{path.stem}"
            spec = importlib.util.spec_from_file_location(module_name, path)
            if not spec or not spec.loader:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for value in vars(module).values():
                if isinstance(value, type) and issubclass(value, SiteAdapter) and value is not SiteAdapter:
                    adapter = value()
                    if adapter.site_id:
                        AdapterRegistry.register(adapter)
                        loaded.append(adapter.site_id)
        except Exception:
            continue
    return loaded


class WebsiteSetupWidget(QWidget):
    """Help users choose a scraping method and install an adapter plugin."""

    adapter_added = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)
        scroll.setWidget(content)

        title = QLabel("Add A Website")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #ffffff;")
        root.addWidget(title)

        intro = QLabel(
            "Connect another novel website by choosing the simplest method that fits it. "
            "Existing adapters remain unchanged."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #a0aec0; font-size: 13px;")
        root.addWidget(intro)

        method_frame = QFrame()
        method_frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        method_frame.setStyleSheet(
            "QFrame { background: #1a1d27; border: 1px solid #2d3748; border-radius: 8px; }"
        )
        method_layout = QVBoxLayout(method_frame)
        method_layout.setContentsMargins(16, 14, 16, 14)
        method_layout.setSpacing(8)
        method_title = QLabel("Recommended scraping method")
        method_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #e2e8f0;")
        method_layout.addWidget(method_title)

        self.method_combo = QComboBox()
        self.method_combo.setMinimumHeight(42)
        self.method_combo.addItems([
            "HTTP + selectolax (fast, static HTML)",
            "curl_cffi (Cloudflare or browser-like requests)",
            "Playwright (JavaScript, accordions, protected pages)",
        ])
        self.method_combo.currentIndexChanged.connect(self._update_method_help)
        method_layout.addWidget(self.method_combo)

        self.method_help = QLabel()
        self.method_help.setWordWrap(True)
        self.method_help.setStyleSheet("color: #718096; font-size: 12px;")
        method_layout.addWidget(self.method_help)
        root.addWidget(method_frame)

        site_frame = QFrame()
        site_frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        site_frame.setStyleSheet(
            "QFrame { background: #141720; border: 1px solid #2d3748; border-radius: 8px; }"
        )
        site_layout = QVBoxLayout(site_frame)
        site_layout.setContentsMargins(16, 14, 16, 14)
        site_title = QLabel("Check a website")
        site_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #e2e8f0;")
        site_layout.addWidget(site_title)
        site_form = QVBoxLayout()
        site_form.setSpacing(8)
        self.site_url = QLineEdit()
        self.site_url.setPlaceholderText("https://example-novel-site.com/novel/...")
        self.site_url.setMinimumHeight(42)
        self.site_url.textChanged.connect(self._suggest_method)
        site_form.addWidget(self.site_url)
        self.btn_check = QPushButton("Check Adapter")
        self.btn_check.setObjectName("btn_secondary")
        self.btn_check.setMinimumHeight(42)
        self.btn_check.clicked.connect(self._check_site)
        site_form.addWidget(self.btn_check)
        language_label = QLabel("Source language")
        language_label.setStyleSheet("color: #a0aec0; font-size: 12px;")
        site_form.addWidget(language_label)
        self.language_combo = QComboBox()
        self.language_combo.setMinimumHeight(42)
        self.language_combo.addItems(["English (translate it)", "Arabic (already translated)"])
        site_form.addWidget(self.language_combo)
        site_layout.addLayout(site_form)
        self.btn_generate = QPushButton("⚙ Generate Adapter Template")
        self.btn_generate.setObjectName("btn_success")
        self.btn_generate.setMinimumHeight(44)
        self.btn_generate.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.btn_generate.clicked.connect(self._generate_template)
        site_layout.addWidget(self.btn_generate)
        self.site_result = QLabel("")
        self.site_result.setWordWrap(True)
        site_layout.addWidget(self.site_result)
        root.addWidget(site_frame)

        actions_frame = QFrame()
        actions_frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        actions_frame.setStyleSheet(
            "QFrame { background: #1a1d27; border: 1px solid #2d3748; border-radius: 8px; }"
        )
        actions_layout = QVBoxLayout(actions_frame)
        actions_layout.setContentsMargins(16, 14, 16, 14)
        actions_title = QLabel("Install an adapter")
        actions_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #e2e8f0;")
        actions_layout.addWidget(actions_title)

        self.btn_upload = QPushButton("⬆ Upload Adapter File")
        self.btn_upload.setObjectName("btn_secondary")
        self.btn_upload.setMinimumHeight(44)
        self.btn_upload.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.btn_upload.clicked.connect(self._import_local_file)
        actions_layout.addWidget(self.btn_upload)

        github_row = QHBoxLayout()
        self.github_url = QLineEdit()
        self.github_url.setPlaceholderText("GitHub raw .py URL")
        self.github_url.setMinimumHeight(42)
        github_row.addWidget(self.github_url, stretch=1)
        self.btn_github = QPushButton("＋ Import from GitHub")
        self.btn_github.setObjectName("btn_success")
        self.btn_github.setMinimumHeight(44)
        self.btn_github.clicked.connect(self._import_github_file)
        github_row.addWidget(self.btn_github)
        actions_layout.addLayout(github_row)

        self.btn_browse_github = QPushButton("Open GitHub adapter search")
        self.btn_browse_github.setObjectName("btn_secondary")
        self.btn_browse_github.setMinimumHeight(42)
        self.btn_browse_github.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://github.com/search?q=python+SiteAdapter+scraper&type=code"))
        )
        actions_layout.addWidget(self.btn_browse_github)

        self.install_result = QLabel(
            "Imported adapters are copied to backend/adapters/custom and loaded for this app session."
        )
        self.install_result.setWordWrap(True)
        self.install_result.setStyleSheet("color: #718096; font-size: 12px;")
        actions_layout.addWidget(self.install_result)
        root.addWidget(actions_frame)
        root.addStretch()
        self._update_method_help(0)

    def _suggest_method(self, url: str) -> None:
        lowered = url.casefold()
        if any(token in lowered for token in ("cloudflare", "protected", "wordpress", "wp-")):
            index = 1
        elif any(token in lowered for token in ("app", "javascript", "dynamic", "series")):
            index = 2
        else:
            index = 0
        self.method_combo.setCurrentIndex(index)

    def _update_method_help(self, index: int) -> None:
        messages = (
            "Use this for ordinary server-rendered HTML. It is the fastest option and works with selectolax CSS selectors.",
            "Use this when the site blocks normal requests or expects a Chrome-like TLS fingerprint. It is faster than a full browser.",
            "Use this when chapter links or text appear only after JavaScript runs, or when the site requires clicks and browser state.",
        )
        self.method_help.setText(messages[index])

    def _check_site(self) -> None:
        url = self.site_url.text().strip()
        if not url.startswith(("http://", "https://")):
            self.site_result.setText("Enter a complete http:// or https:// URL.")
            self.site_result.setStyleSheet("color: #fc8181; font-size: 12px;")
            return
        adapter = AdapterRegistry.find(url)
        if adapter:
            self.site_result.setText(f"Supported by {type(adapter).__name__} ({adapter.site_id}).")
            self.site_result.setStyleSheet("color: #68d391; font-size: 12px;")
        else:
            self.site_result.setText(
                "No installed adapter recognizes this URL. Choose a method above, then import an adapter file or a GitHub raw .py file."
            )
            self.site_result.setStyleSheet("color: #ed8936; font-size: 12px;")

    def _generate_template(self) -> None:
        url = self.site_url.text().strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            QMessageBox.warning(self, "Website URL", "Enter a complete website URL first.")
            return
        if AdapterRegistry.find(url):
            QMessageBox.information(
                self,
                "Already supported",
                "This website already has an installed adapter. Use it from the Library tab.",
            )
            return

        domain = parsed.netloc.lower().split(":", 1)[0]
        site_id = re.sub(r"[^a-z0-9]+", "_", domain.removeprefix("www.")).strip("_") or "custom_site"
        class_name = "".join(part.title() for part in site_id.split("_")) + "Adapter"
        method = ("http", "curl", "playwright")[self.method_combo.currentIndex()]
        language = "Arabic" if self.language_combo.currentIndex() == 1 else "English"
        source = _adapter_template(site_id, class_name, domain, method, language)
        try:
            _CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
            target = _CUSTOM_DIR / f"{site_id}_adapter.py"
            target.write_text(source, encoding="utf-8")
            load_custom_adapters()
            if site_id not in AdapterRegistry._adapters:
                raise RuntimeError("The generated adapter could not be loaded.")
            self.install_result.setText(
                f"Generated and installed {class_name} using {method}. "
                "Try adding the novel now. If scraping fails, import a site-specific adapter from GitHub."
            )
            self.install_result.setStyleSheet("color: #68d391; font-size: 12px;")
            self.adapter_added.emit(site_id)
        except Exception as exc:
            QMessageBox.critical(self, "Template Generation Failed", str(exc))

    def _import_local_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select adapter Python file", str(Path.home()), "Python files (*.py)"
        )
        if file_path:
            self._install_adapter(Path(file_path))

    def _import_github_file(self) -> None:
        url = self.github_url.text().strip()
        if not url.startswith(("https://raw.githubusercontent.com/", "https://github.com/")):
            QMessageBox.warning(self, "GitHub URL", "Use a GitHub file URL or a raw.githubusercontent.com URL.")
            return
        if "github.com/" in url and "raw.githubusercontent.com" not in url:
            match = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)", url)
            if not match:
                QMessageBox.warning(self, "GitHub URL", "Use a GitHub file URL ending in /blob/branch/path.py.")
                return
            owner, repo, branch, path = match.groups()
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                content = response.read()
            _CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
            target = _CUSTOM_DIR / Path(url.split("/")[-1]).name
            target.write_bytes(content)
            self._install_adapter(target, already_copied=True)
        except Exception as exc:
            QMessageBox.critical(self, "GitHub Import Failed", str(exc))

    def _install_adapter(self, source: Path, already_copied: bool = False) -> None:
        if source.suffix.lower() != ".py":
            QMessageBox.warning(self, "Adapter file", "Select a Python .py adapter file.")
            return
        try:
            _CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
            target = source if already_copied else _CUSTOM_DIR / source.name
            if not already_copied:
                shutil.copy2(source, target)
            before = set(AdapterRegistry._adapters)
            load_custom_adapters()
            found = [
                f"{site_id} ({type(AdapterRegistry._adapters[site_id]).__name__})"
                for site_id in AdapterRegistry._adapters
                if site_id not in before
            ]
            if not found:
                raise RuntimeError("No SiteAdapter subclass with a site_id was found in this file.")
            self.install_result.setText(f"Installed: {', '.join(found)}")
            self.install_result.setStyleSheet("color: #68d391; font-size: 12px;")
            self.adapter_added.emit(", ".join(found))
        except Exception as exc:
            QMessageBox.critical(self, "Adapter Import Failed", str(exc))


class AdapterRegistryWidget(QWidget):
    """Show installed adapters and allow removal of custom plugins."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._setup_ui()
        self.refresh()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Adapter Registry")
        title.setStyleSheet("font-size: 22px; font-weight: 700; color: #ffffff;")
        header.addWidget(title)
        header.addStretch()
        self.btn_refresh = QPushButton("↻ Refresh")
        self.btn_refresh.setObjectName("btn_secondary")
        self.btn_refresh.clicked.connect(self.refresh)
        header.addWidget(self.btn_refresh)
        self.btn_delete = QPushButton("🗑 Delete Custom Adapter")
        self.btn_delete.setObjectName("btn_danger")
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self._delete_selected)
        header.addWidget(self.btn_delete)
        layout.addLayout(header)

        info = QLabel(
            "Built-in adapters are protected. Only adapters installed in backend/adapters/custom can be deleted."
        )
        info.setStyleSheet("color: #a0aec0; font-size: 13px;")
        info.setWordWrap(True)
        layout.addWidget(info)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            "Site ID", "Adapter Class", "Source", "Language", "Method", "File"
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._update_delete_state)
        layout.addWidget(self.table, stretch=1)

    def refresh(self) -> None:
        self.table.setRowCount(0)
        for site_id, adapter in sorted(AdapterRegistry._adapters.items()):
            if site_id in _HIDDEN_SITE_IDS:
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            source = "Built-in" if site_id in _BUILTIN_SITE_IDS else "Custom"
            custom_file = next(
                (path.name for path in _CUSTOM_DIR.glob("*.py") if site_id in path.read_text(encoding="utf-8", errors="ignore")),
                "",
            ) if source == "Custom" and _CUSTOM_DIR.exists() else ""
            language, method = self._adapter_details(site_id, adapter, custom_file)
            values = (site_id, type(adapter).__name__, source, language, method, custom_file)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, site_id)
                self.table.setItem(row, column, item)
        self._update_delete_state()

    def _adapter_details(self, site_id: str, adapter: SiteAdapter, custom_file: str) -> tuple[str, str]:
        language = getattr(adapter, "source_language", "")
        if language not in {"Arabic", "English"}:
            if site_id in {"wtrlab", "galaxynovels", "novelfire", "novelphoenix"}:
                language = "Arabic" if getattr(adapter, "is_native_arabic", False) else "English"
            else:
                language = "Unknown"
        if site_id in {"galaxynovels"}:
            return language, "Playwright"
        if site_id in {"novelfire", "novelphoenix"}:
            return language, "curl_cffi"

        declared_method = getattr(adapter, "scraping_method", "")
        if declared_method in {"http", "curl", "playwright"}:
            return language, {
                "http": "HTTP",
                "curl": "curl_cffi",
                "playwright": "Playwright",
            }[declared_method]

        try:
            source = inspect.getsource(type(adapter)).casefold()
        except (OSError, TypeError):
            source = ""
        if "fetch_html_playwright" in source or "async_playwright" in source:
            method = "Playwright"
        elif "curlsession" in source or "curl_cffi" in source:
            method = "curl_cffi"
        elif "fetch_html" in source:
            method = "HTTP"
        else:
            method = "Unknown"
        return language, method

    def _update_delete_state(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            self.btn_delete.setEnabled(False)
            return
        site_id = self.table.item(rows[0].row(), 0).text()
        self.btn_delete.setEnabled(site_id not in _BUILTIN_SITE_IDS)

    def _delete_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        site_id = self.table.item(rows[0].row(), 0).text()
        if site_id in _BUILTIN_SITE_IDS:
            return
        reply = QMessageBox.question(
            self,
            "Delete Custom Adapter",
            f"Remove the custom adapter '{site_id}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        AdapterRegistry._adapters.pop(site_id, None)
        if _CUSTOM_DIR.exists():
            for path in _CUSTOM_DIR.glob("*.py"):
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore")
                    if re.search(rf'site_id\s*=\s*["\']{re.escape(site_id)}["\']', content) or f'SITE_ID = "{site_id}"' in content:
                        path.unlink()
                except OSError:
                    continue
        self.refresh()