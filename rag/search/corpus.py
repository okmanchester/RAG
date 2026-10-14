"""Consistent active-build selection and paged immutable MySQL reads."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import select, text, func

from rag.management.errors import ManagementError
from rag.management.models import KnowledgeBase, Document, DocumentBuild, DocumentVersion, Chunk
from .shared import context, digest


@dataclass(frozen=True)
class Selection:
    kb_id: int
    status: str
    builds: tuple[dict, ...]

    @property
    def fingerprint(self):
        return digest({"kb_id": self.kb_id, "status": self.status, "builds": self.builds})

    @property
    def build_ids(self):
        return [b["build_id"] for b in self.builds]


class ManagedCorpus:
    def __init__(self, engine):
        self.engine = engine
        self.namespace = digest({"host": engine.url.host, "port": engine.url.port,
                                 "database": engine.url.database})[:16]

    def selection(self, kb_id, *, allow_disabled=False):
        if type(kb_id) is not int or kb_id < 1:
            raise ManagementError("INVALID_SEARCH")
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                kb = connection.execute(select(KnowledgeBase.id, KnowledgeBase.status)
                                        .where(KnowledgeBase.id == kb_id)).first()
                if kb is None:
                    raise ManagementError("RECORD_NOT_FOUND")
                if kb.status != "active" and not allow_disabled:
                    raise ManagementError("RESOURCE_DISABLED")
                rows = connection.execute(select(Document.id.label("document_id"), Document.active_build_id,
                    Document.category, DocumentBuild.id.label("build_id"), DocumentBuild.status,
                    DocumentBuild.output_fingerprint,
                    func.json_unquote(func.json_extract(DocumentBuild.document_payload_json, "$.chunk_count")).label("expected_chunks"),
                    DocumentVersion.document_id.label("owner_id"),
                    select(func.count(Chunk.id)).where(Chunk.build_id == DocumentBuild.id)
                    .correlate(DocumentBuild).scalar_subquery().label("total_chunks"),
                    select(func.count(Chunk.id)).where(Chunk.build_id == DocumentBuild.id, Chunk.retrieval_enabled.is_(True))
                    .correlate(DocumentBuild).scalar_subquery().label("eligible_chunks"))
                    .outerjoin(DocumentBuild, Document.active_build_id == DocumentBuild.id)
                    .outerjoin(DocumentVersion, DocumentBuild.document_version_id == DocumentVersion.id)
                    .where(Document.knowledge_base_id == kb_id, Document.status == "active",
                           Document.active_build_id.is_not(None)).order_by(Document.id)).all()
                builds = []
                for row in rows:
                    if row.owner_id != row.document_id or row.status != "ready":
                        raise ManagementError("BUILD_NOT_READY")
                    if not row.total_chunks or (row.expected_chunks is not None and row.total_chunks != int(row.expected_chunks)):
                        raise ManagementError("BUILD_INCOMPLETE")
                    builds.append({"document_id": row.document_id, "build_id": row.build_id,
                                   "output_fingerprint": row.output_fingerprint, "category": row.category,
                                   "eligible_chunks": row.eligible_chunks})
                return Selection(kb_id, kb.status, tuple(builds) if kb.status == "active" else ())

    def iter_chunks(self, selection, page_size=128):
        if not selection.builds:
            return
        last = 0
        # Builds/chunks are immutable, so no transaction is held during model inference.
        while True:
            with self.engine.connect() as connection:
                rows = connection.execute(select(Chunk.__table__, DocumentVersion.source_sha256,
                    DocumentVersion.document_id, DocumentBuild.legacy_doc_id)
                    .join(DocumentBuild, Chunk.build_id == DocumentBuild.id)
                    .join(DocumentVersion, DocumentBuild.document_version_id == DocumentVersion.id)
                    .where(Chunk.build_id.in_(selection.build_ids), Chunk.retrieval_enabled.is_(True), Chunk.id > last)
                    .order_by(Chunk.id).limit(page_size)).mappings().all()
            if not rows:
                return
            for row in rows:
                yield dict(row)
            last = rows[-1]["id"]

    @staticmethod
    def projection(row, selection):
        build = next(b for b in selection.builds if b["build_id"] == row["build_id"])
        return {"chunk_id": row["id"], "kb_id": selection.kb_id, "document_id": row["document_id"],
                "build_id": row["build_id"], "legacy_doc_id": row["legacy_doc_id"],
                "legacy_chunk_id": row["legacy_chunk_id"], "document_sha256": row["source_sha256"],
                "category": build["category"], "structure_type": row["structure_type"],
                "text": row["text"], "payload_sha256": digest(row["payload_json"])}

    def hydrate(self, kb_id, candidates):
        if not candidates:
            # Still verify disabled KBs even when ES has no hits.
            self.selection(kb_id)
            return []
        ids = [int(hit["_id"]) for hit in candidates]
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                kb = connection.execute(select(KnowledgeBase.status).where(KnowledgeBase.id == kb_id)).scalar()
                if kb != "active":
                    raise ManagementError("RESOURCE_DISABLED")
                rows = connection.execute(select(Chunk.__table__, DocumentVersion.source_sha256,
                    DocumentVersion.document_id)
                    .join(DocumentBuild, Chunk.build_id == DocumentBuild.id)
                    .join(DocumentVersion, DocumentBuild.document_version_id == DocumentVersion.id)
                    .join(Document, DocumentVersion.document_id == Document.id)
                    .where(Chunk.id.in_(ids), Chunk.retrieval_enabled.is_(True), Document.knowledge_base_id == kb_id,
                           Document.status == "active", Document.active_build_id == Chunk.build_id,
                           DocumentBuild.status == "ready")).mappings().all()
                by_id = {row["id"]: row for row in rows}
                result = []
                for hit in candidates:
                    row = by_id.get(int(hit["_id"]))
                    if row is None:
                        continue
                    source = hit["_source"]
                    if (source["kb_id"] != kb_id or source["build_id"] != row["build_id"] or
                        source["payload_sha256"] != digest(row["payload_json"])):
                        raise ManagementError("INDEX_CORRUPT")
                    result.append({**context(row["payload_json"], row["source_sha256"],
                                            identifier=f"mysql:{row['id']}"),
                        "mysql_chunk_id": row["id"], "document_id": row["document_id"], "build_id": row["build_id"],
                        "legacy_chunk_id": row["legacy_chunk_id"], "score": hit["_score"],
                        "ranks": hit.get("ranks", {})})
                return result

    @contextmanager
    def index_lock(self, kb_id):
        # Serializes sync/rollback across processes, without holding row locks during embedding.
        lock = f"rag-index:{self.namespace}:{kb_id}"
        with self.engine.connect() as connection:
            if connection.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": lock}) != 1:
                raise ManagementError("INDEX_BUSY")
            try:
                yield
            finally:
                connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock})
