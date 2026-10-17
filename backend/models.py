"""
NovelBridge — SQLite data models.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


# ── Enums ──────────────────────────────────────────────────────────────────────

class NovelStatus(str, Enum):
    pending   = "pending"
    scraping  = "scraping"
    scraped   = "scraped"
    failed    = "failed"


class ChapterStatus(str, Enum):
    pending     = "pending"
    scraped     = "scraped"
    translating = "translating"
    translated  = "translated"
    failed      = "failed"


class JobType(str, Enum):
    scrape      = "scrape"
    translate   = "translate"
    build_epub  = "build_epub"


class JobStatus(str, Enum):
    queued     = "queued"
    running    = "running"
    paused     = "paused"
    completed  = "completed"
    failed     = "failed"
    cancelled  = "cancelled"


# ── SQLModel Tables ────────────────────────────────────────────────────────────

class Novel(SQLModel, table=True):
    id:          Optional[int] = Field(default=None, primary_key=True)
    title:       str
    source_url:  str
    source_site: str
    author:      Optional[str] = None
    cover_url:   Optional[str] = None
    description: Optional[str] = None
    status:      NovelStatus   = Field(default=NovelStatus.pending)
    created_at:  datetime      = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at:  datetime      = Field(default_factory=lambda: datetime.now(timezone.utc))


class Chapter(SQLModel, table=True):
    id:              Optional[int]    = Field(default=None, primary_key=True)
    novel_id:        int              = Field(foreign_key="novel.id", index=True)
    index:           int              # 0-based order
    title:           str
    source_url:      str
    raw_text:        Optional[str]    = None
    translated_text: Optional[str]   = None
    status:          ChapterStatus    = Field(default=ChapterStatus.pending)
    updated_at:      datetime         = Field(default_factory=lambda: datetime.now(timezone.utc))
    # ── Content versioning (§B4) ──────────────────────────────────────────────
    crawler_version: Optional[str]   = Field(default=None)   # adapter.version at scrape time
    crawler_tier:    Optional[str]   = Field(default="legacy")  # "legacy" | "spec"
    content_hash:    Optional[str]   = Field(default=None)   # sha256(raw_text) for dedup
    # ── Empty chapter tracking (§B3) ─────────────────────────────────────────
    empty_attempts:  int             = Field(default=0)   # capped at MAX_EMPTY_ATTEMPTS=3
    volume_serial:   Optional[int]   = Field(default=None, index=True)
    volume_title:    Optional[str]   = Field(default=None)
    volume_explicit: bool            = Field(default=False)
    translated_hash: Optional[str]   = Field(default=None)


class Volume(SQLModel, table=True):
    id:            Optional[int] = Field(default=None, primary_key=True)
    novel_id:      int           = Field(foreign_key="novel.id", index=True)
    serial:        int
    title:         Optional[str] = None
    chapter_count: int           = Field(default=0)

    __table_args__ = (UniqueConstraint("novel_id", "serial", name="uq_volume_novel_serial"),)


class ChapterImage(SQLModel, table=True):
    id:          Optional[int] = Field(default=None, primary_key=True)
    chapter_id:  int           = Field(foreign_key="chapter.id", index=True)
    image_id:    str
    file_path:   str
    media_type:  str
    alt_text:    Optional[str] = None
    size_bytes:  int

    __table_args__ = (
        UniqueConstraint("chapter_id", "image_id", name="uq_chapter_image_id"),
    )


class NovelGlossary(SQLModel, table=True):
    id:        Optional[int] = Field(default=None, primary_key=True)
    novel_id:  int           = Field(foreign_key="novel.id", index=True)
    language:  str           = Field(default="ar")
    terms_json: str          = Field(default="{}")

    __table_args__ = (UniqueConstraint("novel_id", "language", name="uq_novel_glossary_language"),)


class GlossaryRule(SQLModel, table=True):
    id:          Optional[int] = Field(default=None, primary_key=True)
    source_term: str
    target_term: str
    notes:       Optional[str] = None
    is_global:   bool          = Field(default=True)


class Job(SQLModel, table=True):
    id:            Optional[int] = Field(default=None, primary_key=True)
    novel_id:      int           = Field(foreign_key="novel.id", index=True)
    parent_job_id: Optional[int] = Field(default=None)
    type:          JobType
    status:        JobStatus     = Field(default=JobStatus.queued)
    domain:        Optional[str] = Field(default=None)
    priority:      int           = Field(default=0)
    progress:      int           = Field(default=0)   # 0-100
    total_items:   int           = Field(default=0)
    done_items:    int           = Field(default=0)
    failed_items:  int           = Field(default=0)
    options_json:  str           = Field(default="{}")
    current_message: Optional[str] = None
    error_message: Optional[str] = None
    output_path:   Optional[str] = None
    worker_id:     Optional[str] = None
    heartbeat_at:  Optional[datetime] = None
    started_at:    Optional[datetime] = None
    finished_at:   Optional[datetime] = None
    lease_expires_at: Optional[datetime] = None
    created_at:    datetime      = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at:    datetime      = Field(default_factory=lambda: datetime.now(timezone.utc))
