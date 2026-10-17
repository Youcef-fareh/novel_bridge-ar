"""
NovelBridge — Adapter index generator.

Scans all adapter implementations and generates:
1. backend/adapters/_index.json (machine-readable index of all adapters)
2. docs/SOURCES.md (human-readable table of supported sources)

Inspired by lncrawl/scripts/index_gen.py (MIT).
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import sys
from pathlib import Path

# Add project root to sys.path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from typing import Any, Dict, List

from backend.adapters.base import SiteAdapter
from backend.adapters.soup_template import SoupAdapter


ADAPTERS_DIR = Path("backend/adapters")
INDEX_PATH = ADAPTERS_DIR / "_index.json"
SOURCES_MD = Path("docs/SOURCES.md")


def collect_adapters() -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    seen_site_ids: set[str] = set()

    # Search in backend/adapters/*.py and backend/adapters/custom/*.py
    search_dirs = [ADAPTERS_DIR, ADAPTERS_DIR / "custom"]
    for sdir in search_dirs:
        if not sdir.exists():
            continue
        for py_file in sorted(sdir.glob("*.py")):
            if py_file.name.startswith(("_", ".")) or py_file.name in ("base.py", "fetcher.py", "health.py", "lncrawl_bridge.py", "nodriver_fetch.py", "scraper_state.py", "soup_template.py", "tiers.py"):
                continue

            rel_module = str(py_file.with_suffix("")).replace("\\", ".").replace("/", ".")
            try:
                mod = importlib.import_module(rel_module)
            except Exception as e:
                print(f"[WARN] Failed to import {rel_module}: {e}")
                continue

            for name, cls in inspect.getmembers(mod, inspect.isclass):
                if cls.__module__ != mod.__name__:
                    continue
                if issubclass(cls, SiteAdapter) and cls is not SiteAdapter and cls is not SoupAdapter and not cls.__name__.endswith("Template"):
                    try:
                        instance = cls()
                    except Exception:
                        instance = None

                    site_id = getattr(cls, "site_id", "") or (getattr(instance, "site_id", "") if instance else "")
                    if not site_id or site_id in seen_site_ids:
                        continue
                    seen_site_ids.add(site_id)

                    base_url = getattr(cls, "base_url", "") or ""
                    template_name = "SoupAdapter" if issubclass(cls, SoupAdapter) else "Custom"
                    version = getattr(cls, "version", "1")
                    tier = getattr(cls, "tier", "legacy")

                    selectors = {}
                    if issubclass(cls, SoupAdapter):
                        selectors = {
                            "novel_title": getattr(cls, "novel_title_selectors", []),
                            "novel_author": getattr(cls, "novel_author_selectors", []),
                            "novel_cover": getattr(cls, "novel_cover_selectors", []),
                            "chapter_list": getattr(cls, "chapter_list_selectors", []),
                            "chapter_text": getattr(cls, "chapter_body_selectors", []),
                        }

                    sel_hash = hashlib.sha256(json.dumps(selectors, sort_keys=True).encode()).hexdigest()[:12] if selectors else "n/a"

                    entries.append({
                        "site_id": site_id,
                        "class_name": cls.__name__,
                        "module": rel_module,
                        "file": str(py_file.as_posix()),
                        "base_url": base_url,
                        "template": template_name,
                        "version": version,
                        "tier": tier,
                        "selectors_hash": sel_hash,
                        "source_language": getattr(cls, "source_language", "English"),
                        "scraping_method": getattr(cls, "scraping_method", "HTTP"),
                    })

    entries.sort(key=lambda x: x["site_id"])
    return entries


def write_index(entries: List[Dict[str, Any]]) -> None:
    data = {
        "version": "1.0",
        "total_sources": len(entries),
        "adapters": entries,
    }
    INDEX_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Generated {INDEX_PATH} with {len(entries)} adapters.")


def update_sources_md(entries: List[Dict[str, Any]]) -> None:
    lines = [
        "# Supported Sources",
        "",
        "The following sources are supported and monitored by NovelBridge.",
        "",
        "<!-- auto generated sources -->",
        "| Site ID | Class | Template | Tier | Method | Base URL |",
        "|---|---|---|---|---|---|",
    ]
    for e in entries:
        lines.append(f"| `{e['site_id']}` | `{e['class_name']}` | `{e['template']}` | `{e['tier']}` | {e['scraping_method']} | {e['base_url'] or '-'} |")
    lines.append("<!-- /auto generated sources -->")
    lines.append("")

    SOURCES_MD.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"Updated {SOURCES_MD} with table of {len(entries)} sources.")


def main() -> None:
    entries = collect_adapters()
    write_index(entries)
    update_sources_md(entries)


if __name__ == "__main__":
    main()
