"""
NovelBridge — content versioning: tier + version stamps for chapters.

Ported from lncrawl/core/tiers.py (MIT).

Two rules from that module that are easy to get wrong:
- **Precedence is by tier, never by version.** A version is a timestamp; a
  re-synced legacy crawler gets a fresh one and would outrank a spec that
  was meant to replace it.
- **Staleness needs positive evidence.** We only answer True when we have
  the same tier, both versions are known, *and* they differ. Anything else
  is "unknown" and unknown must not invalidate — a needless re-download of
  an entire library is far worse than serving one stale chapter.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

__all__ = [
    "LEGACY",
    "SPEC",
    "TIERS",
    "stamp",
    "stored_version",
    "stored_tier",
    "is_stale",
]

#: A Python crawler from the bundled corpus or the user's own directory.
LEGACY = "legacy"

#: A declarative spec (reserved for future use).
SPEC = "spec"

#: Highest-precedence tier first.
TIERS = (SPEC, LEGACY)

# Keys used inside the ``extra`` JSON column (or chapter attributes).
VERSION_KEY = "crawler_version"
TIER_KEY = "crawler_tier"


def stamp(extra: Optional[Mapping[str, Any]], version: Any, tier: str = LEGACY) -> Dict[str, Any]:
    """Record what produced this content alongside whatever else *extra* holds."""
    out: Dict[str, Any] = dict(extra or {})
    out[VERSION_KEY] = None if version is None else str(version)
    out[TIER_KEY] = tier
    return out


def stored_version(extra: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The version stamped on stored content as a string, or None when absent."""
    if not extra:
        return None
    value = extra.get(VERSION_KEY)
    if value is None or value == "":
        return None
    return str(value)


def stored_tier(extra: Optional[Mapping[str, Any]]) -> str:
    """The tier stamped on stored content (defaults to LEGACY for old content)."""
    if not extra:
        return LEGACY
    return str(extra.get(TIER_KEY) or LEGACY)


def is_stale(extra: Optional[Mapping[str, Any]], version: Any, tier: str = LEGACY) -> bool:
    """Whether stored content was produced by something other than this crawler.

    Deliberately conservative: returns True only with positive evidence —
    same tier, both versions known, and they differ.
    """
    if stored_tier(extra) != tier:
        # The host moved tiers. Content was verified to be equivalent when the
        # spec was adopted, so re-downloading would be pure cost.
        return False
    stored = stored_version(extra)
    if stored is None:
        return False
    if version is None:
        return False
    return stored != str(version)
