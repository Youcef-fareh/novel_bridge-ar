"""
NovelBridge — Async pipeline orchestrator.
Scrape → Translate → Save, with concurrency limits and resumability.
"""
from __future__ import annotations

import asyncio
import logging
import os
import weakref
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from backend.adapters.base import AdapterRegistry
from backend.database import (
    create_job, get_chapters, get_all_glossary_rules,
    get_novel, get_pending_translation_chapters,
    update_chapter, update_job, update_novel, upsert_chapter,
)
from backend.epub_builder import build_epub
from backend.models import (
    Chapter, ChapterStatus, Job, JobStatus, JobType, NovelStatus,
)
from backend.translation import (
    ProviderFailureError,
    get_provider,
)

logger = logging.getLogger("novelbridge.pipeline")

_MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_JOBS", "3"))


def _is_rate_limit_error(error: Exception) -> bool:
    """Return True only for provider rate-limit responses that need recovery time."""
    response = getattr(error, "response", None)
    status_code = getattr(error, "status_code", None) or getattr(response, "status_code", None)
    if status_code == 429:
        return True

    message = str(error).lower()
    return "429" in message or "too many requests" in message or "rate limit exceeded" in message

# Each GUI worker owns a separate event loop. Keep one semaphore per loop.
_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _get_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        _semaphores[loop] = semaphore
    return semaphore



# ── Progress callback type & JobControl ────────────────────────────────────────
ProgressCallback = Callable[[int, int, str], None]  # (done, total, message)


class JobControl:
    """
    Thread-safe controller for pausing, resuming, and cancelling background jobs.
    """
    def __init__(self):
        self._paused = False
        self._cancelled = False

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def cancel(self) -> None:
        self._cancelled = True
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    async def check(self, on_paused: Optional[Callable[[], None]] = None) -> None:
        """
        Check cancellation and pause state.
        If cancelled, raises asyncio.CancelledError.
        If paused, waits until resume() or cancel() is called.
        """
        if self._cancelled:
            raise asyncio.CancelledError("Job was cancelled by the user.")
        if self._paused:
            if on_paused:
                on_paused()
            while self._paused and not self._cancelled:
                await asyncio.sleep(0.25)
            if self._cancelled:
                raise asyncio.CancelledError("Job was cancelled by the user.")


# ── Scrape pipeline ────────────────────────────────────────────────────────────

async def run_scrape_job(
    novel_id: int,
    progress_cb: Optional[ProgressCallback] = None,
    job_control: Optional[JobControl] = None,
) -> Job:
    """
    Scrape novel metadata + all chapters, save to SQLite.
    Resumes from last successful chapter if interrupted.
    """
    novel = get_novel(novel_id)
    if not novel:
        raise ValueError(f"Novel {novel_id} not found in database.")

    adapter = AdapterRegistry.find(novel.source_url)
    if not adapter:
        raise ValueError(f"No adapter found for URL: {novel.source_url}")

    job = create_job(novel_id, JobType.scrape)
    update_job(job.id, status=JobStatus.running)

    done = 0
    total = 1
    try:
        if job_control:
            await job_control.check()

        # Step 1: Fetch metadata
        if progress_cb:
            progress_cb(0, 1, "Fetching novel metadata…")
        meta = await adapter.get_novel_metadata(novel.source_url)
        update_novel(
            novel_id,
            title=meta.title,
            author=meta.author,
            cover_url=meta.cover_url,
            description=meta.description,
            status=NovelStatus.scraping,
        )

        if job_control:
            await job_control.check()

        # Step 2: Fetch chapter list
        if progress_cb:
            progress_cb(0, 1, "Fetching chapter list…")
        chapter_refs = await adapter.get_chapter_list(novel.source_url)
        total = len(chapter_refs)
        update_job(job.id, total_items=total)

        if progress_cb:
            progress_cb(0, total, f"Found {total} chapters. Starting scrape…")

        # Seed chapter rows (idempotent)
        for ref in chapter_refs:
            upsert_chapter(novel_id, ref.index, ref.title, ref.source_url)

        # Step 3: Scrape pending chapters concurrently, bounded by the shared semaphore.
        chapters_by_index = {chapter.index: chapter for chapter in get_chapters(novel_id)}
        pending_refs = [
            ref for ref in chapter_refs
            if chapters_by_index.get(ref.index)
            and chapters_by_index[ref.index].status in (ChapterStatus.pending, ChapterStatus.failed)
        ]
        done += total - len(pending_refs)

        async def scrape_one(ref):
            nonlocal done
            chapter = chapters_by_index[ref.index]
            async with _get_semaphore():
                try:
                    if job_control:
                        await job_control.check()
                    text = await adapter.get_chapter_text(ref.source_url)
                    update_chapter(chapter.id, raw_text=text, status=ChapterStatus.scraped)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Failed to scrape chapter {ref.index}: {e}")
                    update_chapter(chapter.id, status=ChapterStatus.failed)
            done += 1
            progress = int(done / total * 100)
            update_job(job.id, done_items=done, progress=progress)
            if progress_cb:
                progress_cb(done, total, f"Scraped chapter {done}/{total}: {ref.title[:50]}")

        await asyncio.gather(*(scrape_one(ref) for ref in pending_refs))

        update_novel(novel_id, status=NovelStatus.scraped)
        update_job(job.id, status=JobStatus.completed, progress=100, done_items=total)
        if progress_cb:
            progress_cb(total, total, "Scraping complete!")
        return get_job_obj(job.id)

    except asyncio.CancelledError:
        logger.info(f"Scrape job for novel {novel_id} was cancelled.")
        update_job(job.id, status=JobStatus.cancelled, done_items=done)
        raise
    except Exception as e:
        logger.error(f"Scrape job failed: {e}", exc_info=True)
        update_job(job.id, status=JobStatus.failed, error_message=str(e))
        raise


# ── Translation pipeline ───────────────────────────────────────────────────────

async def run_translation_job(
    novel_id: int,
    chapter_ids: Optional[List[int]] = None,
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    progress_cb: Optional[ProgressCallback] = None,
    job_control: Optional[JobControl] = None,
    request_cooldown_seconds: float = 0.0,
) -> Job:
    """
    Translate scraped chapters.
    Supports selecting provider (tokenrouter, gemini, groq, auto) and specific model.
    If chapter_ids is None, translate all pending chapters.
    Resumes: already-translated chapters are skipped.
    If provider fails repeatedly, safely stops and suggests switching provider.
    """
    novel = get_novel(novel_id)
    if not novel:
        raise ValueError(f"Novel {novel_id} not found.")

    provider = get_provider(provider_name, model=model_name)
    glossary = get_all_glossary_rules()

    pending = get_pending_translation_chapters(novel_id)
    if chapter_ids:
        pending = [c for c in pending if c.id in chapter_ids]

    total = len(pending)
    if total == 0:
        raise ValueError("No chapters to translate (all may already be translated).")

    job = create_job(novel_id, JobType.translate, total_items=total)
    update_job(job.id, status=JobStatus.running)

    target_model_info = f" ({model_name})" if model_name else ""
    if progress_cb:
        progress_cb(
            0,
            total,
            f"Translating {total} chapters with {provider.provider_name.capitalize()}{target_model_info}…",
        )

    request_cooldown_seconds = max(0.0, float(request_cooldown_seconds))
    provider_error_wait_seconds = 80.0
    last_error_msg = ""
    stop_event = asyncio.Event()
    request_lock = asyncio.Lock()
    last_request_finished_at = 0.0

    async def translate_one(chapter: Chapter) -> None:
        nonlocal last_error_msg, last_request_finished_at
        if stop_event.is_set():
            return
        if job_control:
            await job_control.check()

        async with _get_semaphore(), request_lock:
            if stop_event.is_set():
                return

            for attempt in (1, 2):
                try:
                    if job_control:
                        await job_control.check()
                    elapsed = asyncio.get_running_loop().time() - last_request_finished_at
                    if elapsed < request_cooldown_seconds:
                        await asyncio.sleep(request_cooldown_seconds - elapsed)
                    update_chapter(chapter.id, status=ChapterStatus.translating)
                    translated = await provider.translate_chapter(
                        chapter.raw_text or "",
                        glossary,
                        model=model_name,
                    )
                    last_request_finished_at = asyncio.get_running_loop().time()
                    update_chapter(chapter.id, translated_text=translated, status=ChapterStatus.translated)
                    return
                except asyncio.CancelledError:
                    update_chapter(chapter.id, status=ChapterStatus.scraped)
                    raise
                except Exception as e:
                    last_request_finished_at = asyncio.get_running_loop().time()
                    last_error_msg = str(e)
                    logger.warning(
                        f"Failed to translate chapter {chapter.index} with {provider.provider_name} "
                        f"(attempt {attempt}/2): {e}"
                    )
                    update_chapter(chapter.id, status=ChapterStatus.failed)

                    if attempt == 1 and _is_rate_limit_error(e):
                        if progress_cb:
                            progress_cb(
                                done,
                                total,
                                f"Provider error on chapter {chapter.index}. Retrying in "
                                f"{int(provider_error_wait_seconds)} seconds…",
                            )
                        await asyncio.sleep(provider_error_wait_seconds)
                        continue

                    if attempt == 1:
                        continue

                    stop_event.set()
                    raise ProviderFailureError(
                        provider=provider.provider_name,
                        error_detail=last_error_msg,
                        suggestion=(
                            f"Translation stopped after the provider failed twice.\n\n"
                            f"Provider error: {last_error_msg}\n\n"
                            f"Suggestion: check the API key, model, or quota, then switch to another provider."
                        ),
                    )

    done = 0
    try:
        queue: asyncio.Queue[Chapter] = asyncio.Queue()
        for ch in pending:
            queue.put_nowait(ch)

        async def worker():
            nonlocal done
            while not stop_event.is_set():
                if job_control:
                    await job_control.check()
                try:
                    chapter = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                try:
                    await translate_one(chapter)
                finally:
                    queue.task_done()

                done += 1
                progress = int(done / total * 100)
                update_job(job.id, done_items=done, progress=progress)
                if progress_cb:
                    progress_cb(done, total, f"Translated {done}/{total} chapters: {chapter.title[:40]}")

        num_workers = min(_MAX_CONCURRENT, total)
        worker_tasks = [asyncio.create_task(worker()) for _ in range(num_workers)]
        await asyncio.gather(*worker_tasks)

        if stop_event.is_set():
            suggestion = (
                f"Translation stopped because provider '{provider.provider_name}' failed after multiple attempts "
                f"({last_error_msg}).\n\n"
                f"💡 Suggestion: Please change the translation provider or model (e.g., switch to TokenRouter, Gemini, or Groq) "
                f"using the Provider dropdown, or check your API key in the 'API Keys' tab."
            )
            update_job(job.id, status=JobStatus.failed, error_message=suggestion)
            raise ProviderFailureError(
                provider=provider.provider_name,
                error_detail=last_error_msg,
                suggestion=suggestion,
            )

        update_job(job.id, status=JobStatus.completed, progress=100, done_items=total)
        if progress_cb:
            progress_cb(total, total, "Translation complete!")
        return get_job_obj(job.id)

    except asyncio.CancelledError:
        logger.info(f"Translation job for novel {novel_id} was cancelled.")
        update_job(job.id, status=JobStatus.cancelled, done_items=done)
        raise
    except ProviderFailureError:
        raise
    except Exception as e:
        logger.error(f"Translation job failed: {e}", exc_info=True)
        update_job(job.id, status=JobStatus.failed, error_message=str(e))
        raise



# ── EPUB build pipeline ────────────────────────────────────────────────────────

async def run_epub_job(
    novel_id: int,
    progress_cb: Optional[ProgressCallback] = None,
) -> tuple[Job, Path]:
    """Build the EPUB for a novel from all translated chapters."""
    novel = get_novel(novel_id)
    if not novel:
        raise ValueError(f"Novel {novel_id} not found.")

    chapters = get_chapters(novel_id)
    
    # Check if native Arabic source
    is_native_ar = False
    if novel.source_site:
        adapter = AdapterRegistry._adapters.get(novel.source_site)
        if adapter and getattr(adapter, "is_native_arabic", False):
            is_native_ar = True

    if is_native_ar:
        available_count = sum(1 for c in chapters if (c.raw_text and c.raw_text.strip()) or (c.translated_text and c.translated_text.strip()))
        if not available_count:
            raise ValueError("No scraped chapters found to build EPUB. Please run scrape first.")
    else:
        available_count = sum(1 for c in chapters if c.translated_text and c.translated_text.strip())
        if not available_count:
            raise ValueError("No translated chapters found. Please run translation first.")

    job = create_job(novel_id, JobType.build_epub, total_items=1)
    update_job(job.id, status=JobStatus.running)

    try:
        if progress_cb:
            progress_cb(0, 1, f"Building EPUB from {available_count} chapters…")

        output_path = await asyncio.get_event_loop().run_in_executor(
            None, build_epub, novel, chapters
        )

        update_job(job.id, status=JobStatus.completed, progress=100, done_items=1)
        if progress_cb:
            progress_cb(1, 1, f"EPUB saved: {output_path.name}")
        return get_job_obj(job.id), output_path

    except Exception as e:
        logger.error(f"EPUB job failed: {e}", exc_info=True)
        update_job(job.id, status=JobStatus.failed, error_message=str(e))
        raise


def get_job_obj(job_id: int) -> Optional[Job]:
    from backend.database import get_job
    return get_job(job_id)
