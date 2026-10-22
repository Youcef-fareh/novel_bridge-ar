"""Persistent, fair background job queue shared by the API and desktop GUI."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from typing import Any, Callable, Optional

from backend.database import (
    cancel_job,
    claim_next_job,
    enqueue_job,
    get_job,
    heartbeat_job,
    recover_interrupted_jobs,
    requeue_job,
    update_job,
)
from backend.config import get_config
from backend.models import Job, JobStatus, JobType

logger = logging.getLogger("novelbridge.scheduler")

ProgressCallback = Callable[[int, int, str], None]


class JobCancelledError(RuntimeError):
    """A scheduled job reached a terminal 'cancelled' state.

    Raised by run_job/wait_for_job as a normal exception — raising
    asyncio.CancelledError here would be interpreted as cancellation of the
    *calling* task (e.g. an HTTP request), dropping the client's connection
    without a proper response.
    """


class JobScheduler:
    def __init__(self) -> None:
        config = get_config()
        self.worker_count = config.crawler.max_concurrent_jobs
        self.max_per_domain = config.crawler.max_sessions_per_origin
        self.poll_interval = float(os.getenv("SCHEDULER_POLL_SECONDS", "0.5"))
        if self.worker_count < 1:
            raise ValueError("MAX_CONCURRENT_JOBS must be at least 1")
        if self.max_per_domain < 1:
            raise ValueError("MAX_CONCURRENT_PER_DOMAIN must be at least 1")
        if self.poll_interval <= 0:
            raise ValueError("SCHEDULER_POLL_SECONDS must be greater than 0")

        self._lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._instance_id = uuid.uuid4().hex[:12]

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_running:
                return
            self._stop_requested.clear()
            self._ready.clear()
            # NOTE: recovery and scrubbing intentionally run inside the
            # scheduler thread's maintenance loop, NOT here. start() is called
            # synchronously from async API endpoints (enqueue), and the heavy
            # DB/filesystem work used to block the uvicorn event loop for
            # seconds on cold start.
            self._thread = threading.Thread(
                target=self._thread_main,
                name="novelbridge-job-scheduler",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("Scheduler worker thread did not start within 10 seconds")

    def stop(self, timeout: Optional[float] = None) -> None:
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_requested.set()
        thread.join(timeout=timeout)
        if not thread.is_alive():
            with self._lock:
                self._thread = None
                self._ready.clear()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception:
            logger.exception("Persistent scheduler stopped unexpectedly")
        finally:
            self._ready.clear()

    async def _serve(self) -> None:
        try:
            # Requeue jobs interrupted by the previous process before workers
            # can claim anything. Runs here (scheduler thread) so start() never
            # blocks the API's event loop.
            await asyncio.to_thread(recover_interrupted_jobs)
        except Exception:
            logger.exception("Startup job recovery failed")
        self._ready.set()
        workers = [
            asyncio.create_task(self._worker_loop(index), name=f"job-worker-{index}")
            for index in range(self.worker_count)
        ]
        maintenance = asyncio.create_task(self._maintenance_loop(), name="job-maintenance")
        try:
            while not self._stop_requested.is_set():
                await asyncio.sleep(self.poll_interval)
        finally:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
            await asyncio.gather(*workers, return_exceptions=True)

    async def _maintenance_loop(self) -> None:
        from backend.scrubber import run_scrubber

        last_scrub = 0.0
        while not self._stop_requested.is_set():
            try:
                recover_interrupted_jobs()
                now = asyncio.get_running_loop().time()
                if now - last_scrub >= 24 * 60 * 60:
                    await asyncio.to_thread(run_scrubber)
                    last_scrub = now
            except Exception:
                logger.exception("Scheduler maintenance failed")
            try:
                await asyncio.wait_for(self._stop_async_wait(), timeout=10)
            except asyncio.TimeoutError:
                continue

    async def _stop_async_wait(self) -> None:
        while not self._stop_requested.is_set():
            await asyncio.sleep(1)

    async def _worker_loop(self, worker_number: int) -> None:
        worker_id = f"{os.getpid()}:{self._instance_id}:{worker_number}"
        while not self._stop_requested.is_set():
            try:
                job = await asyncio.to_thread(
                    claim_next_job,
                    worker_id,
                    self.max_per_domain,
                )
            except Exception:
                logger.exception("Worker %s failed to claim a queued job", worker_id)
                await asyncio.sleep(self.poll_interval)
                continue
            if job is None:
                await asyncio.sleep(self.poll_interval)
                continue
            await self._execute(job, worker_id)

    async def _execute(self, job: Job, worker_id: str) -> None:
        heartbeat = asyncio.create_task(self._heartbeat_loop(job.id, worker_id))
        try:
            options = json.loads(job.options_json or "{}")
            if not isinstance(options, dict):
                raise ValueError(f"Job {job.id} options must be a JSON object")
            progress_cb = self._progress_callback(job.id)
            control = _PersistentJobControl(job.id)

            from backend.pipeline import run_epub_job, run_scrape_job, run_translation_job

            if job.type == JobType.scrape:
                await run_scrape_job(
                    job.novel_id,
                    progress_cb=progress_cb,
                    job_control=control,
                    job_id=job.id,
                )
            elif job.type == JobType.translate:
                await run_translation_job(
                    job.novel_id,
                    chapter_ids=options.get("chapter_ids"),
                    provider_name=options.get("provider_name"),
                    model_name=options.get("model_name"),
                    progress_cb=progress_cb,
                    job_control=control,
                    request_cooldown_seconds=options.get("request_cooldown_seconds", 0.0),
                    job_id=job.id,
                )
            elif job.type == JobType.build_epub:
                _, output_path = await run_epub_job(
                    job.novel_id,
                    progress_cb=progress_cb,
                    job_id=job.id,
                    job_control=control,
                    artifact_format=options.get("format", "epub"),
                    volume=options.get("volume"),
                    language=options.get("language", "ar"),
                )
                update_job(job.id, output_path=str(output_path))
            else:
                raise ValueError(f"Unsupported persistent job type: {job.type}")

            current = get_job(job.id)
            if current and current.status in (JobStatus.running, JobStatus.paused):
                update_job(
                    job.id,
                    status=JobStatus.completed,
                    progress=100,
                    current_message="Completed",
                )
        except asyncio.CancelledError:
            if self._stop_requested.is_set():
                # Scheduler/process shutdown, NOT a user cancel: requeue the
                # job so the next start recovers it (crash-recovery path),
                # preserving completed chapter work, and let the worker task
                # observe the real cancellation.
                try:
                    requeue_job(job.id, "Requeued after scheduler shutdown")
                    logger.info("Job %s requeued during scheduler shutdown", job.id)
                except Exception:
                    logger.exception("Failed to requeue job %s on shutdown", job.id)
                raise
            # User cancel (via JobControl.check) — the job row is already
            # terminal; swallow so this worker keeps serving other jobs.
            cancel_job(job.id)
            logger.info("Job %s was cancelled", job.id)
        except Exception as exc:
            logger.exception("Job %s failed", job.id)
            current = get_job(job.id)
            if current and current.status not in (
                JobStatus.cancelled,
                JobStatus.completed,
                JobStatus.failed,
            ):
                update_job(job.id, status=JobStatus.failed, error_message=str(exc))
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeat_loop(self, job_id: int, worker_id: str) -> None:
        while True:
            await asyncio.sleep(10)
            if not await asyncio.to_thread(heartbeat_job, job_id, worker_id):
                return

    @staticmethod
    def _progress_callback(job_id: int) -> ProgressCallback:
        def update_progress(done: int, total: int, message: str) -> None:
            progress = int(done / total * 100) if total else 0
            update_job(
                job_id,
                done_items=done,
                total_items=total,
                progress=progress,
                current_message=message,
            )

        return update_progress

    def enqueue(
        self,
        novel_id: int,
        job_type: JobType,
        *,
        options: Optional[dict[str, Any]] = None,
        parent_job_id: Optional[int] = None,
        priority: int = 0,
    ) -> Job:
        self.start()
        return enqueue_job(
            novel_id,
            job_type,
            options=options,
            parent_job_id=parent_job_id,
            priority=priority,
        )

    async def run_job(
        self,
        novel_id: int,
        job_type: JobType,
        *,
        options: Optional[dict[str, Any]] = None,
        progress_cb: Optional[ProgressCallback] = None,
        job_control: Any = None,
        parent_job_id: Optional[int] = None,
        priority: int = 0,
    ) -> Job:
        job = self.enqueue(
            novel_id,
            job_type,
            options=options,
            parent_job_id=parent_job_id,
            priority=priority,
        )
        if job_control and hasattr(job_control, "bind_job"):
            job_control.bind_job(job.id)

        previous_progress: tuple[Any, ...] = ()
        while True:
            current = await asyncio.to_thread(get_job, job.id)
            if current is None:
                raise RuntimeError(f"Scheduled job {job.id} disappeared")

            snapshot = (
                current.status,
                current.progress,
                current.done_items,
                current.total_items,
                current.current_message,
            )
            if progress_cb and snapshot != previous_progress:
                progress_cb(
                    current.done_items,
                    current.total_items,
                    current.current_message or current.status.value,
                )
                previous_progress = snapshot

            if current.status == JobStatus.completed:
                return current
            if current.status == JobStatus.cancelled:
                raise JobCancelledError(f"Job {job.id} was cancelled")
            if current.status == JobStatus.failed:
                raise RuntimeError(current.error_message or f"Job {job.id} failed")
            await asyncio.sleep(self.poll_interval)

    async def wait_for_job(
        self,
        job_id: int,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> Job:
        previous_progress: tuple[Any, ...] = ()
        while True:
            current = await asyncio.to_thread(get_job, job_id)
            if current is None:
                raise ValueError(f"Job {job_id} not found")
            snapshot = (
                current.status,
                current.progress,
                current.done_items,
                current.total_items,
                current.current_message,
            )
            if progress_cb and snapshot != previous_progress:
                progress_cb(
                    current.done_items,
                    current.total_items,
                    current.current_message or current.status.value,
                )
                previous_progress = snapshot
            if current.status == JobStatus.completed:
                return current
            if current.status == JobStatus.cancelled:
                raise JobCancelledError(f"Job {job_id} was cancelled")
            if current.status == JobStatus.failed:
                raise RuntimeError(current.error_message or f"Job {job_id} failed")
            await asyncio.sleep(self.poll_interval)


class _PersistentJobControl:
    def __init__(self, job_id: int) -> None:
        self.job_id = job_id

    async def check(self, on_paused: Optional[Callable[[], None]] = None) -> None:
        while True:
            job = await asyncio.to_thread(get_job, self.job_id)
            if job is None:
                raise RuntimeError(f"Job {self.job_id} not found")
            if job.status == JobStatus.cancelled:
                raise asyncio.CancelledError(f"Job {self.job_id} was cancelled")
            if job.status == JobStatus.paused:
                if on_paused:
                    on_paused()
                await asyncio.sleep(0.25)
                continue
            if job.status != JobStatus.running:
                raise RuntimeError(
                    f"Job {self.job_id} cannot continue from state {job.status.value}"
                )
            return


scheduler = JobScheduler()
