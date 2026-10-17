"""Persistence, fairness, recovery, and output-retention tests for Track 3."""
from __future__ import annotations

import importlib
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _fresh_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "scheduler.db"))
    import backend.database as database

    database = importlib.reload(database)
    database.init_db()
    return database


def test_claim_respects_novel_fairness_domain_cap_and_priority(tmp_path, monkeypatch):
    database = _fresh_database(tmp_path, monkeypatch)
    from backend.models import JobStatus, JobType

    first = database.create_novel("https://one.example/novel", "one")
    same_domain = database.create_novel("https://one.example/another", "one")
    other_domain = database.create_novel("https://other.example/novel", "other")

    first_job = database.enqueue_job(first.id, JobType.scrape, priority=1)
    queued_same_novel = database.enqueue_job(first.id, JobType.translate, priority=10)
    queued_same_domain = database.enqueue_job(same_domain.id, JobType.scrape, priority=5)
    queued_other_domain = database.enqueue_job(other_domain.id, JobType.scrape, priority=0)

    claimed = database.claim_next_job("worker-1", max_per_domain=1)
    assert claimed.id == queued_same_novel.id
    assert claimed.status == JobStatus.running
    claimed_other = database.claim_next_job("worker-2", max_per_domain=1)
    assert claimed_other.id == queued_other_domain.id

    database.update_job(claimed_other.id, status=JobStatus.completed)
    assert database.claim_next_job("worker-3", max_per_domain=1) is None

    database.update_job(claimed.id, status=JobStatus.completed)
    claimed_same_domain = database.claim_next_job("worker-3", max_per_domain=1)
    assert claimed_same_domain.id == queued_same_domain.id

    database.update_job(claimed_same_domain.id, status=JobStatus.completed)
    claimed_first = database.claim_next_job("worker-4", max_per_domain=2)
    assert claimed_first.id == first_job.id
    assert database.claim_next_job("worker-5", max_per_domain=2) is None


def test_expired_worker_lease_is_requeued_and_progress_retained(tmp_path, monkeypatch):
    database = _fresh_database(tmp_path, monkeypatch)
    from backend.models import JobStatus, JobType

    novel = database.create_novel("https://example.org/book", "example")
    job = database.enqueue_job(novel.id, JobType.scrape)
    claimed = database.claim_next_job("interrupted-worker")
    database.update_job(job.id, status=JobStatus.running)
    assert database.recover_interrupted_jobs() == 0
    assert database.get_job(job.id).status == JobStatus.running
    database.update_job(job.id, progress=42, done_items=7, total_items=16)

    with database._engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE job SET lease_expires_at = ? WHERE id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(sep=" "),
                job.id,
            ),
        )

    assert database.recover_interrupted_jobs() == 1
    recovered = database.get_job(job.id)
    assert recovered.status == JobStatus.queued
    assert recovered.worker_id is None
    assert recovered.progress == 42
    assert recovered.done_items == 7
    assert database.claim_next_job("replacement-worker").id == job.id


def test_scheduler_schema_migrates_legacy_job_table(tmp_path, monkeypatch):
    database = _fresh_database(tmp_path, monkeypatch)
    from backend.models import JobStatus, JobType

    legacy_columns = {
        "id",
        "novel_id",
        "type",
        "status",
        "progress",
        "total_items",
        "done_items",
        "error_message",
        "created_at",
        "updated_at",
    }

    with database._engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE job")
        conn.exec_driver_sql(
            """
            CREATE TABLE job (
                id INTEGER PRIMARY KEY,
                novel_id INTEGER NOT NULL,
                type VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                total_items INTEGER NOT NULL DEFAULT 0,
                done_items INTEGER NOT NULL DEFAULT 0,
                error_message VARCHAR,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )

    database.init_db()
    with database._engine.connect() as conn:
        columns = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(job)").fetchall()
        }
        indexes = {
            row[1] for row in conn.exec_driver_sql("PRAGMA index_list(job)").fetchall()
        }
    assert legacy_columns <= columns
    assert {
        "parent_job_id",
        "domain",
        "priority",
        "failed_items",
        "options_json",
        "worker_id",
        "heartbeat_at",
        "started_at",
        "finished_at",
        "lease_expires_at",
    } <= columns
    assert "ix_job_scheduler" in indexes
    assert "ix_job_domain_status" in indexes

    novel = database.create_novel("https://example.org/book", "example")
    job = database.create_job(novel.id, JobType.scrape)
    assert job.parent_job_id is None
    assert job.status == JobStatus.queued


def test_pause_resume_and_cancel_persist_to_job(tmp_path, monkeypatch):
    database = _fresh_database(tmp_path, monkeypatch)
    from backend.models import JobStatus, JobType
    from backend.pipeline import JobControl

    novel = database.create_novel("https://example.org/book", "example")
    job = database.enqueue_job(novel.id, JobType.scrape)
    control = JobControl()
    control.bind_job(job.id)
    control.pause()
    assert database.get_job(job.id).status == JobStatus.paused

    control.resume()
    assert database.get_job(job.id).status == JobStatus.queued
    control.cancel()
    assert database.get_job(job.id).status == JobStatus.cancelled
    assert database.get_job(job.id).finished_at is not None


def test_scrubber_removes_only_old_epubs_to_respect_cap(tmp_path):
    from backend.scrubber import scrub_output

    output = tmp_path / "output"
    output.mkdir()
    old_epub = output / "old.epub"
    recent_epub = output / "recent.epub"
    old_other = output / "old.txt"
    old_epub.write_bytes(b"x" * 12)
    recent_epub.write_bytes(b"recent")
    old_other.write_bytes(b"keep")
    old_time = time.time() - 10 * 24 * 60 * 60
    os.utime(old_epub, (old_time, old_time))
    os.utime(old_other, (old_time, old_time))

    removed_count, removed_bytes = scrub_output(
        output,
        max_megabytes=0,
        min_age_seconds=7 * 24 * 60 * 60,
    )

    assert (removed_count, removed_bytes) == (1, 12)
    assert not old_epub.exists()
    assert recent_epub.exists()
    assert old_other.exists()


def test_scheduler_worker_executes_persisted_job(tmp_path, monkeypatch):
    database = _fresh_database(tmp_path, monkeypatch)
    from backend.models import JobStatus, JobType

    scheduler_module = importlib.reload(importlib.import_module("backend.scheduler"))
    scheduler = scheduler_module.JobScheduler()
    monkeypatch.setattr(scheduler, "worker_count", 1)
    monkeypatch.setattr(scheduler, "poll_interval", 0.01)
    novel = database.create_novel("https://example.org/book", "example")

    async def fake_scrape(
        novel_id, progress_cb=None, job_control=None, job_id=None
    ):
        await job_control.check()
        progress_cb(1, 1, "Scraped")
        database.update_job(job_id, status=JobStatus.completed, progress=100)
        return database.get_job(job_id)

    import backend.pipeline as pipeline
    monkeypatch.setattr(pipeline, "run_scrape_job", fake_scrape)

    async def run():
        return await scheduler.run_job(novel.id, JobType.scrape)

    try:
        import asyncio

        completed = asyncio.run(run())
    finally:
        scheduler.stop(timeout=5)

    assert completed.status == JobStatus.completed
    assert completed.progress == 100
    assert completed.current_message == "Scraped"
