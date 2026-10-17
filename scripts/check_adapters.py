"""
NovelBridge — Offline adapter surface and health validator.

Ported from lncrawl/commands/dev/sources.py check_sources() (MIT).
Validates all adapters offline without network requests:
1. Every adapter file imports cleanly.
2. Every adapter subclasses SiteAdapter and implements all 3 required methods.
3. Every adapter has a non-empty site_id and valid can_handle() response.
4. If SoupAdapter subclass, verifies selector lists are configured.
5. Index consistency: backend/adapters/_index.json matches actual adapters.

Usage:
    python scripts/check_adapters.py
"""
from __future__ import annotations

import importlib
import inspect
import json
import sys
from pathlib import Path
from typing import List, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.adapters.base import SiteAdapter
from backend.adapters.soup_template import SoupAdapter

ADAPTERS_DIR = _ROOT / "backend" / "adapters"
INDEX_PATH = ADAPTERS_DIR / "_index.json"

REQUIRED_SURFACE = (
    "site_id",
    "can_handle",
    "get_novel_metadata",
    "get_chapter_list",
    "get_chapter_text",
)


def validate_adapters() -> Tuple[int, List[str]]:
    errors: List[str] = []
    found_site_ids: set[str] = set()
    total_checked = 0

    search_dirs = [ADAPTERS_DIR, ADAPTERS_DIR / "custom"]
    for sdir in search_dirs:
        if not sdir.exists():
            continue
        for py_file in sorted(sdir.glob("*.py")):
            if py_file.name.startswith(("_", ".")) or py_file.name in (
                "base.py",
                "fetcher.py",
                "health.py",
                "lncrawl_bridge.py",
                "nodriver_fetch.py",
                "scraper_state.py",
                "soup_template.py",
                "tiers.py",
            ):
                continue

            rel_module = py_file.relative_to(_ROOT).with_suffix("").as_posix().replace("/", ".")

            # 1. Import check
            try:
                mod = importlib.import_module(rel_module)
            except Exception as e:
                errors.append(f"* {py_file.name}: failed to import ({e})")
                continue

            # 2. Inspect classes
            for name, cls in inspect.getmembers(mod, inspect.isclass):
                if cls.__module__ != mod.__name__:
                    continue
                if issubclass(cls, SiteAdapter) and cls not in (SiteAdapter, SoupAdapter) and not cls.__name__.endswith("Template"):
                    total_checked += 1
                    try:
                        instance = cls()
                    except Exception as e:
                        errors.append(f"* {name} ({py_file.name}): failed instantiation ({e})")
                        continue

                    # 3. Surface check
                    for attr in REQUIRED_SURFACE:
                        val = getattr(instance, attr, None)
                        if val is None:
                            errors.append(f"* {name} ({py_file.name}): missing required attribute/method '{attr}'")

                    site_id = getattr(instance, "site_id", "")
                    if not site_id:
                        errors.append(f"* {name} ({py_file.name}): empty site_id")
                    elif site_id in found_site_ids:
                        errors.append(f"* {name} ({py_file.name}): duplicate site_id '{site_id}'")
                    else:
                        found_site_ids.add(site_id)

                    # 4. can_handle check
                    try:
                        dummy_res = instance.can_handle("https://definitely-not-this-novel-site-12345.com/novel")
                        if dummy_res:
                            errors.append(f"* {name} ({py_file.name}): can_handle() returned True for random dummy domain")
                    except Exception as e:
                        errors.append(f"* {name} ({py_file.name}): can_handle() crashed on dummy URL: {e}")

                    # 5. SoupAdapter selector check
                    if issubclass(cls, SoupAdapter):
                        if not cls.novel_title_selectors:
                            errors.append(f"* {name} ({py_file.name}): novel_title_selectors is empty")
                        if not cls.chapter_list_selectors:
                            errors.append(f"* {name} ({py_file.name}): chapter_list_selectors is empty")
                        if not cls.chapter_body_selectors:
                            errors.append(f"* {name} ({py_file.name}): chapter_body_selectors is empty")

    # 6. Index sync check
    if INDEX_PATH.exists():
        try:
            index_data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            indexed_ids = {a["site_id"] for a in index_data.get("adapters", [])}
            missing_in_index = found_site_ids - indexed_ids
            if missing_in_index:
                errors.append(f"* _index.json: missing adapters {missing_in_index}; run scripts/gen_adapter_index.py")
        except Exception as e:
            errors.append(f"* _index.json: failed to read/parse index ({e})")
    else:
        errors.append("* _index.json: index file missing; run scripts/gen_adapter_index.py")

    return total_checked, errors


def main() -> None:
    print("NovelBridge Adapter Validator (offline surface check)...")
    total, errors = validate_adapters()
    print(f"Checked {total} adapters.")
    if errors:
        print(f"\n[FAIL] {len(errors)} error(s) found:")
        for err in errors:
            print(f"  {err}")
        sys.exit(1)
    else:
        print(f"[OK] All {total} adapters passed surface and integrity checks!")
        sys.exit(0)


if __name__ == "__main__":
    main()
