"""
NovelBridge — FastAPI backend.
Exposes the full REST API for the Flutter phone client.
The PyQt6 GUI calls backend functions directly (in-process).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.adapters.base import AdapterRegistry
from backend.adapters.galaxynovels import GalaxyNovelsAdapter
from backend.adapters.novelfire import NovelFireAdapter
from backend.adapters.novelphoenix import NovelPhoenixAdapter
from backend.adapters.wuxiaspot import WuxiaSpotAdapter
from backend.database import (
    add_glossary_rule, create_novel, delete_glossary_rule,
    get_all_glossary_rules, get_all_novels,
    get_chapters, get_job, get_novel, init_db,
    update_novel,
)
from backend.models import ChapterStatus, JobStatus, NovelStatus
from backend.pipeline import (
    run_epub_job, run_scrape_job, run_translation_job,
)

# ── App init ──────────────────────────────────────────────────────────────────

app = FastAPI(
    title="NovelBridge API",
    description="Web novel scraper + Arabic translator + EPUB generator",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    init_db()
    # Register site adapters
    AdapterRegistry.register(NovelFireAdapter())
    AdapterRegistry.register(NovelPhoenixAdapter())
    AdapterRegistry.register(GalaxyNovelsAdapter())
    AdapterRegistry.register(WuxiaSpotAdapter())


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


@app.post("/novels", status_code=201)
async def add_novel(req: AddNovelRequest, background_tasks: BackgroundTasks):
    url = req.source_url.strip()
    adapter = AdapterRegistry.find(url)
    if not adapter:
        raise HTTPException(status_code=422, detail=f"No adapter found for URL: {url}")

    novel = create_novel(source_url=url, source_site=adapter.site_id)
    background_tasks.add_task(_bg_scrape, novel.id)
    return {"novel_id": novel.id, "message": "Scrape job started"}


async def _bg_scrape(novel_id: int):
    try:
        await run_scrape_job(novel_id)
    except Exception as e:
        pass  # Logged inside pipeline


@app.get("/novels/{novel_id}")
async def get_novel_detail(novel_id: int):
    novel = get_novel(novel_id)
    if not novel:
        raise HTTPException(404, "Novel not found")
    chapters = get_chapters(novel_id)
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
                "status": c.status,
                "has_translation": bool(c.translated_text),
            }
            for c in chapters
        ],
    }


@app.post("/novels/{novel_id}/translate")
async def translate_novel(novel_id: int, req: TranslateRequest, background_tasks: BackgroundTasks):
    novel = get_novel(novel_id)
    if not novel:
        raise HTTPException(404, "Novel not found")
    background_tasks.add_task(_bg_translate, novel_id, req.chapter_ids, req.provider, req.model)
    return {"message": "Translation job started"}


async def _bg_translate(novel_id: int, chapter_ids: Optional[List[int]], provider: Optional[str] = None, model: Optional[str] = None):
    try:
        await run_translation_job(novel_id, chapter_ids, provider_name=provider, model_name=model)
    except Exception:
        pass



@app.get("/novels/{novel_id}/epub")
async def download_epub(novel_id: int):
    novel = get_novel(novel_id)
    if not novel:
        raise HTTPException(404, "Novel not found")
    try:
        _, epub_path = await run_epub_job(novel_id)
        return FileResponse(
            path=str(epub_path),
            media_type="application/epub+zip",
            filename=epub_path.name,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


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
        "created_at": job.created_at.isoformat(),
    }


# ── Glossary endpoints ─────────────────────────────────────────────────────────

@app.get("/glossary")
async def list_glossary():
    rules = get_all_glossary_rules()
    return [
        {"id": r.id, "source_term": r.source_term, "target_term": r.target_term, "notes": r.notes}
        for r in rules
    ]


@app.post("/glossary", status_code=201)
async def create_glossary_rule(req: AddGlossaryRequest):
    rule = add_glossary_rule(req.source_term, req.target_term, req.notes or "")
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


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port, reload=True)
