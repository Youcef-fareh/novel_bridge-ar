# -*- mode: python ; coding: utf-8 -*-
# ─────────────────────────────────────────────────────────────────────────────
# PyInstaller spec for NovelBridge AR
#
# Build locally (one-time test):
#   pyinstaller novel_bridge.spec --noconfirm
#
# Output lands in:  dist/NovelBridgeAR/NovelBridgeAR.exe
# ─────────────────────────────────────────────────────────────────────────────

from PyInstaller.utils.hooks import collect_all, collect_data_files
import sys, os

# ── Collect data files that packages ship as non-Python assets ──────────────
datas = []

# GUI stylesheet & other resources
datas += [("gui/resources", "gui/resources")]

# App icon — bundled so the frozen exe can load it at runtime
datas += [("icon.ico", ".")]

# Site / glossary config files — needed at runtime
datas += [("config", "config")]

# Collect all data files from known heavy packages
for pkg in ("PyQt6", "sqlmodel", "uvicorn", "fastapi", "selectolax"):
    try:
        d, b, h = collect_all(pkg)
        datas     += d
    except Exception:
        pass

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
    "PyQt6.sip",
    # HTTP clients
    "httpx",
    "curl_cffi",
    # Parsers
    "selectolax.parser",
    "bs4",
    "lxml",
    "lxml.etree",
    # EPUB
    "ebooklib",
    # Utilities
    "dotenv",
    "aiofiles",
    "PIL",
    "tenacity",
    # AI providers
    "google.genai",
    "groq",
]

block_cipher = None

a = Analysis(
    ["run_gui.py"],          # entry point
    pathex=["."],
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
