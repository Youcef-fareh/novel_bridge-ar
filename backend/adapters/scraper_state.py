"""
NovelBridge — per-origin scrape pacer + learned memory.

Inspired by lncrawl/services/scraper.py SharedState / Pacer.

Provides three fetch modes (same names as lncrawl):
  open()   — paced + remembered (use for novel/chapter scraping)
  probe()  — single-shot, unpaced, no memory (use for search, cover checks)
  plain()  — direct, no pacer at all (use for translator APIs, epub cover)

Memory is persisted to ``data/scraper_memory.json`` (LRU-500, atomic write)
so learned intervals survive restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger("novelbridge.scraper_state")

# ── Constants ──────────────────────────────────────────────────────────────────

_ALPHA = 0.2           # EMA smoothing factor for interval learning
_BACKOFF_MULT = 2.0    # multiply interval on 429/503
_MAX_INTERVAL = 30.0   # cap learned interval at 30 s
_MIN_INTERVAL = 0.05   # floor: never sleep less than 50 ms
_MEMORY_LIMIT = 500    # LRU cap on persisted origins

_DEFAULT_INTERVAL = float(os.getenv("DEFAULT_SCRAPE_INTERVAL", "0.5"))

_MEMORY_PATH = Path(os.getenv("SCRAPER_MEMORY_PATH", "data/scraper_memory.json"))

# ── User-Agent (single owner — adapters must not set UA themselves) ────────────

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


# ── OriginProfile ──────────────────────────────────────────────────────────────

class OriginProfile:
    """Pacing + health data for one origin (netloc)."""

    __slots__ = (
        "origin", "mean_interval", "last_ok", "ok_count",
        "fail_count", "last_layer", "clearance_at",
    )

    def __init__(
        self,
        origin: str,
        mean_interval: float = _DEFAULT_INTERVAL,
        last_ok: float = 0.0,
        ok_count: int = 0,
        fail_count: int = 0,
        last_layer: str = "httpx",
        clearance_at: float = 0.0,
    ) -> None:
        self.origin = origin
        self.mean_interval = float(mean_interval)
        self.last_ok = float(last_ok)
        self.ok_count = int(ok_count)
        self.fail_count = int(fail_count)
        self.last_layer = str(last_layer)
        self.clearance_at = float(clearance_at)

    def to_dict(self) -> Dict:
        return {
            "mean_interval": self.mean_interval,
            "last_ok": self.last_ok,
            "ok_count": self.ok_count,
            "fail_count": self.fail_count,
            "last_layer": self.last_layer,
            "clearance_at": self.clearance_at,
        }

    @classmethod
    def from_dict(cls, origin: str, data: Dict) -> "OriginProfile":
        return cls(
            origin=origin,
            mean_interval=data.get("mean_interval", _DEFAULT_INTERVAL),
            last_ok=data.get("last_ok", 0.0),
            ok_count=data.get("ok_count", 0),
            fail_count=data.get("fail_count", 0),
            last_layer=data.get("last_layer", "httpx"),
            clearance_at=data.get("clearance_at", 0.0),
        )


# ── Memory (persistent JSON store) ────────────────────────────────────────────

class _Memory:
    """LRU-capped JSON store for OriginProfile data."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: Dict[str, Dict] = {}
        self._order: list[str] = []   # LRU order (most-recent at end)
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                self._data = raw.get("profiles", {})
                self._order = list(self._data.keys())
            except Exception as exc:
                logger.warning("Could not load scraper memory: %s", exc)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"profiles": self._data}, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as exc:
            logger.warning("Could not save scraper memory: %s", exc)

    def get(self, origin: str) -> Optional[Dict]:
        with self._lock:
            return self._data.get(origin)

    def set(self, origin: str, profile_dict: Dict) -> None:
        with self._lock:
            if origin in self._data:
                self._order.remove(origin)
            self._data[origin] = profile_dict
            self._order.append(origin)
            # LRU eviction
            while len(self._order) > _MEMORY_LIMIT:
                evicted = self._order.pop(0)
                self._data.pop(evicted, None)
            self._save()


# ── Pacer ──────────────────────────────────────────────────────────────────────

class Pacer:
    """Async pacing gate: learns intervals from measurement."""

    def __init__(self, memory: _Memory) -> None:
        self._memory = memory
        self._lock = asyncio.Lock() if False else threading.Lock()  # sync lock for _last_call
        self._last_call: Dict[str, float] = {}   # origin -> monotonic clock
        self._async_locks: Dict[str, asyncio.Lock] = {}

    def _get_profile(self, origin: str) -> OriginProfile:
        data = self._memory.get(origin)
        if data:
            return OriginProfile.from_dict(origin, data)
        return OriginProfile(origin)

    def _save_profile(self, profile: OriginProfile) -> None:
        self._memory.set(profile.origin, profile.to_dict())

    def seed(self, origin: str, declared_interval: float) -> None:
        """Seed an origin with its declared delay (from sites.json).
        Only sets if no learned interval exists yet — measurement always wins.
        """
        data = self._memory.get(origin)
        if not data:
            p = OriginProfile(origin, mean_interval=max(_MIN_INTERVAL, declared_interval))
            self._save_profile(p)

    async def wait(self, origin: str) -> None:
        """Wait the learned interval before fetching from *origin*."""
        profile = self._get_profile(origin)
        now = time.monotonic()
        last = self._last_call.get(origin, 0.0)
        elapsed = now - last
        sleep_for = max(0.0, profile.mean_interval - elapsed)
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
        self._last_call[origin] = time.monotonic()

    def learn(self, origin: str, success: bool, status_code: int = 200) -> None:
        """Update the learned interval after a fetch.

        On success: EMA toward observed gap (α=0.2).
        On 429/503: back off ×2, cap at MAX_INTERVAL.
        On other errors: no change (might be transient).
        """
        profile = self._get_profile(origin)
        if success:
            last = self._last_call.get(origin, 0.0)
            observed_gap = time.monotonic() - last
            profile.mean_interval = (
                (1 - _ALPHA) * profile.mean_interval + _ALPHA * observed_gap
            )
            profile.mean_interval = max(_MIN_INTERVAL, min(profile.mean_interval, _MAX_INTERVAL))
            profile.ok_count += 1
            profile.last_ok = time.monotonic()
        elif status_code in (429, 503):
            profile.mean_interval = min(profile.mean_interval * _BACKOFF_MULT, _MAX_INTERVAL)
            profile.fail_count += 1
            logger.info(
                "Pacer backed off %s → %.1fs (status %d)",
                origin, profile.mean_interval, status_code,
            )
        else:
            profile.fail_count += 1
        self._save_profile(profile)

    def get_profile(self, origin: str) -> OriginProfile:
        """Read-only profile access (for explain())."""
        return self._get_profile(origin)


# ── ScraperState: open / probe / plain ────────────────────────────────────────

class ScraperState:
    """Process-wide scraper state manager.

    open()   — paced + remembered (novel/chapter scraping)
    probe()  — single-shot unpaced (search, cover checks)
    plain()  — direct, no pacer (translator APIs, epub cover downloads)
    """

    def __init__(self) -> None:
        self._memory = _Memory(_MEMORY_PATH)
        self.pacer = Pacer(self._memory)

    def open(self, origin: str, declared_interval: float = _DEFAULT_INTERVAL) -> "OpenContext":
        """Return an open-mode context for *origin* (paced + remembered)."""
        self.pacer.seed(origin, declared_interval)
        return OpenContext(self.pacer, origin)

    def probe(self, origin: str) -> "ProbeContext":
        """Return a probe-mode context for *origin* (single-shot, no rotation)."""
        return ProbeContext(origin)

    def plain(self) -> "PlainContext":
        """Return a plain context — no pacer, no memory (for translator APIs)."""
        return PlainContext()

    def explain(self, origin: str) -> str:
        """Return a human-readable diagnosis string for *origin*."""
        profile = self.pacer.get_profile(origin)
        lines = [
            f"origin        : {origin}",
            f"learned_delay : {profile.mean_interval:.2f}s",
            f"ok_count      : {profile.ok_count}",
            f"fail_count    : {profile.fail_count}",
            f"last_layer    : {profile.last_layer}",
        ]
        return "\n".join(lines)


class OpenContext:
    """Paced context: await wait() before fetch, call learn() after."""

    def __init__(self, pacer: Pacer, origin: str) -> None:
        self._pacer = pacer
        self.origin = origin

    async def wait(self) -> None:
        await self._pacer.wait(self.origin)

    def learn(self, success: bool, status_code: int = 200) -> None:
        self._pacer.learn(self.origin, success, status_code)


class ProbeContext:
    """Unpaced context: no wait, no learn — for single-shot reachability."""

    def __init__(self, origin: str) -> None:
        self.origin = origin

    async def wait(self) -> None:
        pass  # intentionally no-op

    def learn(self, success: bool, status_code: int = 200) -> None:
        pass  # intentionally no-op


class PlainContext:
    """Direct context: for translator APIs and non-crawl traffic."""

    async def wait(self) -> None:
        pass

    def learn(self, success: bool, status_code: int = 200) -> None:
        pass


# ── Process-wide singleton ─────────────────────────────────────────────────────
scraper_state = ScraperState()
pacer = scraper_state.pacer


def get_scraper_state() -> ScraperState:
    """Return the global ScraperState instance."""
    return scraper_state


def get_pacer() -> Pacer:
    """Return the global Pacer instance."""
    return pacer
