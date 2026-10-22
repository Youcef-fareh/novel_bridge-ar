"""Regression tests for Phase 2 bug fixes (crashes/hangs/unrecoverable jobs)."""
from __future__ import annotations

import asyncio
import importlib
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


def _fresh_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "phase2.db"))
    import backend.database as database

    database = importlib.reload(database)
    database.init_db()
    return database


# ── 2.2 Scrubber must not force-fail healthy or paused jobs ───────────────────

def test_scrubber_keeps_running_job_with_fresh_heartbeat(tmp_path, monkeypatch):
    database = _fresh_database(tmp_path, monkeypatch)
    from backend.models import JobStatus, JobType

    novel = database.create_novel("https://example.org/book", "example")
    job = database.enqueue_job(novel.id, JobType.scrape)
    database.update_job(job.id, status=JobStatus.running)
    # Backdate started_at 40 h, but heartbeat is fresh (set by update_job).
    with database._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE job SET started_at = ?, heartbeat_at = ? WHERE id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat(sep=" "),
                datetime.now(timezone.utc).isoformat(sep=" "),
                job.id,
            ),
        )

    failed, _deleted = database.get_jobs_for_scrub()
    assert failed == 0
    assert database.get_job(job.id).status == JobStatus.running


def test_scrubber_fails_dead_running_job(tmp_path, monkeypatch):
    database = _fresh_database(tmp_path, monkeypatch)
    from backend.models import JobStatus, JobType

    novel = database.create_novel("https://example.org/book", "example")
    job = database.enqueue_job(novel.id, JobType.scrape)
    database.update_job(job.id, status=JobStatus.running)
    with database._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE job SET started_at = ?, heartbeat_at = ? WHERE id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat(sep=" "),
                (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(sep=" "),
                job.id,
            ),
        )

    failed, _deleted = database.get_jobs_for_scrub()
    assert failed == 1
    assert database.get_job(job.id).status == JobStatus.failed


def test_scrubber_requeues_stale_paused_job_instead_of_failing(tmp_path, monkeypatch):
    database = _fresh_database(tmp_path, monkeypatch)
    from backend.models import JobStatus, JobType

    novel = database.create_novel("https://example.org/book", "example")
    job = database.enqueue_job(novel.id, JobType.scrape)
    database.update_job(job.id, status=JobStatus.paused)
    with database._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE job SET started_at = ?, heartbeat_at = ? WHERE id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat(sep=" "),
                (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(sep=" "),
                job.id,
            ),
        )

    database.get_jobs_for_scrub()
    assert database.get_job(job.id).status == JobStatus.queued


def test_scrubber_keeps_user_paused_job_with_fresh_heartbeat(tmp_path, monkeypatch):
    database = _fresh_database(tmp_path, monkeypatch)
    from backend.models import JobStatus, JobType

    novel = database.create_novel("https://example.org/book", "example")
    job = database.enqueue_job(novel.id, JobType.scrape)
    database.update_job(job.id, status=JobStatus.paused)
    with database._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE job SET started_at = ?, heartbeat_at = ? WHERE id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat(sep=" "),
                datetime.now(timezone.utc).isoformat(sep=" "),
                job.id,
            ),
        )

    database.get_jobs_for_scrub()
    assert database.get_job(job.id).status == JobStatus.paused


# ── 2.6 JobCancelledError instead of asyncio.CancelledError control flow ──────

def test_run_job_raises_job_cancelled_error(tmp_path, monkeypatch):
    database = _fresh_database(tmp_path, monkeypatch)
    scheduler_module = importlib.reload(importlib.import_module("backend.scheduler"))
    from backend.models import JobStatus, JobType

    sched = scheduler_module.JobScheduler()
    monkeypatch.setattr(sched, "poll_interval", 0.01)
    novel = database.create_novel("https://example.org/book", "example")
    job = database.enqueue_job(novel.id, JobType.scrape)
    database.update_job(job.id, status=JobStatus.cancelled)

    async def run():
        return await sched.wait_for_job(job.id)

    try:
        with pytest.raises(scheduler_module.JobCancelledError):
            asyncio.run(run())
    finally:
        sched.stop(timeout=5)


def test_job_cancelled_error_is_not_a_task_cancellation():
    """JobCancelledError must be a plain Exception so HTTP/GUI code can catch it."""
    from backend.scheduler import JobCancelledError

    assert issubclass(JobCancelledError, RuntimeError)
    assert not issubclass(JobCancelledError, asyncio.CancelledError)


# ── 2.6 Shutdown requeues instead of cancelling ───────────────────────────────

def test_requeue_job_returns_inflight_job_to_queue(tmp_path, monkeypatch):
    database = _fresh_database(tmp_path, monkeypatch)
    from backend.models import JobStatus, JobType

    novel = database.create_novel("https://example.org/book", "example")
    job = database.enqueue_job(novel.id, JobType.scrape)
    database.update_job(job.id, status=JobStatus.running, progress=42, done_items=7)

    requeued = database.requeue_job(job.id)
    assert requeued.status == JobStatus.queued
    assert requeued.worker_id is None
    assert requeued.progress == 42
    # Terminal jobs are untouched.
    database.update_job(job.id, status=JobStatus.completed)
    assert database.requeue_job(job.id).status == JobStatus.completed


# ── 2.7 One malformed release tag must not kill the update check ──────────────

def test_select_update_skips_malformed_tags():
    from backend.update_manager import select_update

    releases = [
        {"tag_name": "app-2.0", "draft": False, "prerelease": False, "assets": []},
        {
            "tag_name": "v1.2.0",
            "draft": False,
            "prerelease": False,
            "html_url": "",
            "body": "",
            "assets": [
                {
                    "name": "NovelBridgeAR_Setup.exe",
                    "browser_download_url": "https://example.org/setup.exe",
                    "size": 100,
                },
                {
                    "name": "NovelBridgeAR_Setup.exe.sha256",
                    "browser_download_url": "https://example.org/setup.exe.sha256",
                },
            ],
        },
    ]
    plan = select_update(releases, "v1.0.0")
    assert plan is not None
    assert plan.version == "1.2.0"


def test_select_update_all_malformed_returns_none():
    from backend.update_manager import select_update

    releases = [{"tag_name": "side-tag", "draft": False, "prerelease": False, "assets": []}]
    assert select_update(releases, "v1.0.0") is None


# ── 2.8 Clearing an API key must remove it from os.environ ────────────────────

def test_apply_env_clears_empty_keys(monkeypatch):
    from gui.web_bridge import _apply_env

    monkeypatch.setenv("TESTPROV_API_KEY", "stale-secret")
    _apply_env({"TESTPROV_API_KEY": ""})
    assert os.getenv("TESTPROV_API_KEY") is None

    _apply_env({"TESTPROV_API_KEY": "new-secret"})
    assert os.getenv("TESTPROV_API_KEY") == "new-secret"


# ── 2.4 Pacer reservations serialize concurrent waiters ───────────────────────

def test_pacer_serializes_concurrent_waiters(monkeypatch):
    from backend.adapters import scraper_state

    memory = scraper_state._Memory(Path("nonexistent-dir/memory.json"))
    pacer = scraper_state.Pacer(memory)
    monkeypatch.setattr(pacer, "_get_profile", lambda origin: scraper_state.OriginProfile(origin, mean_interval=0.15))

    fire_times = []

    async def one_wait():
        await pacer.wait("https://origin.example")
        fire_times.append(time.monotonic())

    async def main():
        await asyncio.gather(one_wait(), one_wait(), one_wait())

    asyncio.run(main())
    fire_times.sort()
    gaps = [b - a for a, b in zip(fire_times, fire_times[1:])]
    # Without atomic reservation all three fire together (gap ~0).
    assert all(gap > 0.05 for gap in gaps), gaps


# ── 2.1/2.3 Wiring checks (source-level, Qt widgets are not unit-testable) ────

def test_update_check_handlers_are_bound_methods():
    """Lambdas connected to worker signals run on the worker thread; bound
    methods are auto-queued to the GUI thread. The update-check paths must use
    bound methods so QMessageBox is never built off the GUI thread."""
    import ast
    import io
    import tokenize

    source = Path("gui/app.py").read_text(encoding="utf-8")
    # Strip comments and docstrings so wording in prose doesn't affect the check.
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    code_only = "".join(
        tok.string
        for tok in tokens
        if tok.type not in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_check_for_updates":
            assert not any(
                isinstance(child, ast.Lambda) for child in ast.walk(node)
            ), "update-check lambdas re-introduce the cross-thread bug"
            break
    else:
        raise AssertionError("_check_for_updates not found")
    assert "connect(self._on_update_check_completed)" in code_only
    assert "connect(self._on_update_check_failed)" in code_only


def test_test_provider_does_not_block_on_network():
    """test_provider must return immediately and deliver results via signal."""
    import ast

    source = Path("gui/web_bridge.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "test_provider":
            text = ast.get_source_segment(source, node) or ""
            assert "_AsyncWorker" in text
            assert "provider_test_finished" in text
            break
    else:
        raise AssertionError("test_provider not found")


def test_scheduler_start_does_not_run_scrubber_inline():
    """start() is called from async endpoints; heavy work must not run inline."""
    import ast

    source = Path("backend/scheduler.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "start":
            text = ast.get_source_segment(source, node) or ""
            assert "run_scrubber" not in text
            assert "recover_interrupted_jobs()" not in text
            break
    else:
        raise AssertionError("scheduler.start not found")
