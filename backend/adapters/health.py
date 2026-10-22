"""
NovelBridge — per-origin scrape health tally.

Ported from lncrawl/services/health.py (MIT).
Keyed by reason string so a new signal is just a new string, not a schema change.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Dict, List, Tuple


class SourceHealth:
    """Per-origin tally of what a scrape needed beyond a plain HTTP fetch.

    Reasons are open strings — ``empty_body``, ``blocked_403``,
    ``rate_429``, ``timeout``, ``challenge``, ``empty_cover`` — so adding a
    new signal never requires a schema change.

    Thread-safe; all mutations are under a single lock.
    """

    MAX_SAMPLES = 5

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # {domain: {reason: count}}
        self._counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # {(domain, reason): [detail, ...]}
        self._samples: Dict[Tuple[str, str], List[str]] = defaultdict(list)

    def record(self, domain: str, reason: str, detail: str = "") -> None:
        """Record one occurrence of *reason* for *domain*."""
        if not domain:
            return
        with self._lock:
            self._counts[domain][reason] += 1
            if not detail:
                return
            bucket = self._samples[(domain, reason)]
            if detail not in bucket and len(bucket) < self.MAX_SAMPLES:
                bucket.append(detail)

    def count(self, domain: str, reason: str) -> int:
        """Return how many times *reason* has been recorded for *domain*."""
        with self._lock:
            return self._counts.get(domain, {}).get(reason, 0)

    def reasons(self, domain: str) -> Dict[str, int]:
        """Return ``{reason: count}`` for *domain*."""
        with self._lock:
            return dict(self._counts.get(domain, {}))

    def tally(self) -> Dict[str, Dict[str, int]]:
        """Return the full tally ``{domain: {reason: count}}``."""
        with self._lock:
            return {domain: dict(reasons) for domain, reasons in self._counts.items()}

    def samples(self, domain: str, reason: str) -> List[str]:
        """Return stored detail samples for *(domain, reason)* (up to MAX_SAMPLES)."""
        with self._lock:
            return list(self._samples.get((domain, reason), ()))

    def reset(self) -> None:
        """Clear all tallies (useful in tests)."""
        with self._lock:
            self._counts.clear()
            self._samples.clear()

    def summary_line(self, domain: str) -> str:
        """Return a one-line human-readable summary for *domain*."""
        r = self.reasons(domain)
        if not r:
            return f"{domain}: no issues recorded"
        parts = [f"{reason}:{cnt}" for reason, cnt in sorted(r.items())]
        return f"{domain}: " + ", ".join(parts)


# Process-wide singleton — adapters and pipeline both import this.
health = SourceHealth()


def get_source_health() -> SourceHealth:
    """Return the process-wide SourceHealth singleton."""
    return health
