"""Shared data contracts used by crawler, persistence, AI, and web layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class SyncRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AIStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    DISABLED = "disabled"


class ShareItemStatus(StrEnum):
    """How an item was handled in a completed share batch."""

    SHARED = "shared"
    REVIEWED_UNSHARED = "reviewed_unshared"


@dataclass(frozen=True, slots=True)
class SourceConfig:
    key: str
    name: str
    base_url: str
    list_path: str
    category: str
    history_start: date = date(2025, 7, 1)
    site_key: str = ""


@dataclass(frozen=True, slots=True)
class ListEntry:
    source_key: str
    article_id: str
    title: str
    url: str
    published_date: date
    list_position: int


@dataclass(frozen=True, slots=True)
class NoticeDocument:
    source_key: str
    article_id: str
    official_title: str
    original_url: str
    published_date: date
    body_text: str
    body_html: str
    attachment_names: tuple[str, ...] = ()
    site_key: str = ""
    categories: tuple[str, ...] = ()
    first_seen_at: datetime | None = None
    last_checked_at: datetime | None = None
    first_sync_run_id: int | None = None


@dataclass(slots=True)
class AIEnrichment:
    short_title: str = ""
    summary: str = ""
    audience: str = ""
    deadline: str = ""
    action: str = ""
    start_time: str = ""
    materials: list[str] = field(default_factory=list)
    method_or_location: str = ""
    contact: str = ""
    requires_external_system: bool | None = None
    uncertainty: str = ""
    tags: list[str] = field(default_factory=list)
    status: AIStatus = AIStatus.PENDING
    model: str = ""
    generated_at: datetime | None = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class SyncRunResult:
    run_id: int
    status: SyncRunStatus
    new_count: int
    known_count: int
    failed_sources: tuple[str, ...] = ()
    error: str = ""


@dataclass(frozen=True, slots=True)
class ShareCandidate:
    notice_id: int
    official_title: str
    short_title: str
    original_url: str
    published_date: date
    first_sync_run_id: int


@dataclass(frozen=True, slots=True)
class SearchHit:
    notice_id: int
    official_title: str
    short_title: str
    original_url: str
    published_date: date
    source_name: str
    categories: tuple[str, ...]
    snippet: str = ""
