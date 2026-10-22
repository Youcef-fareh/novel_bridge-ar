"""Application logging configuration and privacy-conscious diagnostics export."""
from __future__ import annotations

import io
import json
import logging
import os
import platform
import re
import sys
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from backend.config import base_dir, get_config

_LOG_FILENAME = "novelbridge.log"
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUP_COUNT = 5
_PROVIDER_KEY_NAMES = (
    "TOKENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "ORCAROUTER_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
    "GROK_API_KEY",
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(api[_ -]?key|authorization|bearer)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)


def logs_dir() -> Path:
    """Return the application log directory using the configured data path."""
    return get_config().paths.data_dir / "logs"


def setup_logging() -> None:
    """Install rotating UTF-8 file logging and an INFO-level console mirror."""
    config = get_config()
    destination = config.paths.data_dir / "logs"
    destination.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.diagnostics.log_level))
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    expected_path = destination / _LOG_FILENAME
    file_handler = next(
        (
            handler
            for handler in root.handlers
            if isinstance(handler, RotatingFileHandler)
            and getattr(handler, "_novelbridge_handler", False)
        ),
        None,
    )
    if file_handler and Path(file_handler.baseFilename) != expected_path:
        root.removeHandler(file_handler)
        file_handler.close()
        file_handler = None
    if file_handler is None:
        file_handler = RotatingFileHandler(
            expected_path,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        file_handler._novelbridge_handler = True
        root.addHandler(file_handler)

    if not any(
        getattr(handler, "_novelbridge_console", False) for handler in root.handlers
    ):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.INFO)
        console_handler._novelbridge_console = True
        root.addHandler(console_handler)
    for handler in root.handlers:
        if getattr(handler, "_novelbridge_handler", False) or getattr(
            handler, "_novelbridge_console", False
        ):
            handler.setFormatter(formatter)
    file_handler.maxBytes = _LOG_MAX_BYTES
    file_handler.backupCount = _LOG_BACKUP_COUNT
    file_handler.setLevel(logging.DEBUG)


def _safe_error_lines() -> list[str]:
    path = logs_dir() / _LOG_FILENAME
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    error_lines = [line for line in lines if " ERROR " in line or " CRITICAL " in line]
    return [
        _redact_sensitive_text(line)
        for line in error_lines[-30:]
    ]


def _redact_sensitive_text(text: str) -> str:
    text = _SENSITIVE_VALUE_RE.sub(r"\1=***", text)
    keys = sorted(
        (os.environ.get(name, "") for name in _PROVIDER_KEY_NAMES),
        key=len,
        reverse=True,
    )
    for key in keys:
        if key:
            text = text.replace(key, "***")
    return text


def diagnostics_summary() -> str:
    """Build a clipboard-safe report without including credential values."""
    configured_secrets = get_config().to_public_dict()["secrets"]
    configured = [
        name.removesuffix("_API_KEY")
        for name in _PROVIDER_KEY_NAMES
        if configured_secrets.get(name, {}).get("configured")
    ]
    version_file = base_dir() / "VERSION"
    version = version_file.read_text(encoding="utf-8").strip()
    errors = _safe_error_lines()
    return "\n".join(
        [
            f"NovelBridge version: {version}",
            f"Platform: {platform.platform()}",
            f"Python: {sys.version.split()[0]}",
            f"Configured providers: {', '.join(configured) if configured else 'none'}",
            "Recent errors:",
            *(errors or ["(none)"]),
        ]
    )


def diagnostics_archive() -> bytes:
    """Create a ZIP containing rotated logs, VERSION, and minimal system facts."""
    directory = logs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    version_file = base_dir() / "VERSION"
    system_info = {
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "app_version": version_file.read_text(encoding="utf-8").strip(),
    }
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(directory.glob(f"{_LOG_FILENAME}*")):
            if path.is_file():
                content = path.read_text(encoding="utf-8", errors="replace")
                bundle.writestr(
                    f"logs/{path.name}",
                    _redact_sensitive_text(content),
                )
        bundle.write(version_file, arcname="VERSION")
        bundle.writestr(
            "system-info.json",
            json.dumps(system_info, ensure_ascii=False, indent=2) + "\n",
        )
    return archive.getvalue()
