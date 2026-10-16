"""Explicit public DTOs; database/configuration dictionaries never cross HTTP unfiltered."""
from datetime import datetime, timezone
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_serializer

Identifier = Annotated[int, Field(gt=0, le=18446744073709551615)]
ManagementStatus = Literal["active", "disabled", "archived"]
TaskStatus = Literal["pending", "running", "succeeded", "failed"]
Stage = Literal["validation", "parsing", "chunking", "publishing", "persisting"]
BuildStatus = Literal["ready", "failed", "empty"]
Mode = Literal["dense", "bm25", "hybrid"]
T = TypeVar("T")


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_serializer("*", when_used="json", check_fields=False)
    def serialize_dates(self, value):
        if isinstance(value, datetime):
            value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
            return value.isoformat(timespec="microseconds").replace("+00:00", "Z")
        return value


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Error(PublicModel):
    error_code: str
    error: str


class Page(PublicModel, Generic[T]):
    total: int
    limit: int
    offset: int
    items: list[T]


class KnowledgeBase(PublicModel):
    id: Identifier
    code: str
    name: str
    description: str
    status: ManagementStatus
    created_at: datetime
    updated_at: datetime


class Document(PublicModel):
    id: Identifier
    knowledge_base_id: Identifier
    source_path: str
    title: str
    category: str
    status: ManagementStatus
    active_build_id: Identifier | None
    created_at: datetime
    updated_at: datetime


class DocumentDetail(Document):
    active_chunk_count: int
    active_eligible_chunk_count: int


class Version(PublicModel):
    id: Identifier
    document_id: Identifier
    source_sha256: str
    original_filename: str
    format: Literal["pdf", "docx"]
    file_size: int
    created_at: datetime


class Build(PublicModel):
    id: Identifier
    document_version_id: Identifier
    legacy_doc_id: str
    output_fingerprint: str
    status: BuildStatus
    created_at: datetime
    chunk_count: int
    is_active: bool
    can_activate: bool


class TaskSubmission(PublicModel):
    task_id: Identifier
    status: TaskStatus
    build_id: Identifier | None
    reused: bool


class UploadResult(PublicModel):
    document_id: Identifier
    version_id: Identifier
    source_sha256: str
    active_build_id: Identifier | None
    task_id: Identifier
    status: TaskStatus
    build_id: Identifier | None
    reused_version: bool
    reused_task: bool


class SyncTarget(PublicModel):
    kb_id: Identifier
    desired_revision: int
    status: TaskStatus
    paused: bool


class Activation(RequestModel):
    build_id: Annotated[int, Field(strict=True, gt=0, le=18446744073709551615)]


class ActivationResult(PublicModel):
    document_id: Identifier
    previous_build_id: Identifier | None
    active_build_id: Identifier
    sync_target: SyncTarget


class StatusChange(RequestModel):
    status: Literal["active", "disabled"]


class StatusResult(Document):
    sync_target: SyncTarget


class ProcessingTask(PublicModel):
    id: Identifier
    document_version_id: Identifier
    status: TaskStatus
    stage: Stage
    build_id: Identifier | None
    attempt_count: int
    cycle_attempts: int
    scheduled_at: datetime
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
    error_code: str | None
    error: str | None


class ProcessingAttempt(PublicModel):
    id: Identifier
    task_id: Identifier
    ordinal: int
    status: Literal["running", "succeeded", "failed", "interrupted"]
    stage: Stage
    started_at: datetime
    finished_at: datetime | None
    error_code: str | None
    error: str | None


class ProcessingDetail(ProcessingTask):
    attempts: Page[ProcessingAttempt]
    build_status: BuildStatus | None = None
    can_activate: bool = False


class RetryResult(PublicModel):
    task_id: Identifier
    status: TaskStatus


class SyncTask(PublicModel):
    knowledge_base_id: Identifier
    desired_revision: int
    applied_revision: int
    status: TaskStatus
    paused: bool
    scheduled_at: datetime
    attempt_count: int
    cycle_attempts: int
    generation: str | None
    error_code: str | None
    error: str | None = None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SyncAttempt(PublicModel):
    id: Identifier
    knowledge_base_id: Identifier
    ordinal: int
    target_revision: int
    status: Literal["running", "succeeded", "failed", "interrupted", "superseded", "deferred"]
    manual: bool
    generation: str | None
    started_at: datetime
    finished_at: datetime | None
    error_code: str | None
    error: str | None = None


class SyncControl(PublicModel):
    kb_id: Identifier
    sync_task: SyncTask


class IndexStatus(SyncControl):
    attempts: Page[SyncAttempt]
    index_checked: bool
    sync_lag: bool | None = None
    generation: str | None = None
    state: str | None = None
    chunk_count: int | None = None


class SearchRequest(RequestModel):
    query: Annotated[str, Field(min_length=1, pattern=r"\S")]
    top_k: Annotated[int, Field(strict=True, ge=1, le=50)] = 10
    mode: Mode | None = None
    category: Annotated[str, Field(min_length=1, pattern=r"\S")] | None = None
    structure_type: Annotated[str, Field(min_length=1, pattern=r"\S")] | None = None


class SearchContext(PublicModel):
    id: str
    mysql_chunk_id: Identifier
    document_id: Identifier
    version_id: Identifier
    build_id: Identifier
    legacy_chunk_id: str
    doc_id: str
    document_sha256: str
    source_path: str
    title: str
    heading_path: list[str]
    text: str
    score: float
    ranks: dict[str, int]
    structure_type: str
    sources: list[dict[str, JsonValue]]
    derived_sources: list[dict[str, JsonValue]]
    asset_refs: list[dict[str, JsonValue]]


class SearchResult(PublicModel):
    kb_id: Identifier
    generation: str
    mode: Mode
    sync_lag: bool
    contexts: list[SearchContext]


class Health(PublicModel):
    status: Literal["ok"] = "ok"


class ProcessStatus(PublicModel):
    state: Literal["unknown", "running", "stopped", "waiting", "backoff"]
    restart_count: int = 0
    last_exit_code: int | None = None
    next_start_at: datetime | None = None


class DependencyStatus(PublicModel):
    state: Literal["unknown", "available", "unavailable"]
    checked_at: datetime | None = None


class BackupStatus(PublicModel):
    status: Literal["running", "succeeded", "failed"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_success_at: datetime | None = None
    error_code: str | None = None


class RuntimeStatus(PublicModel):
    signal: Literal["unknown", "stale", "fresh"]
    mode: Literal["demo", "supervised"] | None = None
    updated_at: datetime | None = None
    supervisor: Literal["unknown", "running", "stopped"]
    api: ProcessStatus
    worker: ProcessStatus
    dependencies: dict[str, DependencyStatus]
    backup: BackupStatus | None = None
