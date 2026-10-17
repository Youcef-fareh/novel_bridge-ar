"""Maintenance for persistent jobs and generated EPUB output."""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from backend.database import get_jobs_for_scrub
from backend.config import get_config

logger = logging.getLogger("novelbridge.scrubber")

_OUTPUT_DIR = get_config().paths.output
_OUTPUT_MAX_MB = get_config().output_max_mb
_MIN_ARTIFACT_AGE_SECONDS = 7 * 24 * 60 * 60


def scrub_output(
    output_dir: Path = _OUTPUT_DIR,
    max_megabytes: int = _OUTPUT_MAX_MB,
    min_age_seconds: int = _MIN_ARTIFACT_AGE_SECONDS,
) -> tuple[int, int]:
    """Remove oldest generated EPUBs over the configured cap, never fresh files."""
    if max_megabytes < 0:
        raise ValueError("OUTPUT_MAX_MB must not be negative")
    if not output_dir.exists():
        return 0, 0

    files = [
        path for path in output_dir.glob("*.epub")
        if path.is_file() and time.time() - path.stat().st_mtime >= min_age_seconds
    ]
    total_bytes = sum(path.stat().st_size for path in files)
    limit_bytes = max_megabytes * 1024 * 1024
    removed_count = 0
    removed_bytes = 0

    for path in sorted(files, key=lambda item: (item.stat().st_mtime, item.name)):
        if total_bytes <= limit_bytes:
            break
        size = path.stat().st_size
        try:
            path.unlink()
        except OSError:
            logger.warning("Could not remove old EPUB output %s", path, exc_info=True)
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
        "removed_epubs": removed_epubs,
        "removed_bytes": removed_bytes,
    }
    if any(result.values()):
        logger.info("Scheduler scrubber completed: %s", result)
    return result
