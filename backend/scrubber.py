"""Maintenance for persistent jobs and generated EPUB output."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from backend.database import get_jobs_for_scrub
from backend.config import get_config, output_path

logger = logging.getLogger("novelbridge.scrubber")

_MIN_ARTIFACT_AGE_SECONDS = 7 * 24 * 60 * 60
_ARTIFACT_EXTENSIONS = (".epub", ".zip", ".pdf", ".azw3", ".mobi", ".docx", ".txt")


def scrub_output(
    output_dir: Path | None = None,
    max_megabytes: int | None = None,
    min_age_seconds: int = _MIN_ARTIFACT_AGE_SECONDS,
) -> tuple[int, int]:
    """Remove old generated artifacts over the configured cap, never fresh files.

    Defaults read the live config on every call so runtime changes to
    config.json (paths.output, output_max_mb) are always honored.
    """
    config = get_config()
    if output_dir is None:
        output_dir = output_path()
    if max_megabytes is None:
        max_megabytes = config.output_max_mb
    if max_megabytes < 0:
        raise ValueError("OUTPUT_MAX_MB must not be negative")
    if not output_dir.exists():
        return 0, 0

    def _stat_safe(path: Path) -> tuple[float, int] | None:
        try:
            stat = path.stat()
        except OSError:
            # File vanished between glob and stat (or unreadable).
            return None
        return stat.st_mtime, stat.st_size

    candidates: list[tuple[float, int, Path]] = []
    total_bytes = 0
    cutoff = time.time() - min_age_seconds
    for path in output_dir.iterdir():
        if not path.is_file() or path.suffix.lower() not in _ARTIFACT_EXTENSIONS:
            continue
        stats = _stat_safe(path)
        if stats is None:
            continue
        mtime, size = stats
        total_bytes += size
        if mtime <= cutoff:
            candidates.append((mtime, size, path))
    limit_bytes = max_megabytes * 1024 * 1024
    removed_count = 0
    removed_bytes = 0

    for _, size, path in sorted(candidates, key=lambda item: (item[0], item[2].name)):
        if total_bytes <= limit_bytes:
            break
        try:
            path.unlink()
        except OSError:
            logger.warning("Could not remove old artifact %s", path, exc_info=True)
            continue
        total_bytes -= size
        removed_bytes += size
        removed_count += 1

    return removed_count, removed_bytes


def run_scrubber() -> dict[str, int]:
    """Apply job retention/runtime policies and the configured output disk cap."""
    failed_jobs, deleted_jobs = get_jobs_for_scrub()
    removed_epubs, removed_bytes = scrub_output()
    result = {
        "failed_jobs": failed_jobs,
        "deleted_jobs": deleted_jobs,
        "removed_artifacts": removed_epubs,
        "removed_bytes": removed_bytes,
    }
    if any(result.values()):
        logger.info("Scheduler scrubber completed: %s", result)
    return result
