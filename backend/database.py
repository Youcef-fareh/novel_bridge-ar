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
    """Create all tables and seed glossary from config file."""
    SQLModel.metadata.create_all(_engine)
    _seed_glossary_from_config()


def _seed_glossary_from_config() -> None:
    """Load default glossary rules from config/glossary.json if DB is empty."""
    config_path = Path("config/glossary.json")
    if not config_path.exists():
        return
    with Session(_engine) as session:
        existing = session.exec(select(GlossaryRule)).first()
        if existing:
            return  # already seeded
        data = json.loads(config_path.read_text(encoding="utf-8"))
        for rule in data.get("rules", []):
            session.add(GlossaryRule(
                source_term=rule["source_term"],
                target_term=rule["target_term"],
                notes=rule.get("notes"),
            ))
        session.commit()


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


def get_pending_translation_chapters(novel_id: int) -> List[Chapter]:
    with Session(_engine) as session:
        return list(session.exec(
            select(Chapter).where(
                Chapter.novel_id == novel_id,
                Chapter.status.in_([ChapterStatus.scraped, ChapterStatus.failed])
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
