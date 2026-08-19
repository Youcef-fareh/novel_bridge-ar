"""
NovelBridge — SQLite database engine, session helpers, and CRUD operations.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, List, Optional

from dotenv import load_dotenv
from sqlmodel import Session, SQLModel, create_engine, select

from backend.models import (
    Chapter, ChapterStatus, GlossaryRule,
    Job, JobStatus, JobType,
    Novel, NovelStatus,
)

load_dotenv()

_DB_PATH = Path(os.getenv("DB_PATH", "data/novelbridge.db"))
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_engine = create_engine(
    f"sqlite:///{_DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False,
)


def init_db() -> None:
    """Create all tables and seed/sync glossary from config file."""
    SQLModel.metadata.create_all(_engine)
    recover_interrupted_translations()
    sync_glossary_from_config(overwrite=False)


def sync_glossary_from_config(overwrite: bool = False) -> int:
    """
    Sync glossary rules from config/glossary.json into the database.
    Inserts missing rules, and if overwrite=True, updates existing ones.
    Returns the total number of rules processed.
    """
    config_path = Path("config/glossary.json")
    if not config_path.exists():
        return 0
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return 0

    rules_data = data.get("rules", [])
    if not rules_data:
        return 0

    synced_count = 0
    with Session(_engine) as session:
        existing_rules = {r.source_term.strip().lower(): r for r in session.exec(select(GlossaryRule)).all()}
        for rule in rules_data:
            st = rule.get("source_term", "").strip()
            tt = rule.get("target_term", "").strip()
            notes = rule.get("notes", "")
            if not st or not tt:
                continue
            key = st.lower()
            if key in existing_rules:
                if overwrite:
                    existing_rules[key].source_term = st
                    existing_rules[key].target_term = tt
                    existing_rules[key].notes = notes
                    session.add(existing_rules[key])
                    synced_count += 1
            else:
                new_rule = GlossaryRule(
                    source_term=st,
                    target_term=tt,
                    notes=notes,
                )
                session.add(new_rule)
                existing_rules[key] = new_rule
                synced_count += 1
        session.commit()
    return synced_count


@contextmanager
def get_session() -> Generator[Session, None, None]:
    with Session(_engine) as session:
        yield session


# ── Novel CRUD ─────────────────────────────────────────────────────────────────

def create_novel(source_url: str, source_site: str, title: str = "Unknown") -> Novel:
    with Session(_engine) as session:
        novel = Novel(title=title, source_url=source_url, source_site=source_site)
        session.add(novel)
        session.commit()
        session.refresh(novel)
        return novel


def get_novel(novel_id: int) -> Optional[Novel]:
    with Session(_engine) as session:
        return session.get(Novel, novel_id)


def get_all_novels() -> List[Novel]:
    with Session(_engine) as session:
        return list(session.exec(select(Novel)).all())


def update_novel(novel_id: int, **kwargs) -> Optional[Novel]:
    with Session(_engine) as session:
        novel = session.get(Novel, novel_id)
        if not novel:
            return None
        for k, v in kwargs.items():
            setattr(novel, k, v)
        novel.updated_at = datetime.utcnow()
        session.add(novel)
        session.commit()
        session.refresh(novel)
        return novel


def delete_novel(novel_id: int) -> bool:
    with Session(_engine) as session:
        novel = session.get(Novel, novel_id)
        if not novel:
            return False
        # Delete related chapters and jobs
        for ch in session.exec(select(Chapter).where(Chapter.novel_id == novel_id)).all():
            session.delete(ch)
        for job in session.exec(select(Job).where(Job.novel_id == novel_id)).all():
            session.delete(job)
        session.delete(novel)
        session.commit()
        return True


# ── Chapter CRUD ───────────────────────────────────────────────────────────────

def upsert_chapter(novel_id: int, index: int, title: str, source_url: str) -> Chapter:
    with Session(_engine) as session:
        existing = session.exec(
            select(Chapter).where(Chapter.novel_id == novel_id, Chapter.index == index)
        ).first()
        if existing:
            return existing
        chapter = Chapter(novel_id=novel_id, index=index, title=title, source_url=source_url)
        session.add(chapter)
        session.commit()
        session.refresh(chapter)
        return chapter


def get_chapters(novel_id: int) -> List[Chapter]:
    with Session(_engine) as session:
        return list(
            session.exec(select(Chapter).where(Chapter.novel_id == novel_id).order_by(Chapter.index)).all()
        )


def update_chapter(chapter_id: int, **kwargs) -> Optional[Chapter]:
    with Session(_engine) as session:
        chapter = session.get(Chapter, chapter_id)
        if not chapter:
            return None
        for k, v in kwargs.items():
            setattr(chapter, k, v)
        chapter.updated_at = datetime.utcnow()
        session.add(chapter)
        session.commit()
        session.refresh(chapter)
        return chapter


def recover_interrupted_translations() -> int:
    """Make chapters left in progress by a stopped process retryable."""
    with Session(_engine) as session:
        chapters = list(session.exec(
            select(Chapter).where(Chapter.status == ChapterStatus.translating)
        ).all())
        for chapter in chapters:
            chapter.status = ChapterStatus.scraped
            chapter.updated_at = datetime.utcnow()
            session.add(chapter)
        session.commit()
        return len(chapters)


def get_pending_translation_chapters(novel_id: int) -> List[Chapter]:
    with Session(_engine) as session:
        return list(session.exec(
            select(Chapter).where(
                Chapter.novel_id == novel_id,
                Chapter.status.in_([
                    ChapterStatus.scraped,
                    ChapterStatus.translating,
                    ChapterStatus.failed,
                ])
            ).order_by(Chapter.index)
        ).all())


# ── Glossary CRUD ──────────────────────────────────────────────────────────────

def get_all_glossary_rules() -> List[GlossaryRule]:
    with Session(_engine) as session:
        return list(session.exec(select(GlossaryRule)).all())


def add_glossary_rule(source_term: str, target_term: str, notes: str = "") -> GlossaryRule:
    with Session(_engine) as session:
        rule = GlossaryRule(source_term=source_term, target_term=target_term, notes=notes)
        session.add(rule)
        session.commit()
        session.refresh(rule)
        return rule


def delete_glossary_rule(rule_id: int) -> bool:
    with Session(_engine) as session:
        rule = session.get(GlossaryRule, rule_id)
        if not rule:
            return False
        session.delete(rule)
        session.commit()
        return True


# ── Job CRUD ───────────────────────────────────────────────────────────────────

def create_job(novel_id: int, job_type: JobType, total_items: int = 0) -> Job:
    with Session(_engine) as session:
        job = Job(novel_id=novel_id, type=job_type, total_items=total_items)
        session.add(job)
        session.commit()
        session.refresh(job)
        return job


def get_job(job_id: int) -> Optional[Job]:
    with Session(_engine) as session:
        return session.get(Job, job_id)


def update_job(job_id: int, **kwargs) -> Optional[Job]:
    with Session(_engine) as session:
        job = session.get(Job, job_id)
        if not job:
            return None
        for k, v in kwargs.items():
            setattr(job, k, v)
        job.updated_at = datetime.utcnow()
        session.add(job)
        session.commit()
        session.refresh(job)
        return job


def get_active_jobs(novel_id: int) -> List[Job]:
    with Session(_engine) as session:
        return list(session.exec(
            select(Job).where(
                Job.novel_id == novel_id,
                Job.status.in_([JobStatus.queued, JobStatus.running])
            )
        ).all())
