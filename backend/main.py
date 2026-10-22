"""
NovelBridge — FastAPI backend.
Exposes the full REST API for the Flutter phone client.
The PyQt6 GUI calls backend functions directly (in-process).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import httpx
import logging
from pathlib import Path
import threading
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
import inspect
from urllib.parse import urlparse

from backend.adapters import register_standard_adapters
from backend.adapters.base import AdapterRegistry
from backend.adapters.health import get_source_health
from backend.adapters.preferences import get_preferences
from backend.adapters.scraper_state import get_pacer
from backend.api_errors import install_exception_handlers
from backend.chapter_images import _download_image
from backend.config import get_config, output_path
from backend.logging_setup import diagnostics_archive, setup_logging
from backend.database import (
    add_glossary_rule, create_novel, delete_glossary_rule,
    delete_novel_glossary_term, get_all_glossary_rules, get_all_novels,
    get_chapters, get_job, get_novel, get_novel_glossary as load_novel_glossary, init_db,
    get_chapter_image, get_novel_chapter_images, get_novels_page,
    get_volumes, list_jobs, replay_job, set_novel_glossary_term,
)
from backend.glossary_suggestions import suggest_glossary_terms
from backend.models import JobStatus
from backend.models import JobType
from backend.scheduler import JobCancelledError, scheduler
from backend.schemas import ArtifactRequest, JobSummary, NovelSummary, Paginated

# ── App init ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    get_config()
    init_db()
    register_standard_adapters()
    scheduler.start()
    try:
        yield
    finally:
        await asyncio.to_thread(scheduler.stop, 30)


app = FastAPI(
    title="NovelBridge API",
    description="Web novel scraper + Arabic translator + EPUB generator",
    version="0.1.0",
    lifespan=lifespan,
)
logger = logging.getLogger("novelbridge.search")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
install_exception_handlers(app)


# ── Request/Response schemas ───────────────────────────────────────────────────

class AddNovelRequest(BaseModel):
    source_url: str

class TranslateRequest(BaseModel):
    chapter_ids: Optional[List[int]] = None
    provider: Optional[str] = None
    model: Optional[str] = None

class AddGlossaryRequest(BaseModel):
    source_term: str
    target_term: str
    notes:       Optional[str] = ""


# ── Novel endpoints ────────────────────────────────────────────────────────────

@app.get("/novels")
async def list_novels():
    novels = get_all_novels()
    return [
        {
            "id": n.id,
            "title": n.title,
            "source_url": n.source_url,
            "source_site": n.source_site,
            "author": n.author,
            "cover_url": n.cover_url,
            "status": n.status,
            "created_at": n.created_at.isoformat(),
        }
        for n in novels
    ]


@app.get("/novels/page", response_model=Paginated[NovelSummary])
async def list_novels_page(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
):
    novels, total = get_novels_page(offset, limit)
    return {
        "items": [
            {
                "id": novel.id,
                "title": novel.title,
                "source_url": novel.source_url,
                "source_site": novel.source_site,
                "author": novel.author,
                "cover_url": novel.cover_url,
                "status": novel.status.value,
                "created_at": novel.created_at.isoformat(),
            }
            for novel in novels
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@app.post("/novels", status_code=201)
async def add_novel(req: AddNovelRequest):
    url = req.source_url.strip()
    adapter = AdapterRegistry.find(url)
    if not adapter:
        raise HTTPException(status_code=422, detail=f"No adapter found for URL: {url}")

    novel = create_novel(source_url=url, source_site=adapter.site_id)
    job = scheduler.enqueue(novel.id, JobType.scrape)
    return {"novel_id": novel.id, "job_id": job.id, "message": "Scrape job queued"}


@app.get("/novels/{novel_id}")
async def get_novel_detail(novel_id: int):
    novel = get_novel(novel_id)
    if not novel:
        raise HTTPException(404, "Novel not found")
    chapters = get_chapters(novel_id)
    chapter_images = get_novel_chapter_images(novel_id)
    return {
        "id": novel.id,
        "title": novel.title,
        "author": novel.author,
        "cover_url": novel.cover_url,
        "description": novel.description,
        "source_url": novel.source_url,
        "source_site": novel.source_site,
        "status": novel.status,
        "chapters": [
            {
                "id": c.id,
                "index": c.index,
                "title": c.title,
                "volume_serial": c.volume_serial,
                "status": c.status,
                "has_translation": bool(c.translated_text),
                "images": [
                    {
                        "image_id": image.image_id,
                        "url": f"/chapters/{c.id}/image/{image.image_id}",
                        "alt_text": image.alt_text,
                        "media_type": image.media_type,
                        "size_bytes": image.size_bytes,
                    }
                    for image in chapter_images.get(c.id, [])
                ],
            }
            for c in chapters
        ],
    }


@app.get("/novels/{novel_id}/volumes")
async def list_novel_volumes(novel_id: int):
    if not get_novel(novel_id):
        raise HTTPException(404, "Novel not found")
    return [
        {
            "serial": volume.serial,
            "title": volume.title,
            "chapter_count": volume.chapter_count,
        }
        for volume in get_volumes(novel_id)
    ]


@app.post("/novels/{novel_id}/translate")
async def translate_novel(novel_id: int, req: TranslateRequest):
    novel = get_novel(novel_id)
    if not novel:
        raise HTTPException(404, "Novel not found")
    adapter = AdapterRegistry.find(novel.source_url)
    if adapter and getattr(adapter, "is_native_arabic", False):
        raise HTTPException(400, "This novel is already in Arabic and does not need translation.")
    job = scheduler.enqueue(
        novel_id,
        JobType.translate,
        options={
            "chapter_ids": req.chapter_ids,
            "provider_name": req.provider,
            "model_name": req.model,
        },
    )
    return {"job_id": job.id, "message": "Translation job queued"}



@app.get("/novels/{novel_id}/epub")
async def download_epub(novel_id: int):
    novel = get_novel(novel_id)
    if not novel:
        raise HTTPException(404, "Novel not found")
    try:
        job = await scheduler.run_job(
            novel_id, JobType.build_epub, options={"format": "epub"}
        )
        if not job.output_path:
            raise HTTPException(500, f"EPUB job {job.id} completed without an output path")
        epub_path = Path(job.output_path)
        return FileResponse(
            path=str(epub_path),
            media_type="application/epub+zip",
            filename=epub_path.name,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except JobCancelledError as e:
        raise HTTPException(409, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


# ── Job endpoints ──────────────────────────────────────────────────────────────

@app.get("/jobs/{job_id}")
async def get_job_status(job_id: int):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {
        "id": job.id,
        "novel_id": job.novel_id,
        "type": job.type,
        "status": job.status,
        "progress": job.progress,
        "done_items": job.done_items,
        "total_items": job.total_items,
        "error_message": job.error_message,
        "current_message": job.current_message,
        "failed_items": job.failed_items,
        "output_path": job.output_path,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@app.get("/jobs", response_model=Paginated[JobSummary])
async def list_job_statuses(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    status: Optional[JobStatus] = None,
    type: Optional[JobType] = None,
):
    jobs, total = list_jobs(offset=offset, limit=limit, status=status, job_type=type)
    return {
        "items": [
            JobSummary(
                id=job.id,
                novel_id=job.novel_id,
                type=job.type,
                status=job.status,
                progress=job.progress,
                done_items=job.done_items,
                total_items=job.total_items,
                failed_items=job.failed_items,
                current_message=job.current_message,
                error_message=job.error_message,
                output_path=job.output_path,
            )
            for job in jobs
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@app.post("/jobs/{job_id}/replay", status_code=202)
async def replay_job_endpoint(job_id: int):
    job = replay_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    scheduler.start()
    return {"job_id": job.id, "parent_job_id": job.parent_job_id, "status": job.status}


@app.post("/jobs/{job_id}/cancel")
async def cancel_job_endpoint(job_id: int):
    from backend.database import cancel_job

    job = cancel_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"job_id": job.id, "status": job.status}


@app.post("/jobs/{job_id}/pause")
async def pause_job_endpoint(job_id: int):
    from backend.database import get_job, update_job

    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status not in (JobStatus.queued, JobStatus.running):
        raise HTTPException(409, f"Cannot pause a job in state {job.status.value}")
    job = update_job(job_id, status=JobStatus.paused)
    return {"job_id": job_id, "status": job.status}


@app.post("/jobs/{job_id}/resume")
async def resume_job_endpoint(job_id: int):
    from backend.database import get_job, update_job

    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != JobStatus.paused:
        raise HTTPException(409, f"Cannot resume a job in state {job.status.value}")
    status = JobStatus.queued if job.started_at is None else JobStatus.running
    job = update_job(job_id, status=status)
    return {"job_id": job_id, "status": job.status}


# ── Glossary endpoints ─────────────────────────────────────────────────────────

@app.get("/glossary")
async def list_glossary():
    rules = get_all_glossary_rules()
    return [
        {"id": r.id, "source_term": r.source_term, "target_term": r.target_term, "notes": r.notes}
        for r in rules
    ]


@app.get("/novels/{novel_id}/glossary")
async def list_novel_glossary(novel_id: int):
    if not get_novel(novel_id):
        raise HTTPException(404, "Novel not found")
    return {"novel_id": novel_id, "language": "ar", "terms": load_novel_glossary(novel_id)}


@app.get("/novels/{novel_id}/glossary/suggestions")
async def get_novel_glossary_suggestions(
    novel_id: int,
    limit: int = Query(default=50, ge=1, le=100),
):
    if not get_novel(novel_id):
        raise HTTPException(404, "Novel not found")
    existing = set(load_novel_glossary(novel_id))
    existing.update(rule.source_term for rule in get_all_glossary_rules())
    suggestions = suggest_glossary_terms(
        get_chapters(novel_id),
        existing_terms=existing,
        limit=limit,
    )
    return {
        "novel_id": novel_id,
        "language": "ar",
        "review_required": True,
        "apply_endpoint": f"/novels/{novel_id}/glossary",
        "items": suggestions,
        "total": len(suggestions),
    }


@app.put("/novels/{novel_id}/glossary")
async def upsert_novel_glossary(novel_id: int, req: AddGlossaryRequest):
    try:
        terms = set_novel_glossary_term(
            novel_id, req.source_term, req.target_term, language="ar"
        )
    except ValueError as exc:
        raise HTTPException(404 if "not found" in str(exc).lower() else 422, str(exc))
    return {"novel_id": novel_id, "language": "ar", "terms": terms}


@app.delete("/novels/{novel_id}/glossary/{source_term}")
async def remove_novel_glossary_term(novel_id: int, source_term: str):
    if not get_novel(novel_id):
        raise HTTPException(404, "Novel not found")
    if not delete_novel_glossary_term(novel_id, source_term, language="ar"):
        raise HTTPException(404, "Novel glossary term not found")
    return {"message": "Deleted"}


@app.post("/glossary", status_code=201)
async def create_glossary_rule(req: AddGlossaryRequest):
    source_term = (req.source_term or "").strip()
    target_term = (req.target_term or "").strip()
    if not source_term or not target_term:
        raise HTTPException(422, "Glossary source and target terms must not be empty")
    rule = add_glossary_rule(source_term, target_term, req.notes or "")
    return {"id": rule.id, "source_term": rule.source_term, "target_term": rule.target_term}


@app.delete("/glossary/{rule_id}")
async def remove_glossary_rule(rule_id: int):
    ok = delete_glossary_rule(rule_id)
    if not ok:
        raise HTTPException(404, "Rule not found")
    return {"message": "Deleted"}


# ── Sites endpoint ─────────────────────────────────────────────────────────────

@app.get("/sites")
async def list_sites():
    return AdapterRegistry.list_all()


@app.get("/sources")
async def list_sources():
    return AdapterRegistry.list_all()


@app.get("/sources/{site_id}/diagnosis")
async def source_diagnosis(site_id: str):
    adapter = AdapterRegistry._adapters.get(site_id)
    if not adapter:
        raise HTTPException(404, "Source not found")
    configured_url = getattr(adapter, "base_url", "") or ""
    if isinstance(configured_url, (list, tuple)):
        configured_url = configured_url[0] if configured_url else ""
    origin = urlparse(configured_url).netloc if isinstance(configured_url, str) else ""
    profile = get_pacer().get_profile(origin) if origin else None
    return {
        "site_id": site_id,
        "adapter": type(adapter).__name__,
        "template": type(adapter).__mro__[1].__name__,
        "version": getattr(adapter, "version", "1"),
        "tier": getattr(adapter, "tier", "legacy"),
        "selectors": {
            name: getattr(adapter, name)
            for name in (
                "novel_title_selectors",
                "novel_author_selectors",
                "novel_cover_selectors",
                "novel_description_selectors",
                "chapter_list_selectors",
                "chapter_body_selectors",
            )
            if hasattr(adapter, name)
        },
        "pacing": {
            "mean_interval": profile.mean_interval,
            "ok_count": profile.ok_count,
            "fail_count": profile.fail_count,
        } if profile else None,
        "health": get_source_health().reasons(origin) if origin else {},
    }


@app.get("/search")
def _search_adapters(
    domain: Optional[str],
    source_language: Optional[str],
    category: Optional[str],
):
    searchers = []
    enabled_adapters = get_preferences()["enabled_adapters"]
    for adapter in AdapterRegistry._adapters.values():
        if enabled_adapters.get(adapter.site_id, True) is False:
            continue
        is_native_arabic = bool(getattr(adapter, "is_native_arabic", False))
        adapter_language = str(getattr(adapter, "source_language", "")).casefold()
        if source_language == "en-ar" and (is_native_arabic or adapter_language != "english"):
            continue
        if source_language == "ar-ar" and not is_native_arabic:
            continue
        adapter_category = getattr(adapter, "category", "") or (
            "arabic" if is_native_arabic else "webnovel"
        )
        if category and category != "all" and str(adapter_category).casefold() != category.casefold():
            continue
        adapter_base_url = getattr(adapter, "base_url", "")
        if isinstance(adapter_base_url, (list, tuple)):
            adapter_base_url = adapter_base_url[0] if adapter_base_url else ""
        adapter_host = (
            urlparse(adapter_base_url).hostname or ""
            if isinstance(adapter_base_url, str)
            else ""
        )
        if domain and (
            domain.casefold() not in adapter.site_id.casefold()
            and domain.casefold() != adapter_host.casefold()
        ):
            continue
        search = getattr(adapter, "search", None)
        if not getattr(adapter, "can_search", False) or not callable(search):
            continue
        searchers.append((adapter, search))
    return searchers


async def search_sources_progressive(
    query: str,
    *,
    domain: Optional[str] = None,
    limit: int = 20,
    source_language: Optional[str] = None,
    category: Optional[str] = None,
    on_progress=None,
    on_results=None,
    cancel_event: Optional[threading.Event] = None,
    source_timeout: float = 20,
):
    searchers = _search_adapters(domain, source_language, category)
    enabled_adapters = get_preferences()["enabled_adapters"]
    if not searchers and not any(
        enabled and getattr(adapter, "can_search", False)
        and callable(getattr(adapter, "search", None))
        for adapter in AdapterRegistry._adapters.values()
        for enabled in (enabled_adapters.get(adapter.site_id, True) is not False,)
    ):
        raise HTTPException(501, "No registered source adapter supports search.")
    if not searchers:
        return {"query": query, "items": [], "total": 0, "failed": 0, "cancelled": False}

    semaphore = asyncio.Semaphore(5)
    total = len(searchers)
    completed = 0
    failed = 0
    results = []

    def progress(current: str = ""):
        if on_progress:
            on_progress({
                "completed": completed,
                "total": total,
                "current": current,
                "running": max(0, total - completed),
                "failed": failed,
                "result_count": len(results),
            })

    async def search_adapter(adapter, search):
        nonlocal completed, failed
        async with semaphore:
            if cancel_event and cancel_event.is_set():
                return
            progress(adapter.site_id)
            try:
                kwargs = {"limit": limit} if "limit" in inspect.signature(search).parameters else {}
                if inspect.iscoroutinefunction(search):
                    found = await asyncio.wait_for(search(query, **kwargs), timeout=source_timeout)
                else:
                    found = await asyncio.wait_for(
                        asyncio.to_thread(search, query, **kwargs),
                        timeout=source_timeout,
                    )
                    if inspect.isawaitable(found):
                        found = await asyncio.wait_for(found, timeout=source_timeout)
                batch = list(found or [])[:limit]
                results.extend(batch)
                if on_results and batch:
                    on_results(adapter.site_id, batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed += 1
                logger.warning("Source search failed for adapter %s: %s", adapter.site_id, exc)
            finally:
                completed += 1
                progress()

    tasks = [asyncio.create_task(search_adapter(adapter, search)) for adapter, search in searchers]
    pending = set(tasks)
    cancelled = False
    try:
        while pending:
            if cancel_event and cancel_event.is_set():
                cancelled = True
                for task in pending:
                    task.cancel()
                break
            done, pending = await asyncio.wait(
                pending, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if cancelled:
            # Mark sources that did not complete as no longer running.
            completed = total
            progress()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {
        "query": query,
        "items": results[:limit * max(1, total)],
        "total": len(results),
        "failed": failed,
        "cancelled": cancelled,
    }


@app.get("/search")
async def search_sources(
    query: str = Query(min_length=2, max_length=50),
    domain: Optional[str] = None,
    limit: int = Query(default=20, ge=1, le=50),
    source_language: Optional[str] = None,
    category: Optional[str] = None,
):
    return await search_sources_progressive(
        query,
        domain=domain,
        limit=limit,
        source_language=source_language,
        category=category,
    )


@app.get("/novels/{novel_id}/cover")
async def novel_cover(novel_id: int):
    novel = get_novel(novel_id)
    if not novel:
        raise HTTPException(404, "Novel not found")
    if not novel.cover_url:
        raise HTTPException(404, "Novel cover not found")
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            image, media_type = await _download_image(client, novel.cover_url)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise HTTPException(502, f"Could not fetch novel cover: {exc}") from exc
    return Response(
        content=image,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/chapters/{chapter_id}/image/{image_id}")
async def chapter_image(chapter_id: int, image_id: str):
    image = get_chapter_image(chapter_id, image_id)
    if not image:
        raise HTTPException(404, "Chapter image not found")
    path = Path(image.file_path).resolve()
    image_root = (get_config().paths.data_dir / "chapter-images").resolve()
    if not path.is_relative_to(image_root):
        raise HTTPException(403, "Chapter image path is outside the image store")
    if not path.is_file():
        raise HTTPException(404, "Chapter image file not found")
    return FileResponse(path=str(path), media_type=image.media_type)


@app.post("/novels/{novel_id}/artifacts", status_code=202)
async def create_artifact(novel_id: int, req: ArtifactRequest):
    if not get_novel(novel_id):
        raise HTTPException(404, "Novel not found")
    try:
        from backend.binder import available_formats
        if req.format not in available_formats():
            raise HTTPException(
                400,
                f"Format '{req.format}' is unavailable; choose from {', '.join(available_formats())}",
            )
        job = scheduler.enqueue(
            novel_id,
            JobType.build_epub,
            options={
                "format": req.format,
                "volume": req.volume,
                "language": req.language,
            },
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"job_id": job.id, "message": "Artifact job queued"}


@app.get("/artifacts/{job_id}/download")
async def download_artifact(job_id: int):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Artifact job not found")
    if job.status != JobStatus.completed or not job.output_path:
        raise HTTPException(409, "Artifact is not ready for download")
    output_root = output_path()
    artifact_path = Path(job.output_path).resolve()
    if not artifact_path.is_relative_to(output_root):
        raise HTTPException(403, "Artifact path is outside the output directory")
    if not artifact_path.is_file():
        raise HTTPException(404, "Artifact file not found")
    return FileResponse(path=str(artifact_path), filename=artifact_path.name)


@app.get("/settings")
async def public_settings():
    return get_config().to_public_dict()


@app.get("/diagnostics/logs/export")
async def export_diagnostic_logs():
    if not get_config().diagnostics.allow_export:
        raise HTTPException(403, "Export disabled in settings")
    payload = diagnostics_archive()
    return StreamingResponse(
        iter((payload,)),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="NovelBridge-diagnostics.zip"',
            "Content-Length": str(len(payload)),
        },
    )


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


if __name__ == "__main__":
    import uvicorn
    port = get_config().api_port
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port, reload=True)
