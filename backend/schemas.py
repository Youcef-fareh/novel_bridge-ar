"""Stable request and response DTOs for the HTTP API."""
from __future__ import annotations

from typing import Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, Field

from backend.models import JobStatus, JobType

T = TypeVar("T")


class Paginated(BaseModel, Generic[T]):
    items: list[T]
    total: int
    offset: int
    limit: int


class JobSummary(BaseModel):
    id: int
    novel_id: int
    type: JobType
    status: JobStatus
    progress: int
    done_items: int
    total_items: int
    failed_items: int
    current_message: Optional[str] = None
    error_message: Optional[str] = None
    output_path: Optional[str] = None


class NovelSummary(BaseModel):
    id: int
    title: str
    source_url: str
    source_site: str
    author: Optional[str] = None
    cover_url: Optional[str] = None
    status: str
    created_at: str


class ArtifactRequest(BaseModel):
    format: str = Field(default="epub", pattern="^(epub|txt|json|pdf|azw3|mobi|docx)$")
    volume: Optional[int] = Field(default=None, ge=1)
    language: Literal["ar"] = "ar"


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=50)
    domain: Optional[str] = None
