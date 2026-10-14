"""Management tables. Source JSON remains complete and build-scoped."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT, CHAR, DATETIME, INTEGER, JSON, LONGTEXT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_0900_ai_ci"}
HASH = CHAR(64, charset="ascii", collation="ascii_bin")
IDENTITY = String(128, collation="utf8mb4_bin")


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), default=utcnow, onupdate=utcnow)


class KnowledgeBase(Timestamped, Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("code", name="uq_kb_code"),
                      CheckConstraint("status IN ('active','disabled','archived')", name="ck_kb_status"), OPTIONS)
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(IDENTITY)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(LONGTEXT, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")


class Document(Timestamped, Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("knowledge_base_id", "source_key", name="uq_document_source"),
                      CheckConstraint("status IN ('active','disabled','archived')", name="ck_document_status"),
                      Index("ix_document_kb_status_id", "knowledge_base_id", "status", "id"), OPTIONS)
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    knowledge_base_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), ForeignKey("knowledge_bases.id", ondelete="RESTRICT"))
    source_key: Mapped[str] = mapped_column(HASH)
    source_path: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(LONGTEXT)
    category: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="active")
    active_build_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey(
        "document_builds.id", name="fk_document_active_build", use_alter=True, ondelete="RESTRICT"))


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "source_sha256", name="uq_version_source"), OPTIONS)
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), ForeignKey("documents.id", ondelete="RESTRICT"))
    source_sha256: Mapped[str] = mapped_column(HASH)
    original_filename: Mapped[str] = mapped_column(String(255))
    format: Mapped[str] = mapped_column(String(16))
    file_size: Mapped[int] = mapped_column(BIGINT(unsigned=True))
    storage_uri: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), default=utcnow)


class DocumentBuild(Base):
    __tablename__ = "document_builds"
    __table_args__ = (UniqueConstraint("document_version_id", "output_fingerprint", name="uq_build_output"),
                      CheckConstraint("status IN ('ready','failed','empty')", name="ck_build_status"), OPTIONS)
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    document_version_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), ForeignKey("document_versions.id", ondelete="RESTRICT"))
    legacy_doc_id: Mapped[str] = mapped_column(IDENTITY)
    output_fingerprint: Mapped[str] = mapped_column(HASH)
    pipeline_config_json: Mapped[dict] = mapped_column(JSON)
    document_payload_json: Mapped[dict] = mapped_column(JSON)
    artifact_manifest_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), default=utcnow)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("build_id", "legacy_chunk_id", name="uq_chunk_legacy"),
                      UniqueConstraint("build_id", "chunk_index", name="uq_chunk_position"),
                      Index("ix_chunk_build_retrieval_position", "build_id", "retrieval_enabled", "chunk_index"), OPTIONS)
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    build_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), ForeignKey("document_builds.id", ondelete="RESTRICT"))
    legacy_chunk_id: Mapped[str] = mapped_column(IDENTITY)
    chunk_index: Mapped[int] = mapped_column(INTEGER(unsigned=True))
    body: Mapped[str] = mapped_column(LONGTEXT)
    text: Mapped[str] = mapped_column(LONGTEXT)
    token_count: Mapped[int] = mapped_column(INTEGER(unsigned=True))
    structure_type: Mapped[str] = mapped_column(String(64))
    retrieval_enabled: Mapped[bool] = mapped_column(Boolean)
    payload_json: Mapped[dict] = mapped_column(JSON)


class ImportRun(Base):
    __tablename__ = "import_runs"
    __table_args__ = (CheckConstraint("status IN ('running','succeeded','partial_failed','failed')", name="ck_import_status"),
                      CheckConstraint("result_storage IN ('legacy','items')", name="ck_import_result_storage"), OPTIONS)
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    knowledge_base_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), ForeignKey("knowledge_bases.id", ondelete="RESTRICT"))
    source_uri: Mapped[str] = mapped_column(String(2048))
    snapshot_fingerprint: Mapped[str | None] = mapped_column(HASH)
    status: Mapped[str] = mapped_column(String(16), default="running")
    summary_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # Compatibility archive only; deferred to avoid loading old cumulative arrays.
    results_json: Mapped[list] = mapped_column(JSON, default=list, deferred=True)
    result_storage: Mapped[str] = mapped_column(String(16), default="items", server_default="legacy")
    error_message: Mapped[str | None] = mapped_column(LONGTEXT)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))


class ImportRunItem(Base):
    __tablename__ = "import_run_items"
    __table_args__ = (UniqueConstraint("import_run_id", "ordinal", name="uq_import_item_ordinal"),
                      CheckConstraint("status IN ('imported','reused','failed')", name="ck_import_item_status"),
                      Index("ix_import_item_run_status_ordinal", "import_run_id", "status", "ordinal"), OPTIONS)
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    import_run_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), ForeignKey("import_runs.id", ondelete="RESTRICT"))
    ordinal: Mapped[int] = mapped_column(INTEGER(unsigned=True))
    status: Mapped[str] = mapped_column(String(16))
    document_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("documents.id", ondelete="RESTRICT"))
    build_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("document_builds.id", ondelete="RESTRICT"))
    payload_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), default=utcnow)


class ProcessingTask(Timestamped, Base):
    __tablename__ = "processing_tasks"
    __table_args__ = (
        UniqueConstraint("document_version_id", "config_sha256", name="uq_task_version_config"),
        CheckConstraint("status IN ('pending','running','succeeded','failed')", name="ck_task_status"),
        CheckConstraint("stage IN ('validation','parsing','chunking','publishing','persisting')", name="ck_task_stage"),
        Index("ix_task_schedule", "status", "scheduled_at", "id"),
        Index("ix_task_lease", "status", "lease_expires_at"), OPTIONS)
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    document_version_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), ForeignKey("document_versions.id", ondelete="RESTRICT"))
    config_sha256: Mapped[str] = mapped_column(HASH)
    config_json: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    stage: Mapped[str] = mapped_column(String(16), default="validation")
    build_id: Mapped[int | None] = mapped_column(BIGINT(unsigned=True), ForeignKey("document_builds.id", ondelete="RESTRICT"))
    attempt_count: Mapped[int] = mapped_column(INTEGER(unsigned=True), default=0)
    cycle_attempts: Mapped[int] = mapped_column(INTEGER(unsigned=True), default=0)
    scheduled_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), default=utcnow)
    execution_token: Mapped[str | None] = mapped_column(String(32))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    error_code: Mapped[str | None] = mapped_column(String(64))
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))


class ProcessingAttempt(Base):
    __tablename__ = "processing_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "ordinal", name="uq_attempt_ordinal"),
        CheckConstraint("status IN ('running','succeeded','failed','interrupted')", name="ck_attempt_status"),
        CheckConstraint("stage IN ('validation','parsing','chunking','publishing','persisting')", name="ck_attempt_stage"), OPTIONS)
    id: Mapped[int] = mapped_column(BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BIGINT(unsigned=True), ForeignKey("processing_tasks.id", ondelete="RESTRICT"))
    ordinal: Mapped[int] = mapped_column(INTEGER(unsigned=True))
    execution_token: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="running")
    stage: Mapped[str] = mapped_column(String(16), default="validation")
    started_at: Mapped[datetime] = mapped_column(DATETIME(fsp=6), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DATETIME(fsp=6))
    error_code: Mapped[str | None] = mapped_column(String(64))
