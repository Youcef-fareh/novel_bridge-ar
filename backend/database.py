"""
NovelBridge — SQLite database engine, session helpers, and CRUD operations.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator, List, Optional
from urllib.parse import urlsplit

from dotenv import load_dotenv
from sqlalchemy import delete as sql_delete, event
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy import func

from backend.models import (
    Chapter, ChapterImage, ChapterStatus, GlossaryRule, NovelGlossary, Volume,
    Job, JobStatus, JobType,
    Novel, NovelStatus,
)
from backend.config import get_config

load_dotenv()

_DB_PATH = get_config().paths.db
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_engine = create_engine(
    f"sqlite:///{_DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
    echo=False,
)


@event.listens_for(_engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _migrate_schema() -> None:
    """Add newly introduced chapter and scheduler columns to existing databases."""
    migrations = {
        "chapter": {
            "crawler_version": "TEXT",
            "crawler_tier": "TEXT DEFAULT 'legacy'",
            "empty_attempts": "INTEGER NOT NULL DEFAULT 0",
            "content_hash": "TEXT",
            "volume_serial": "INTEGER",
            "volume_title": "TEXT",
            "volume_explicit": "INTEGER NOT NULL DEFAULT 0",
            "translated_hash": "TEXT",
        },
        "job": {
            "parent_job_id": "INTEGER",
            "domain": "TEXT",
            "priority": "INTEGER NOT NULL DEFAULT 0",
            "failed_items": "INTEGER NOT NULL DEFAULT 0",
            "options_json": "TEXT NOT NULL DEFAULT '{}'",
            "current_message": "TEXT",
            "output_path": "TEXT",
            "worker_id": "TEXT",
            "heartbeat_at": "DATETIME",
            "started_at": "DATETIME",
            "finished_at": "DATETIME",
            "lease_expires_at": "DATETIME",
        },
    }
    with _engine.begin() as conn:
        for table_name, columns in migrations.items():
            existing = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
            }
            if not existing:
                continue
            for column_name, column_type in columns.items():
                if column_name not in existing:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                    )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_job_scheduler ON job(status, priority, created_at)"
        )
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_job_domain_status ON job(domain, status)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_job_parent_job_id ON job(parent_job_id)")


def init_db() -> None:
    """Create all tables, migrate columns, and seed/sync glossary from config file."""
    SQLModel.metadata.create_all(_engine)
    _migrate_schema()
    recover_interrupted_translations()
    recover_interrupted_jobs()
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


def get_novels_page(offset: int, limit: int) -> tuple[List[Novel], int]:
    with Session(_engine) as session:
        total = session.exec(select(func.count()).select_from(Novel)).one()
        novels = session.exec(
            select(Novel)
            .order_by(Novel.created_at.desc(), Novel.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        return list(novels), int(total)


def update_novel(novel_id: int, **kwargs) -> Optional[Novel]:
    with Session(_engine) as session:
        novel = session.get(Novel, novel_id)
        if not novel:
            return None
        for k, v in kwargs.items():
            setattr(novel, k, v)
        novel.updated_at = datetime.now(timezone.utc)
        session.add(novel)
        session.commit()
        session.refresh(novel)
        return novel


def delete_novel(novel_id: int) -> bool:
    image_paths: list[Path] = []
    with Session(_engine) as session:
        if session.get(Novel, novel_id) is None:
            return False

        chapter_ids = select(Chapter.id).where(Chapter.novel_id == novel_id)
        image_paths = [
            Path(path)
            for path in session.exec(
                select(ChapterImage.file_path)
                .join(Chapter, Chapter.id == ChapterImage.chapter_id)
                .where(Chapter.novel_id == novel_id)
            ).all()
        ]

        # Issue deletes in foreign-key order so SQLite never sees an orphaned child.
        session.execute(
            sql_delete(ChapterImage).where(ChapterImage.chapter_id.in_(chapter_ids))
        )
        session.execute(sql_delete(Chapter).where(Chapter.novel_id == novel_id))
        session.execute(sql_delete(Job).where(Job.novel_id == novel_id))
        session.execute(sql_delete(NovelGlossary).where(NovelGlossary.novel_id == novel_id))
        session.execute(sql_delete(Volume).where(Volume.novel_id == novel_id))
        session.execute(sql_delete(Novel).where(Novel.id == novel_id))
        session.commit()
    image_root = (get_config().paths.data_dir / "chapter-images").resolve()
    for image_path in image_paths:
        if image_path.resolve().is_relative_to(image_root):
            image_path.unlink(missing_ok=True)
    return True


# ── Chapter CRUD ───────────────────────────────────────────────────────────────

def upsert_chapter(
    novel_id: int,
    index: int,
    title: str,
    source_url: str,
    volume_serial: Optional[int] = None,
    volume_title: Optional[str] = None,
) -> Chapter:
    stale_image_paths: list[Path] = []
    with Session(_engine) as session:
        existing = session.exec(
            select(Chapter).where(Chapter.novel_id == novel_id, Chapter.index == index)
        ).first()
        if existing:
            changed_source = existing.source_url != source_url
            existing.title = title
            existing.source_url = source_url
            if volume_serial is not None:
                existing.volume_serial = volume_serial
                existing.volume_explicit = True
                existing.volume_title = volume_title
            elif changed_source:
                existing.volume_serial = None
                existing.volume_explicit = False
                existing.volume_title = None
            if changed_source:
                stored_images = session.exec(
                    select(ChapterImage).where(ChapterImage.chapter_id == existing.id)
                ).all()
                stale_image_paths.extend(Path(image.file_path) for image in stored_images)
                for image in stored_images:
                    session.delete(image)
                existing.raw_text = None
                existing.content_hash = None
                existing.translated_text = None
                existing.translated_hash = None
                existing.empty_attempts = 0
                existing.crawler_version = None
                existing.crawler_tier = "legacy"
                existing.status = ChapterStatus.pending
            if not existing.volume_explicit:
                existing.volume_serial = None
            session.add(existing)
            session.commit()
            session.refresh(existing)
            result = existing
        else:
            chapter = Chapter(
                novel_id=novel_id,
                index=index,
                title=title,
                source_url=source_url,
                volume_serial=volume_serial,
                volume_title=volume_title,
                volume_explicit=volume_serial is not None,
            )
            session.add(chapter)
            session.commit()
            session.refresh(chapter)
            result = chapter
    image_root = (get_config().paths.data_dir / "chapter-images").resolve()
    for image_path in stale_image_paths:
        if image_path.resolve().is_relative_to(image_root):
            image_path.unlink(missing_ok=True)
    return result


def get_chapters(novel_id: int) -> List[Chapter]:
    with Session(_engine) as session:
        return list(
            session.exec(select(Chapter).where(Chapter.novel_id == novel_id).order_by(Chapter.index)).all()
        )


def repair_kolnovel_pdf_chapter_duplicates(
    novel_id: int, chapter_urls: List[str]
) -> int:
    """Collapse the known Kolnovel TOC pattern of a chapter plus its /pdf link."""
    def chapter_key(url: str) -> tuple[str, str, str]:
        parsed = urlsplit(url)
        path = parsed.path.rstrip("/")
        if path.lower().endswith("/pdf"):
            path = path[:-4].rstrip("/")
        return (
            parsed.hostname.lower() if parsed.hostname else "",
            path.casefold(),
            parsed.query,
        )

    ordered_keys = [chapter_key(url) for url in chapter_urls]
    if not ordered_keys or len(set(ordered_keys)) != len(ordered_keys):
        return 0

    with Session(_engine) as session:
        chapters = list(session.exec(
            select(Chapter)
            .where(Chapter.novel_id == novel_id)
            .order_by(Chapter.index)
        ).all())
        grouped: dict[tuple[str, str, str], list[Chapter]] = {}
        for chapter in chapters:
            grouped.setdefault(chapter_key(chapter.source_url), []).append(chapter)

        if set(grouped) != set(ordered_keys) or len(chapters) != len(ordered_keys) * 2:
            return 0

        retained: dict[tuple[str, str, str], Chapter] = {}
        pdf_chapters: list[Chapter] = []
        for key, copies in grouped.items():
            pdf_copies = [
                chapter
                for chapter in copies
                if urlsplit(chapter.source_url).path.rstrip("/").lower().endswith("/pdf")
            ]
            real_copies = [chapter for chapter in copies if chapter not in pdf_copies]
            if len(copies) != 2 or len(pdf_copies) != 1 or len(real_copies) != 1:
                return 0
            pdf_chapter = pdf_copies[0]
            if (
                (pdf_chapter.raw_text and pdf_chapter.raw_text.strip())
                or (pdf_chapter.translated_text and pdf_chapter.translated_text.strip())
                or pdf_chapter.status not in (ChapterStatus.pending, ChapterStatus.failed)
            ):
                return 0
            pdf_chapters.append(pdf_chapter)
            retained[key] = real_copies[0]

        pdf_ids = [chapter.id for chapter in pdf_chapters]
        if session.exec(
            select(ChapterImage).where(ChapterImage.chapter_id.in_(pdf_ids))
        ).first():
            return 0

        for index, key in enumerate(ordered_keys):
            chapter = retained[key]
            chapter.index = -(index + 1)
            session.add(chapter)
        for chapter in pdf_chapters:
            session.delete(chapter)
        session.flush()
        for index, key in enumerate(ordered_keys):
            chapter = retained[key]
            chapter.index = index
            session.add(chapter)
        session.commit()
        return len(pdf_chapters)


def refresh_volumes(novel_id: int, chapters_per_volume: int = 100) -> List[Volume]:
    if chapters_per_volume < 1:
        raise ValueError("chapters_per_volume must be at least 1")
    chapters = get_chapters(novel_id)
    counts: dict[int, int] = {}
    titles: dict[int, str] = {}
    for chapter in chapters:
        serial = (
            chapter.volume_serial
            if chapter.volume_explicit and chapter.volume_serial is not None
            else (chapter.index // chapters_per_volume) + 1
        )
        counts[serial] = counts.get(serial, 0) + 1
        if chapter.volume_explicit and chapter.volume_title:
            titles.setdefault(serial, chapter.volume_title)
    with Session(_engine) as session:
        for chapter in chapters:
            serial = (
                chapter.volume_serial
                if chapter.volume_explicit and chapter.volume_serial is not None
                else (chapter.index // chapters_per_volume) + 1
            )
            if chapter.volume_serial != serial:
                stored_chapter = session.get(Chapter, chapter.id)
                if stored_chapter:
                    stored_chapter.volume_serial = serial
        existing = {
            volume.serial: volume
            for volume in session.exec(select(Volume).where(Volume.novel_id == novel_id)).all()
        }
        for serial, count in counts.items():
            volume = existing.pop(serial, None)
            if volume:
                volume.chapter_count = count
                volume.title = titles.get(serial, volume.title or f"Volume {serial}")
            else:
                volume = Volume(
                    novel_id=novel_id,
                    serial=serial,
                    title=titles.get(serial, f"Volume {serial}"),
                    chapter_count=count,
                )
            session.add(volume)
        for obsolete in existing.values():
            session.delete(obsolete)
        session.commit()
        return list(
            session.exec(
                select(Volume).where(Volume.novel_id == novel_id).order_by(Volume.serial)
            ).all()
        )


def replace_chapter_images(chapter_id: int, images: list[dict]) -> list[ChapterImage]:
    with Session(_engine) as session:
        old_images = list(session.exec(
            select(ChapterImage).where(ChapterImage.chapter_id == chapter_id)
        ).all())
        for image in old_images:
            session.delete(image)
        stored = [
            ChapterImage(
                chapter_id=chapter_id,
                image_id=image["image_id"],
                file_path=image["file_path"],
                media_type=image["media_type"],
                alt_text=image.get("alt_text"),
                size_bytes=image["size_bytes"],
            )
            for image in images
        ]
        session.add_all(stored)
        session.commit()
        for image in stored:
            session.refresh(image)
        return stored


def get_chapter_images(chapter_id: int) -> List[ChapterImage]:
    with Session(_engine) as session:
        return list(session.exec(
            select(ChapterImage)
            .where(ChapterImage.chapter_id == chapter_id)
            .order_by(ChapterImage.id)
        ).all())


def get_novel_chapter_images(novel_id: int) -> dict[int, List[ChapterImage]]:
    with Session(_engine) as session:
        images = session.exec(
            select(ChapterImage)
            .join(Chapter, ChapterImage.chapter_id == Chapter.id)
            .where(Chapter.novel_id == novel_id)
            .order_by(ChapterImage.chapter_id, ChapterImage.id)
        ).all()
        grouped: dict[int, List[ChapterImage]] = {}
        for image in images:
            grouped.setdefault(image.chapter_id, []).append(image)
        return grouped


def get_chapter_image(chapter_id: int, image_id: str) -> Optional[ChapterImage]:
    with Session(_engine) as session:
        return session.exec(
            select(ChapterImage).where(
                ChapterImage.chapter_id == chapter_id,
                ChapterImage.image_id == image_id,
            )
        ).first()


def get_volumes(novel_id: int) -> List[Volume]:
    with Session(_engine) as session:
        return list(
            session.exec(
                select(Volume).where(Volume.novel_id == novel_id).order_by(Volume.serial)
            ).all()
        )


def update_chapter(chapter_id: int, **kwargs) -> Optional[Chapter]:
    with Session(_engine) as session:
        chapter = session.get(Chapter, chapter_id)
        if not chapter:
            return None
        for k, v in kwargs.items():
            setattr(chapter, k, v)
        chapter.updated_at = datetime.now(timezone.utc)
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
            chapter.updated_at = datetime.now(timezone.utc)
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


def get_novel_glossary(novel_id: int, language: str = "ar") -> dict[str, str]:
    with Session(_engine) as session:
        glossary = session.exec(
            select(NovelGlossary).where(
                NovelGlossary.novel_id == novel_id,
                NovelGlossary.language == language,
            )
        ).first()
        if not glossary:
            return {}
        terms = json.loads(glossary.terms_json)
        if not isinstance(terms, dict) or not all(
            isinstance(source, str) and isinstance(target, str)
            for source, target in terms.items()
        ):
            raise ValueError(f"Invalid glossary data for novel {novel_id} ({language})")
        return terms


def set_novel_glossary_term(
    novel_id: int, source_term: str, target_term: str, language: str = "ar"
) -> dict[str, str]:
    source_term = source_term.strip()
    target_term = target_term.strip()
    if not source_term or not target_term:
        raise ValueError("Glossary source and target terms must not be empty")
    with Session(_engine) as session:
        novel = session.get(Novel, novel_id)
        if not novel:
            raise ValueError(f"Novel {novel_id} not found")
        glossary = session.exec(
            select(NovelGlossary).where(
                NovelGlossary.novel_id == novel_id,
                NovelGlossary.language == language,
            )
        ).first()
        terms = json.loads(glossary.terms_json) if glossary else {}
        terms[source_term] = target_term
        if glossary:
            glossary.terms_json = json.dumps(terms, ensure_ascii=False)
        else:
            glossary = NovelGlossary(
                novel_id=novel_id,
                language=language,
                terms_json=json.dumps(terms, ensure_ascii=False),
            )
        session.add(glossary)
        session.commit()
        return terms


def delete_novel_glossary_term(
    novel_id: int, source_term: str, language: str = "ar"
) -> bool:
    with Session(_engine) as session:
        glossary = session.exec(
            select(NovelGlossary).where(
                NovelGlossary.novel_id == novel_id,
                NovelGlossary.language == language,
            )
        ).first()
        if not glossary:
            return False
        terms = json.loads(glossary.terms_json)
        if source_term not in terms:
            return False
        del terms[source_term]
        if terms:
            glossary.terms_json = json.dumps(terms, ensure_ascii=False)
        else:
            session.delete(glossary)
        session.commit()
        return True


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

def create_job(
    novel_id: int,
    job_type: JobType,
    total_items: int = 0,
    *,
    domain: Optional[str] = None,
    parent_job_id: Optional[int] = None,
    priority: int = 0,
    options: Optional[dict] = None,
) -> Job:
    with Session(_engine) as session:
        job = Job(
            novel_id=novel_id,
            type=job_type,
            total_items=total_items,
            domain=domain,
            parent_job_id=parent_job_id,
            priority=priority,
            options_json=json.dumps(options or {}),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job


def enqueue_job(
    novel_id: int,
    job_type: JobType,
    *,
    options: Optional[dict] = None,
    parent_job_id: Optional[int] = None,
    priority: int = 0,
) -> Job:
    novel = get_novel(novel_id)
    if not novel:
        raise ValueError(f"Novel {novel_id} not found.")
    from urllib.parse import urlparse

    domain = (urlparse(novel.source_url).hostname or novel.source_site).lower().rstrip(".")
    return create_job(
        novel_id,
        job_type,
        domain=domain,
        parent_job_id=parent_job_id,
        priority=priority,
        options=options,
    )


def get_job(job_id: int) -> Optional[Job]:
    with Session(_engine) as session:
        return session.get(Job, job_id)


def list_jobs(
    offset: int = 0,
    limit: int = 50,
    status: Optional[JobStatus] = None,
    job_type: Optional[JobType] = None,
) -> tuple[List[Job], int]:
    from sqlalchemy import func

    statement = select(Job)
    count_statement = select(func.count()).select_from(Job)
    if status is not None:
        statement = statement.where(Job.status == status)
        count_statement = count_statement.where(Job.status == status)
    if job_type is not None:
        statement = statement.where(Job.type == job_type)
        count_statement = count_statement.where(Job.type == job_type)
    with Session(_engine) as session:
        total = session.exec(count_statement).one()
        items = list(
            session.exec(
                statement.order_by(Job.created_at.desc(), Job.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
        )
        return items, total


def replay_job(job_id: int) -> Optional[Job]:
    previous = get_job(job_id)
    if not previous:
        return None
    options = json.loads(previous.options_json or "{}")
    if not isinstance(options, dict):
        raise ValueError(f"Job {job_id} options must be a JSON object")
    return enqueue_job(
        previous.novel_id,
        previous.type,
        options=options,
        parent_job_id=previous.id,
        priority=previous.priority,
    )


def update_job(job_id: int, **kwargs) -> Optional[Job]:
    with Session(_engine) as session:
        job = session.get(Job, job_id)
        if not job:
            return None
        for k, v in kwargs.items():
            setattr(job, k, v)
        now = datetime.now(timezone.utc)
        job.updated_at = now
        if kwargs.get("status") == JobStatus.running:
            job.started_at = job.started_at or now
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=60)
        elif kwargs.get("status") in (
            JobStatus.completed,
            JobStatus.failed,
            JobStatus.cancelled,
        ):
            job.finished_at = now
            job.lease_expires_at = None
            job.worker_id = None
        session.add(job)
        session.commit()
        session.refresh(job)
        return job


def claim_next_job(
    worker_id: str,
    max_per_domain: int = 2,
    lease_seconds: int = 60,
) -> Optional[Job]:
    """Atomically claim the fairest queued job that fits novel/domain limits."""
    now = datetime.now(timezone.utc)
    lease_expiry = now + timedelta(seconds=lease_seconds)
    with _engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            candidates = conn.exec_driver_sql(
                """
                SELECT j.id, j.novel_id, j.domain
                FROM job AS j
                WHERE j.status = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM job AS active
                      WHERE active.novel_id = j.novel_id
                        AND active.status IN (?, ?)
                  )
                ORDER BY j.priority DESC, j.created_at ASC, j.id ASC
                """,
                (JobStatus.queued.value, JobStatus.running.value, JobStatus.paused.value),
            ).mappings().all()

            selected_id = None
            for candidate in candidates:
                domain = candidate["domain"]
                if domain:
                    active_count = conn.exec_driver_sql(
                        "SELECT COUNT(*) FROM job WHERE domain = ? AND status IN (?, ?)",
                        (domain, JobStatus.running.value, JobStatus.paused.value),
                    ).scalar_one()
                    if active_count >= max_per_domain:
                        continue
                selected_id = candidate["id"]
                break

            if selected_id is None:
                conn.commit()
                return None

            result = conn.exec_driver_sql(
                """
                UPDATE job
                SET status = ?, worker_id = ?, started_at = COALESCE(started_at, ?),
                    heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    JobStatus.running.value,
                    worker_id,
                    now.isoformat(),
                    now.isoformat(),
                    lease_expiry.isoformat(),
                    now.isoformat(),
                    selected_id,
                    JobStatus.queued.value,
                ),
            )
            conn.commit()
            if result.rowcount != 1:
                return None
        except Exception:
            conn.rollback()
            raise
    return get_job(selected_id)


def heartbeat_job(job_id: int, worker_id: Optional[str], lease_seconds: int = 60) -> bool:
    now = datetime.now(timezone.utc)
    with _engine.begin() as conn:
        result = conn.exec_driver_sql(
            """
            UPDATE job
            SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
            WHERE id = ? AND status IN (?, ?)
              AND (? IS NULL OR worker_id = ?)
            """,
            (
                now.isoformat(),
                (now + timedelta(seconds=lease_seconds)).isoformat(),
                now.isoformat(),
                job_id,
                JobStatus.running.value,
                JobStatus.paused.value,
                worker_id,
                worker_id,
            ),
        )
        return result.rowcount == 1


def cancel_job(job_id: int) -> Optional[Job]:
    job = get_job(job_id)
    if not job or job.status in (
        JobStatus.completed,
        JobStatus.failed,
        JobStatus.cancelled,
    ):
        return job
    return update_job(job_id, status=JobStatus.cancelled, current_message="Cancelled")


def recover_interrupted_jobs(lease_grace_seconds: int = 0) -> int:
    """Requeue jobs whose worker lease expired, preserving completed chapter work."""
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(seconds=max(lease_grace_seconds, 0))
    with _engine.begin() as conn:
        result = conn.exec_driver_sql(
            """
            UPDATE job
            SET status = ?, worker_id = NULL, lease_expires_at = NULL,
                heartbeat_at = NULL, current_message = 'Recovered after worker interruption',
                updated_at = ?
            WHERE status = ?
              AND (
                  lease_expires_at IS NULL
                  OR julianday(lease_expires_at) <= julianday(?)
              )
            """,
            (
                JobStatus.queued.value,
                now.isoformat(),
                JobStatus.running.value,
                stale_before.isoformat(),
            ),
        )
        return result.rowcount


def get_jobs_for_scrub(retention_days: int = 90) -> tuple[int, int]:
    """Delete old terminal jobs and mark jobs exceeding the maximum runtime failed."""
    now = datetime.now(timezone.utc)
    finished_before = (now - timedelta(days=retention_days)).isoformat()
    long_running_before = (now - timedelta(hours=16)).isoformat()
    with _engine.begin() as conn:
        cancelled = conn.exec_driver_sql(
            """
            UPDATE job
            SET status = ?, error_message = 'Job exceeded the 16-hour runtime limit',
                current_message = 'Stopped by scheduler scrubber', finished_at = ?,
                worker_id = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE status IN (?, ?)
              AND started_at IS NOT NULL AND started_at <= ?
            """,
            (
                JobStatus.failed.value,
                now.isoformat(),
                now.isoformat(),
                JobStatus.running.value,
                JobStatus.paused.value,
                long_running_before,
            ),
        ).rowcount
        deleted = conn.exec_driver_sql(
            """
            DELETE FROM job
            WHERE status IN (?, ?, ?) AND COALESCE(finished_at, updated_at) <= ?
            """,
            (
                JobStatus.completed.value,
                JobStatus.failed.value,
                JobStatus.cancelled.value,
                finished_before,
            ),
        ).rowcount
    return cancelled, deleted


def get_active_jobs(novel_id: int) -> List[Job]:
    with Session(_engine) as session:
        return list(session.exec(
            select(Job).where(
                Job.novel_id == novel_id,
                Job.status.in_([JobStatus.queued, JobStatus.running])
            )
        ).all())
