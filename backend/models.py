"""
NovelBridge — SQLite data models.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

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
    created_at:  datetime      = Field(default_factory=datetime.utcnow)
    updated_at:  datetime      = Field(default_factory=datetime.utcnow)


class Chapter(SQLModel, table=True):
    id:              Optional[int]    = Field(default=None, primary_key=True)
    novel_id:        int              = Field(foreign_key="novel.id", index=True)
    index:           int              # 0-based order
    title:           str
    source_url:      str
    raw_text:        Optional[str]    = None
    translated_text: Optional[str]   = None
    status:          ChapterStatus    = Field(default=ChapterStatus.pending)
    updated_at:      datetime         = Field(default_factory=datetime.utcnow)


class GlossaryRule(SQLModel, table=True):
    id:          Optional[int] = Field(default=None, primary_key=True)
    source_term: str
    target_term: str
    notes:       Optional[str] = None
    is_global:   bool          = Field(default=True)


class Job(SQLModel, table=True):
    id:            Optional[int] = Field(default=None, primary_key=True)
    novel_id:      int           = Field(foreign_key="novel.id", index=True)
    type:          JobType
    status:        JobStatus     = Field(default=JobStatus.queued)
    progress:      int           = Field(default=0)   # 0-100
    total_items:   int           = Field(default=0)
    done_items:    int           = Field(default=0)
    error_message: Optional[str] = None
    created_at:    datetime      = Field(default_factory=datetime.utcnow)
    updated_at:    datetime      = Field(default_factory=datetime.utcnow)
