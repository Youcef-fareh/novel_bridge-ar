"""Persistent adapter and source-search preferences."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from backend.config import get_config

_lock = threading.RLock()
_DEFAULTS: dict[str, Any] = {
    "enabled_adapters": {},
    "search_limit": 20,
    "source_language": "all",
    "category": "all",
}


def _path() -> Path:
    return get_config().paths.data_dir / "source_preferences.json"


def get_preferences() -> dict[str, Any]:
    path = _path()
    with _lock:
        if not path.exists():
            return {**_DEFAULTS, "enabled_adapters": {}}
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read source preferences from {path}") from exc
    if not isinstance(stored, dict):
        raise ValueError(f"{path} must contain a JSON object")
    prefs = {**_DEFAULTS, **stored}
    if not isinstance(prefs["enabled_adapters"], dict):
        raise ValueError("enabled_adapters must be a JSON object")
    if isinstance(prefs["search_limit"], bool) or not isinstance(prefs["search_limit"], int):
        raise ValueError("search_limit must be an integer")
    if not 1 <= prefs["search_limit"] <= 50:
        raise ValueError("search_limit must be between 1 and 50")
    if prefs["source_language"] not in {"all", "en-ar", "ar-ar"}:
        raise ValueError("source_language must be all, en-ar, or ar-ar")
    if prefs["category"] not in {"all", "webnovel", "arabic"}:
        raise ValueError("category must be all, webnovel, or arabic")
    return prefs


def save_preferences(**updates: Any) -> dict[str, Any]:
    unknown = set(updates) - set(_DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown source preference(s): {', '.join(sorted(unknown))}")
    with _lock:
        prefs = get_preferences()
        prefs.update(updates)
        if not isinstance(prefs["enabled_adapters"], dict):
            raise ValueError("enabled_adapters must be a JSON object")
        if isinstance(prefs["search_limit"], bool) or not isinstance(prefs["search_limit"], int):
            raise ValueError("search_limit must be an integer")
        if not 1 <= prefs["search_limit"] <= 50:
            raise ValueError("search_limit must be between 1 and 50")
        if prefs["source_language"] not in {"all", "en-ar", "ar-ar"}:
            raise ValueError("source_language must be all, en-ar, or ar-ar")
        if prefs["category"] not in {"all", "webnovel", "arabic"}:
            raise ValueError("category must be all, webnovel, or arabic")

        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(prefs, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
        except OSError:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)
            raise
        return prefs


def adapter_is_enabled(site_id: str) -> bool:
    return get_preferences()["enabled_adapters"].get(site_id, True) is not False


def set_adapter_enabled(site_id: str, enabled: bool) -> None:
    if not site_id.strip():
        raise ValueError("site_id must not be empty")
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    adapters = get_preferences()["enabled_adapters"].copy()
    adapters[site_id] = enabled
    save_preferences(enabled_adapters=adapters)
