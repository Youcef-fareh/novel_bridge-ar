"""
NovelBridge — Async pipeline orchestrator.
Scrape → Translate → Save, with concurrency limits and resumability.
"""
from __future__ import annotations

import asyncio
import logging
import os
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
from backend.translation.gemini_provider import GeminiProvider
from backend.translation.groq_provider import GroqProvider

logger = logging.getLogger("novelbridge.pipeline")

_MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_JOBS", "3"))

# Semaphore shared across the whole process
_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
    return _semaphore


def _pick_provider():
    """Return the best available translation provider."""
    gemini = GeminiProvider()
    if gemini.is_available():
        return gemini
    groq = GroqProvider()
    if groq.is_available():
        return groq
    raise RuntimeError(
        "No translation API key configured. "
        "Please set GEMINI_API_KEY or GROQ_API_KEY in your .env file."
    )


# ── Progress callback type ─────────────────────────────────────────────────────
ProgressCallback = Callable[[int, int, str], None]  # (done, total, message)


# ── Scrape pipeline ────────────────────────────────────────────────────────────

async def run_scrape_job(
    novel_id: int,
    progress_cb: Optional[ProgressCallback] = None,
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

    try:
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

        # Step 3: Scrape each pending chapter
        done = 0
        for ref in chapter_refs:
            chapters = get_chapters(novel_id)
            ch = next((c for c in chapters if c.index == ref.index), None)
            if ch and ch.status not in (ChapterStatus.pending, ChapterStatus.failed):
                done += 1
                continue  # Already scraped — resume support

            async with _get_semaphore():
                try:
                    text = await adapter.get_chapter_text(ref.source_url)
                    update_chapter(ch.id, raw_text=text, status=ChapterStatus.scraped)
                except Exception as e:
                    logger.warning(f"Failed to scrape chapter {ref.index}: {e}")
                    if ch:
                        update_chapter(ch.id, status=ChapterStatus.failed)

            done += 1
            progress = int(done / total * 100)
            update_job(job.id, done_items=done, progress=progress)
            if progress_cb:
                progress_cb(done, total, f"Scraped chapter {done}/{total}: {ref.title[:50]}")

        update_novel(novel_id, status=NovelStatus.scraped)
        update_job(job.id, status=JobStatus.completed, progress=100, done_items=total)
        if progress_cb:
            progress_cb(total, total, "Scraping complete!")
        return get_job_obj(job.id)

    except Exception as e:
        logger.error(f"Scrape job failed: {e}", exc_info=True)
        update_job(job.id, status=JobStatus.failed, error_message=str(e))
        raise


# ── Translation pipeline ───────────────────────────────────────────────────────

async def run_translation_job(
    novel_id: int,
    chapter_ids: Optional[List[int]] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> Job:
    """
    Translate scraped chapters.
    If chapter_ids is None, translate all pending chapters.
    Resumes: already-translated chapters are skipped.
    """
    novel = get_novel(novel_id)
    if not novel:
        raise ValueError(f"Novel {novel_id} not found.")

    provider = _pick_provider()
    glossary = get_all_glossary_rules()

    pending = get_pending_translation_chapters(novel_id)
    if chapter_ids:
        pending = [c for c in pending if c.id in chapter_ids]

    total = len(pending)
    if total == 0:
        raise ValueError("No chapters to translate (all may already be translated).")

    job = create_job(novel_id, JobType.translate, total_items=total)
    update_job(job.id, status=JobStatus.running)

    if progress_cb:
        progress_cb(0, total, f"Translating {total} chapters with {provider.provider_name}…")

    async def translate_one(chapter: Chapter) -> None:
        async with _get_semaphore():
            try:
                update_chapter(chapter.id, status=ChapterStatus.translating)
                translated = await provider.translate_chapter(chapter.raw_text or "", glossary)
                update_chapter(chapter.id, translated_text=translated, status=ChapterStatus.translated)
            except Exception as e:
                logger.warning(f"Failed to translate chapter {chapter.index}: {e}")
                update_chapter(chapter.id, status=ChapterStatus.failed)
                # Try fallback provider if Gemini failed
                groq = GroqProvider()
                if provider.provider_name == "gemini" and groq.is_available():
                    try:
                        translated = await groq.translate_chapter(chapter.raw_text or "", glossary)
                        update_chapter(chapter.id, translated_text=translated, status=ChapterStatus.translated)
                        logger.info(f"Fallback Groq succeeded for chapter {chapter.index}")
                    except Exception as e2:
                        logger.error(f"Groq fallback also failed for chapter {chapter.index}: {e2}")

    try:
        done = 0
        # Process in batches respecting the semaphore
        tasks = [translate_one(ch) for ch in pending]
        for coro in asyncio.as_completed(tasks):
            await coro
            done += 1
            progress = int(done / total * 100)
            update_job(job.id, done_items=done, progress=progress)
            if progress_cb:
                progress_cb(done, total, f"Translated {done}/{total} chapters")

        update_job(job.id, status=JobStatus.completed, progress=100)
        if progress_cb:
            progress_cb(total, total, "Translation complete!")
        return get_job_obj(job.id)

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
