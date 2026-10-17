"""
NovelBridge — Async pipeline orchestrator.
Scrape → Translate → Save, with concurrency limits and resumability.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import weakref
import httpx
from datetime import datetime
from pathlib import Path
import re
from typing import Callable, List, Optional
from urllib.parse import urlparse

from backend.adapters.base import AdapterRegistry, ChapterRef, SiteAdapter
from backend.adapters.health import get_source_health
from backend.adapters.tiers import is_stale
from backend.config import get_config
from backend.database import (
    create_job, get_chapters, get_all_glossary_rules, get_novel_glossary,
    get_job, get_novel, get_pending_translation_chapters,
    get_novel_chapter_images as load_novel_chapter_images, refresh_volumes, update_chapter,
    repair_kolnovel_pdf_chapter_duplicates, update_job, update_novel, upsert_chapter,
)
from backend.epub_builder import build_epub
from backend.models import (
    Chapter, ChapterStatus, GlossaryRule, Job, JobStatus, JobType, NovelStatus,
)
from backend.translation import (
    ProviderFailureError,
    get_provider,
)

logger = logging.getLogger("novelbridge.pipeline")


class EmptyChapterError(ValueError):
    pass


_VOLUME_TITLE_RE = re.compile(r"\b(?:volume|vol\.?|book|arc)\s*(\d+)\b", re.IGNORECASE)


def _volume_metadata(chapter_ref: ChapterRef) -> tuple[Optional[int], Optional[str]]:
    serial = chapter_ref.volume_serial
    title = chapter_ref.volume_title
    if serial is None and title:
        match = _VOLUME_TITLE_RE.search(title)
        if match:
            serial = int(match.group(1))
    if serial is not None:
        if serial < 1:
            raise ValueError(f"Chapter volume serial must be positive: {serial}")
        return serial, title
    match = _VOLUME_TITLE_RE.search(chapter_ref.title)
    if not match:
        return None, None
    serial = int(match.group(1))
    return serial, f"Volume {serial}"

_MAX_CONCURRENT = get_config().crawler.max_concurrent_jobs


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
_origin_semaphores: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def _get_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        _semaphores[loop] = semaphore
    return semaphore


def _get_origin_semaphore(origin: str) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphores = _origin_semaphores.get(loop)
    if semaphores is None:
        semaphores = {}
        _origin_semaphores[loop] = semaphores
    key = origin.lower()
    semaphore = semaphores.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(get_config().crawler.max_sessions_per_origin)
        semaphores[key] = semaphore
    return semaphore


# ── Progress callback type & JobControl ────────────────────────────────────────
ProgressCallback = Callable[[int, int, str], None]  # (done, total, message)


class JobControl:
    """
    Thread-safe controller for pausing, resuming, and cancelling background jobs.
    """
    def __init__(self, job_id: Optional[int] = None):
        self._paused = False
        self._cancelled = False
        self._job_id = job_id

    def bind_job(self, job_id: int) -> None:
        self._job_id = job_id
        if self._cancelled:
            from backend.database import cancel_job

            cancel_job(job_id)
        elif self._paused:
            update_job(job_id, status=JobStatus.paused)

    def pause(self) -> None:
        self._paused = True
        if self._job_id is not None:
            job = get_job(self._job_id)
            if job and job.status in (JobStatus.queued, JobStatus.running):
                update_job(self._job_id, status=JobStatus.paused)

    def resume(self) -> None:
        self._paused = False
        if self._job_id is not None:
            job = get_job(self._job_id)
            if job and job.status == JobStatus.paused:
                update_job(
                    self._job_id,
                    status=JobStatus.queued if job.started_at is None else JobStatus.running,
                )

    def cancel(self) -> None:
        self._cancelled = True
        self._paused = False
        if self._job_id is not None:
            from backend.database import cancel_job

            cancel_job(self._job_id)

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
    job_id: Optional[int] = None,
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

    job = get_job(job_id) if job_id is not None else None
    if job_id is not None and job is None:
        raise ValueError(f"Job {job_id} not found.")
    job = job or create_job(novel_id, JobType.scrape)
    update_job(job.id, status=JobStatus.running)

    done = 0
    failed = 0
    total = 1
    origin = urlparse(novel.source_url).netloc
    try:
        if job_control:
            await job_control.check()

        # Step 1: Fetch metadata
        if progress_cb:
            progress_cb(0, 1, "Fetching novel metadata…")
        async with _get_origin_semaphore(origin):
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
        async with _get_origin_semaphore(origin):
            chapter_refs = await adapter.get_chapter_list(novel.source_url)

        if adapter.site_id == "lncrawl_ar_kolnovel":
            repaired = repair_kolnovel_pdf_chapter_duplicates(
                novel_id,
                [ref.source_url for ref in chapter_refs],
            )
            if repaired:
                logger.info(
                    "Removed %s duplicate Kolnovel PDF chapter entries for novel %s",
                    repaired,
                    novel_id,
                )

        # Seed chapter rows (idempotent)
        for ref in chapter_refs:
            volume_serial, volume_title = _volume_metadata(ref)
            upsert_chapter(
                novel_id,
                ref.index,
                ref.title,
                ref.source_url,
                volume_serial=volume_serial,
                volume_title=volume_title,
            )
        refresh_volumes(novel_id)

        # Step 3: Scrape pending chapters concurrently, bounded by the shared semaphore.
        chapters_by_index = {chapter.index: chapter for chapter in get_chapters(novel_id)}
        MAX_EMPTY_ATTEMPTS = 3
        min_chapter_chars = get_config().crawler.min_chapter_chars
        pending_refs = [
            ref for ref in chapter_refs
            if chapters_by_index.get(ref.index)
            and (
                chapters_by_index[ref.index].status in (ChapterStatus.pending, ChapterStatus.failed)
                or (
                    chapters_by_index[ref.index].status in (
                        ChapterStatus.scraped,
                        ChapterStatus.translated,
                    )
                    and is_stale(
                        {
                            "crawler_version": chapters_by_index[ref.index].crawler_version,
                            "crawler_tier": chapters_by_index[ref.index].crawler_tier,
                        },
                        getattr(adapter, "version", None),
                        getattr(adapter, "tier", "legacy"),
                    )
                )
            )
            and (chapters_by_index[ref.index].empty_attempts or 0) < MAX_EMPTY_ATTEMPTS
        ]
        total = len(pending_refs)
        update_job(job.id, total_items=total)

        if progress_cb:
            progress_cb(
                0,
                total,
                f"Found {len(chapter_refs)} chapters; {total} need scraping…",
            )

        if not pending_refs:
            failed_chapters = sum(
                chapter.status == ChapterStatus.failed
                for chapter in chapters_by_index.values()
            )
            final_status = NovelStatus.failed if failed_chapters else NovelStatus.scraped
            update_novel(novel_id, status=final_status)
            update_job(
                job.id,
                status=JobStatus.failed if failed_chapters else JobStatus.completed,
                progress=100,
                done_items=0,
                failed_items=failed_chapters,
                error_message=(
                    f"{failed_chapters} chapter(s) remain failed or exhausted retries."
                    if failed_chapters
                    else None
                ),
                current_message=(
                    f"{failed_chapters} chapter(s) remain failed or exhausted retries."
                    if failed_chapters
                    else "All chapters are already scraped."
                ),
            )
            if progress_cb:
                progress_cb(
                    0,
                    0,
                    f"{failed_chapters} chapter(s) remain failed or exhausted retries."
                    if failed_chapters
                    else "All chapters are already scraped.",
                )
            return get_job_obj(job.id)

        async def scrape_one(ref):
            nonlocal done, failed
            chapter = chapters_by_index[ref.index]
            origin = urlparse(ref.source_url).netloc
            async with _get_semaphore(), _get_origin_semaphore(origin):
                try:
                    if job_control:
                        await job_control.check()
                    text = await adapter.get_chapter_text(ref.source_url)
                    stripped = (text or "").strip()
                    if len(stripped) < min_chapter_chars:
                        attempts = (chapter.empty_attempts or 0) + 1
                        import urllib.parse
                        origin = urllib.parse.urlparse(ref.source_url).netloc
                        get_source_health().record(origin, "empty_body")
                        raise EmptyChapterError(
                            f"Scraper returned empty chapter text (<{min_chapter_chars} chars, attempt "
                            f"{attempts}/{MAX_EMPTY_ATTEMPTS})."
                        )

                    if type(adapter).get_chapter_images is not SiteAdapter.get_chapter_images:
                        from backend.chapter_images import store_chapter_images
                        try:
                            images = await adapter.get_chapter_images(ref.source_url)
                        except (httpx.HTTPError, TimeoutError, OSError, RuntimeError) as exc:
                            logger.warning(
                                "Could not discover images for chapter %s: %s",
                                ref.index,
                                exc,
                            )
                        else:
                            await store_chapter_images(novel_id, chapter.id, images)

                    import hashlib
                    chash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    changed_content = chapter.content_hash != chash
                    version = str(getattr(adapter, "version", "1"))
                    tier = str(getattr(adapter, "tier", "legacy"))
                    changes = {
                        "raw_text": text,
                        "status": ChapterStatus.scraped if changed_content else chapter.status,
                        "empty_attempts": 0,
                        "content_hash": chash,
                        "crawler_version": version,
                        "crawler_tier": tier,
                    }
                    if changed_content:
                        changes.update(translated_text=None, translated_hash=None)
                    update_chapter(chapter.id, **changes)
                except asyncio.CancelledError:
                    raise
                except EmptyChapterError as e:
                    failed += 1
                    attempts = (chapter.empty_attempts or 0) + 1
                    update_chapter(chapter.id, status=ChapterStatus.failed, empty_attempts=attempts)
                    logger.warning("Failed to scrape chapter %s: %s", ref.index, e)
                except Exception as e:
                    failed += 1
                    logger.warning(f"Failed to scrape chapter {ref.index}: {e}")
                    update_chapter(chapter.id, status=ChapterStatus.failed)
            done += 1
            progress = int(done / total * 100)
            update_job(job.id, done_items=done, progress=progress)
            if progress_cb:
                progress_cb(done, total, f"Scraped chapter {done}/{total}: {ref.title[:50]}")

        await asyncio.gather(*(scrape_one(ref) for ref in pending_refs))

        final_status = NovelStatus.failed if failed else NovelStatus.scraped
        update_novel(novel_id, status=final_status)
        update_job(
            job.id,
            status=JobStatus.failed if failed else JobStatus.completed,
            progress=100,
            done_items=total,
            failed_items=failed,
            error_message=f"{failed} chapter(s) failed to scrape." if failed else None,
            current_message=(
                f"Scraping finished with {failed} failed chapter(s)."
                if failed
                else "Scraping complete!"
            ),
        )
        if progress_cb:
            progress_cb(
                total,
                total,
                f"Scraping finished with {failed} failed chapter(s)."
                if failed
                else "Scraping complete!",
            )
        return get_job_obj(job.id)

    except asyncio.CancelledError:
        logger.info(f"Scrape job for novel {novel_id} was cancelled.")
        update_novel(novel_id, status=NovelStatus.pending)
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
    job_id: Optional[int] = None,
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

    adapter = AdapterRegistry.find(novel.source_url)
    if adapter and getattr(adapter, "is_native_arabic", False):
        raise ValueError("This novel is already in Arabic and does not need translation.")

    provider = get_provider(provider_name, model=model_name)
    glossary_by_source = {
        rule.source_term.casefold(): rule
        for rule in get_all_glossary_rules()
        if rule.is_global
    }
    glossary_by_source.update({
        source.casefold(): GlossaryRule(
            source_term=source,
            target_term=target,
            notes="Novel-specific glossary",
            is_global=False,
        )
        for source, target in get_novel_glossary(novel_id, "ar").items()
    })
    glossary = list(glossary_by_source.values())

    pending = get_pending_translation_chapters(novel_id)
    chapter_map = {chapter.id: chapter for chapter in get_chapters(novel_id)}
    if not chapter_ids:
        import hashlib

        for chapter in chapter_map.values():
            if (
                chapter.status == ChapterStatus.translated
                and chapter.raw_text
            ):
                source_hash = hashlib.sha256(chapter.raw_text.encode("utf-8")).hexdigest()
                has_translation = bool(chapter.translated_text and chapter.translated_text.strip())
                source_changed = (
                    chapter.translated_hash is not None
                    and chapter.translated_hash != source_hash
                )
                if not has_translation or source_changed:
                    pending.append(chapter)
    pending.sort(key=lambda chapter: chapter.index)
    if chapter_ids:
        pending_by_id = {chapter.id: chapter for chapter in pending}
        pending = [pending_by_id[chapter_id] for chapter_id in chapter_ids if chapter_id in pending_by_id]

    total = len(pending)
    if total == 0:
        job = get_job(job_id) if job_id is not None else None
        if job_id is not None and job is None:
            raise ValueError(f"Job {job_id} not found.")
        job = job or create_job(novel_id, JobType.translate)
        update_job(
            job.id,
            status=JobStatus.completed,
            progress=100,
            done_items=0,
            total_items=0,
            current_message="All chapters already have current translations.",
        )
        return get_job_obj(job.id)

    job = get_job(job_id) if job_id is not None else None
    if job_id is not None and job is None:
        raise ValueError(f"Job {job_id} not found.")
    job = job or create_job(novel_id, JobType.translate, total_items=total)
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
                    source_text = (chapter.raw_text or "").strip()
                    if not source_text:
                        raise ValueError(
                            f"Chapter {chapter.index} has no scraped source text. "
                            "Run scraping again before translating."
                        )
                    import hashlib
                    source_hash = hashlib.sha256((chapter.raw_text or "").encode("utf-8")).hexdigest()
                    if (
                        chapter.status == ChapterStatus.translated
                        and chapter.translated_text
                        and chapter.translated_text.strip()
                        and (
                            chapter.translated_hash is None
                            or chapter.translated_hash == source_hash
                        )
                    ):
                        return
                    previous = next(
                        (
                            item.translated_text
                            for item in reversed(get_chapters(novel_id))
                            if item.index < chapter.index
                            and item.translated_text
                            and item.status == ChapterStatus.translated
                        ),
                        None,
                    )
                    context = (previous or "")[-500:]
                    elapsed = asyncio.get_running_loop().time() - last_request_finished_at
                    if elapsed < request_cooldown_seconds:
                        await asyncio.sleep(request_cooldown_seconds - elapsed)
                    update_chapter(chapter.id, status=ChapterStatus.translating)
                    translate_options = {"model": model_name}
                    if "context" in inspect.signature(provider.translate_chapter).parameters:
                        translate_options["context"] = context or None
                    translated = await provider.translate_chapter(
                        source_text,
                        glossary,
                        **translate_options,
                    )
                    last_request_finished_at = asyncio.get_running_loop().time()
                    update_chapter(
                        chapter.id,
                        translated_text=translated,
                        translated_hash=source_hash,
                        status=ChapterStatus.translated,
                    )
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
    job_id: Optional[int] = None,
    job_control: Optional[JobControl] = None,
    artifact_format: str = "epub",
    volume: Optional[int] = None,
    language: str = "ar",
) -> tuple[Job, Path]:
    """Build an EPUB or another supported downloadable artifact."""
    novel = get_novel(novel_id)
    if not novel:
        raise ValueError(f"Novel {novel_id} not found.")

    chapters = get_chapters(novel_id)
    if volume is not None:
        chapters = [
            chapter for chapter in chapters
            if (chapter.volume_serial or chapter.index // 100 + 1) == volume
        ]

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

    job = get_job(job_id) if job_id is not None else None
    if job_id is not None and job is None:
        raise ValueError(f"Job {job_id} not found.")
    job = job or create_job(novel_id, JobType.build_epub, total_items=1)
    update_job(job.id, status=JobStatus.running)

    try:
        if job_control:
            await job_control.check()
        if progress_cb:
            progress_cb(0, 1, f"Building {artifact_format.upper()} from {available_count} chapters…")

        from backend.binder import make_artifact
        output_dir = Path(os.getenv("OUTPUT_DIR", "output"))
        images_by_chapter = (
            load_novel_chapter_images(novel_id)
            if artifact_format.lower() == "epub"
            else {}
        )
        output_path = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: make_artifact(
                novel,
                chapters,
                artifact_format,
                output_dir,
                volume=volume,
                language=language,
                native_arabic=is_native_ar,
                chapter_images=images_by_chapter,
            ),
        )
        if job_control:
            await job_control.check()

        update_job(
            job.id,
            status=JobStatus.completed,
            progress=100,
            done_items=1,
            output_path=str(output_path),
        )
        if progress_cb:
            progress_cb(1, 1, f"Artifact saved: {output_path.name}")
        return get_job_obj(job.id), output_path

    except asyncio.CancelledError:
        update_job(job.id, status=JobStatus.cancelled, current_message="Cancelled")
        raise
    except Exception as e:
        logger.error(f"EPUB job failed: {e}", exc_info=True)
        update_job(job.id, status=JobStatus.failed, error_message=str(e))
        raise


def get_job_obj(job_id: int) -> Optional[Job]:
    from backend.database import get_job
    return get_job(job_id)
