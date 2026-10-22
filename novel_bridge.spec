# -*- mode: python ; coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────────────────
# PyInstaller spec for NovelBridge AR
#
# Build locally (one-time test):
#   pyinstaller novel_bridge.spec --noconfirm
#
# Output lands in:  dist/NovelBridgeAR/NovelBridgeAR.exe
# ─────────────────────────────────────────────────────────────────────────────

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT_ROOT = Path.cwd().resolve()
LNCRAWL_CHECKOUT = PROJECT_ROOT / "lightnovel-crawler-dev"
LNCRAWL_SOURCES = PROJECT_ROOT / "other sources" / "lightnovel-crawler-sources"

for required_path in (
    LNCRAWL_CHECKOUT / "lncrawl",
    LNCRAWL_CHECKOUT / "LICENSE",
    LNCRAWL_SOURCES / "en",
    LNCRAWL_SOURCES / "ar",
    LNCRAWL_SOURCES / "LICENSE",
):
    if not required_path.exists():
        raise FileNotFoundError(
            f"Required LightNovelCrawler packaging input is missing: {required_path}"
        )

sys.path.insert(0, str(LNCRAWL_CHECKOUT))


def collect_runtime_tree(source_root: Path, destination_root: str) -> list[tuple[str, str]]:
    return [
        (
            str(path),
            str(Path(destination_root) / path.relative_to(source_root)),
        )
        for path in source_root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    ]

# ── Collect data files that packages ship as non-Python assets ──────────────
datas = []

# GUI stylesheet & other resources
datas += [("gui/resources", "gui/resources")]
datas += [("NovelBridge_fixed.html", ".")]
datas += [(".env.example", ".")]
datas += [("VERSION", ".")]

# App icon — bundled so the frozen exe can load it at runtime
datas += [("icon.ico", ".")]

# Public site / glossary config files — OAuth client secrets stay external.
datas += [("config/sites.json", "config")]
datas += [("config/glossary.json", "config")]

# Keep the crawler checkout and source catalog alongside the executable so the
# runtime bridge can discover them using its normal folder-layout detection.
datas += collect_runtime_tree(
    LNCRAWL_CHECKOUT / "lncrawl",
    "lightnovel-crawler-dev/lncrawl",
)
datas += [
    (
        str(LNCRAWL_CHECKOUT / "LICENSE"),
        "lightnovel-crawler-dev",
    ),
    (
        str(LNCRAWL_CHECKOUT / "README.md"),
        "lightnovel-crawler-dev",
    ),
]
datas += collect_runtime_tree(
    LNCRAWL_SOURCES,
    "other sources/lightnovel-crawler-sources",
)
datas += [
    (
        str(LNCRAWL_SOURCES / "LICENSE"),
        "licenses/lightnovel-crawler-sources",
    ),
]

# Playwright needs its Node driver and JavaScript files at runtime. Browser
# binaries are distributed separately and must not be folded into this bundle.
datas += collect_data_files(
    "playwright",
    excludes=["**/.local-browsers/**", "**/ms-playwright/**"],
)

# ── Hidden imports that PyInstaller's static analysis may miss ────────────
hidden_imports = [
    # SQLModel / SQLAlchemy
    "sqlmodel",
    "sqlalchemy.dialects.sqlite",
    # FastAPI + uvicorn
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "fastapi",
    # Qt platform plugin (Windows)
    "PyQt6.QtWidgets",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtWebChannel",
    "PyQt6.sip",
    # HTTP clients
    "httpx",
    "curl_cffi",
    "playwright",
    "playwright.async_api",
    # Parsers
    "selectolax.parser",
    "bs4",
    "lxml",
    "lxml.etree",
    # EPUB
    "ebooklib",
    # Google Drive export
    "google.auth.transport.requests",
    "google.oauth2.credentials",
    "google_auth_oauthlib.flow",
    "googleapiclient.discovery",
    "googleapiclient.http",
    # Utilities
    "dotenv",
    "aiofiles",
    # AI providers
    "google.genai",
    "groq",
    "openai",
    # Dependencies referenced by dynamically loaded external source modules.
    "Crypto.Cipher.AES",
    "box",
    "exejs",
    "pyease_grpc",
    "regex",
    "requests.utils",
]
hidden_imports += collect_submodules("lncrawl")
hidden_imports += collect_submodules("scraper")

block_cipher = None

a = Analysis(
    ["run_gui.py"],          # entry point
    pathex=[str(PROJECT_ROOT), str(LNCRAWL_CHECKOUT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "pytest_asyncio",
        "_pytest",
        "tkinter",
        "matplotlib",
        "numpy",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# The app only builds the Drive v3 API; keep its static discovery document.
# Qt's DevTools debug frontend is not used by the application and is large.
filtered_datas = []
for destination, source, typecode in a.datas:
    normalized_destination = destination.replace("\\", "/")
    is_unused_google_discovery_doc = (
        normalized_destination.startswith("googleapiclient/discovery_cache/documents/")
        and normalized_destination != "googleapiclient/discovery_cache/documents/drive.v3.json"
    )
    is_unused_webengine_devtools_debug = normalized_destination.endswith(
        "qtwebengine_devtools_resources.debug.pak"
    )
    if not is_unused_google_discovery_doc and not is_unused_webengine_devtools_debug:
        filtered_datas.append((destination, source, typecode))
a.datas = filtered_datas

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NovelBridgeAR",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # no console window — pure GUI app
    uac_admin=True,         # request administrator rights when the app starts
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="icon.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="NovelBridgeAR",   # output folder: dist/NovelBridgeAR/
)
