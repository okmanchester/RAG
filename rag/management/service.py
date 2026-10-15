"""Transactional management operations, reusable by a future HTTP layer."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .errors import ManagementError, public_error, safe_error, stored_error

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from .config import ROOT, migration_config
from .models import Base, Chunk, Document, DocumentBuild, DocumentVersion, ImportRun, ImportRunItem, KnowledgeBase, utcnow
from .repository import MODELS, build_document, chunk_count, current_build, record, required
from .storage import fingerprint, load_snapshot, publish_raw, relative_path

STATES = ("active", "disabled", "archived")
ITEM_STATES = ("imported", "reused", "failed")
STATUS_FILTERS = {"kb": STATES, "document": STATES, "build": ("ready", "failed", "empty"),
                  "import": ("running", "succeeded", "partial_failed", "failed")}


def validate_page(limit, offset):
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 1000 or offset < 0:
        raise ManagementError("INVALID_PAGE")


def empty_summary(documents=0):
    return {"documents": documents, "imported": 0, "reused": 0, "failed": 0, "chunks": 0, "eligible_chunks": 0}


class ManagementService:
    def __init__(self, engine, storage_root: Path):
        self.engine = engine
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.storage_root = Path(storage_root)

    def db_upgrade(self):
        from alembic import command
        with self.engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
        return self.db_check()

    def db_check(self):
        from alembic.migration import MigrationContext
        from alembic.script import ScriptDirectory
        expected = set(ScriptDirectory.from_config(migration_config()).get_heads())
        with self.engine.connect() as connection:
            actual = set(MigrationContext.configure(connection).get_current_heads())
            missing = set(Base.metadata.tables) - set(inspect(connection).get_table_names())
            if actual != expected or missing:
                raise ManagementError('SCHEMA_OUTDATED')
            return {"database": connection.scalar(text("SELECT DATABASE()")),
                    "server_version": connection.scalar(text("SELECT VERSION()")),
                    "migration_heads": sorted(actual), "business_tables": len(Base.metadata.tables),
                    "session_timezone": connection.scalar(text("SELECT @@session.time_zone"))}

    def kb_create(self, code, name, description=""):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", code):
            raise ManagementError('INVALID_KB_CODE')
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            raise ManagementError('INVALID_KB_NAME')
        with self.sessions.begin() as session:
            if session.scalar(select(KnowledgeBase.id).where(KnowledgeBase.code == code)):
                raise ManagementError('KB_CODE_EXISTS')
            item = KnowledgeBase(code=code, name=name, description=description)
            session.add(item)
            session.flush()
            from .index_sync import initialize
            return {**record(item), "sync_target": initialize(session, item.id)}

    def kb_update(self, kb_id, *, name=None, description=None):
        if name is None and description is None:
            raise ManagementError('EMPTY_KB_UPDATE')
        if name is not None and (not name.strip() or len(name) > 255):
            raise ManagementError('INVALID_KB_NAME')
        with self.sessions.begin() as session:
            item = required(session, KnowledgeBase, kb_id, lock=True)
            if name is not None:
                item.name = name
            if description is not None:
                item.description = description
            session.flush()
            return record(item)

    def set_status(self, kind, identifier, status):
        if kind not in ("kb", "document") or status not in STATES:
            raise ManagementError('INVALID_MANAGEMENT_STATUS')
        with self.sessions.begin() as session:
            item = required(session, MODELS[kind], identifier, lock=True)
            changed = item.status != status
            item.status = status
            session.flush()
            from .index_sync import mark_dirty, state_row, target
            kb_id = item.id if kind == "kb" else item.knowledge_base_id
            sync_target = mark_dirty(session, kb_id) if changed else target(state_row(session, kb_id))
            return {**record(item), "sync_target": sync_target}

    def show(self, kind, identifier, *, limit=100, offset=0):
        if not isinstance(kind, str) or kind not in MODELS:
            raise ManagementError("INVALID_RESOURCE")
        if kind == "import":
            return self.show_import(identifier, limit=limit, offset=offset)
        with self.sessions() as session:
            item = required(session, MODELS[kind], identifier)
            result = record(item)
            if kind == "document":
                result["active_chunk_count"] = chunk_count(session, item.active_build_id) if item.active_build_id else 0
                result["active_eligible_chunk_count"] = chunk_count(session, item.active_build_id, eligible=True) if item.active_build_id else 0
            return result

    def list_records(self, kind, *, kb_id=None, document_id=None, build_id=None, status=None,
                     eligible_only=False, limit=100, offset=0):
        validate_page(limit, offset)
        if not isinstance(kind, str) or kind not in MODELS:
            raise ManagementError("INVALID_RESOURCE")
        if status is not None:
            if kind not in STATUS_FILTERS:
                raise ManagementError("UNSUPPORTED_STATUS_FILTER")
            if status not in STATUS_FILTERS[kind]:
                raise ManagementError("INVALID_STATUS_FILTER")
        model = MODELS[kind]
        with self.sessions() as session:
            statement = select(model)
            if kind == "import":
                # The obsolete legacy array is deliberately not read by current queries.
                from sqlalchemy.orm import defer
                statement = statement.options(defer(ImportRun.results_json))
            if kind == "document":
                required(session, KnowledgeBase, kb_id)
                statement = statement.where(Document.knowledge_base_id == kb_id)
            elif kind in ("version", "build"):
                required(session, Document, document_id)
                if kind == "version":
                    statement = statement.where(DocumentVersion.document_id == document_id)
                else:
                    statement = statement.join(DocumentVersion).where(DocumentVersion.document_id == document_id)
            elif kind == "chunk":
                if build_id is None:
                    if document_id is None:
                        raise ManagementError('BUILD_SELECTOR_REQUIRED')
                    build_id = current_build(session, document_id)
                build = required(session, DocumentBuild, build_id)
                if document_id is not None and build_document(session, build) != document_id:
                    raise ManagementError('BUILD_OWNERSHIP')
                statement = statement.where(Chunk.build_id == build_id)
                if eligible_only:
                    statement = statement.where(Chunk.retrieval_enabled.is_(True))
            if status is not None:
                statement = statement.where(model.status == status)
            total = session.scalar(select(func.count()).select_from(statement.subquery()))
            order = Chunk.chunk_index if kind == "chunk" else model.id
            items = [self._import_header(item) if kind == "import" else record(item)
                     for item in session.scalars(statement.order_by(order).offset(offset).limit(limit))]
            return {"total": total, "limit": limit, "offset": offset, "items": items}

    def import_corpus(self, kb_id, source: Path, raw_root: Path = ROOT / "data/raw"):
        source, raw_root = Path(source).resolve(), Path(raw_root).resolve()
        with self.sessions.begin() as session:
            required(session, KnowledgeBase, kb_id)
            run = ImportRun(knowledge_base_id=kb_id, source_uri=source.as_posix(), summary_json=empty_summary())
            session.add(run)
            session.flush()
            run_id = run.id
        try:
            snapshot = load_snapshot(source)
            snapshot_dir = snapshot.publish(self.storage_root)
            with self.sessions.begin() as session:
                run = required(session, ImportRun, run_id)
                run.snapshot_fingerprint = snapshot.fingerprint
                run.summary_json = empty_summary(len(snapshot.documents))
            for ordinal, payload in enumerate(snapshot.documents, 1):
                try:
                    raw_file = publish_raw(raw_root, payload["source_path"], payload["sha256"], self.storage_root)
                    chunks = snapshot.chunks_by_doc[payload["doc_id"]]
                    self._import_document(kb_id, payload, chunks, raw_file, snapshot, snapshot_dir, run_id, ordinal)
                except Exception as exc:
                    result = {"legacy_doc_id": payload["doc_id"], "source_path": payload["source_path"],
                              "status": "failed", **public_error(exc)}
                    # The document transaction was rolled back; persist its failure separately.
                    with self.sessions.begin() as session:
                        self._append_item(session, run_id, ordinal, result)
            self._finish_import(run_id)
        except Exception as exc:
            self._finish_import(run_id, exc)
        return self.show_import(run_id)

    @staticmethod
    def _append_item(session, run_id, ordinal, result):
        run = required(session, ImportRun, run_id, lock=True)
        session.add(ImportRunItem(import_run_id=run_id, ordinal=ordinal, status=result["status"],
                                  document_id=result.get("document_id"), build_id=result.get("build_id"),
                                  payload_json=result))
        summary = dict(run.summary_json)
        summary[result["status"]] += 1
        summary["chunks"] += result.get("chunks", 0)
        summary["eligible_chunks"] += result.get("eligible_chunks", 0)
        run.summary_json = summary
        # A log insertion/counter failure must roll back the same document transaction.
        session.flush()

    def _finish_import(self, run_id, failure=None):
        with self.sessions.begin() as session:
            run = required(session, ImportRun, run_id, lock=True)
            if failure is not None:
                details = public_error(failure)
                run.status, run.error_code, run.error_message = "failed", details["error_code"], details["error"]
            else:
                summary = run.summary_json
                processed = sum(summary[key] for key in ITEM_STATES)
                if processed != summary["documents"]:
                    raise ManagementError("IMPORT_ITEM_INCOMPLETE")
                run.status = "failed" if summary["failed"] == processed else "partial_failed" if summary["failed"] else "succeeded"
            run.finished_at = utcnow()

    @staticmethod
    def _import_header(run):
        result = {column.name: getattr(run, column.name) for column in run.__table__.columns if column.name != "results_json"}
        if result["error_message"] is not None:
            error = stored_error(result["error_code"], result["error_message"])
            result["error_code"], result["error_message"] = error["error_code"], error["error"]
        return result

    @staticmethod
    def _item_page(session, import_id, limit, offset, status=None):
        statement = select(ImportRunItem).where(ImportRunItem.import_run_id == import_id)
        if status is not None:
            statement = statement.where(ImportRunItem.status == status)
        total = session.scalar(select(func.count()).select_from(statement.subquery()))
        items = []
        for item in session.scalars(statement.order_by(ImportRunItem.ordinal).offset(offset).limit(limit)):
            payload = dict(item.payload_json)
            if item.status == "failed":
                payload.update(stored_error(payload.get("error_code"), payload.get("error")))
            items.append(payload)
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    def show_import(self, import_id, *, limit=100, offset=0):
        validate_page(limit, offset)
        with self.sessions() as session:
            run = required(session, ImportRun, import_id)
            result = self._import_header(run)
            page = self._item_page(session, import_id, limit, offset)
            result["results_json"] = page.pop("items")
            result["results_page"] = page
            return result

    def import_items(self, import_id, *, limit=100, offset=0, status=None):
        validate_page(limit, offset)
        if status is not None and status not in ITEM_STATES:
            raise ManagementError("INVALID_STATUS_FILTER")
        with self.sessions() as session:
            required(session, ImportRun, import_id)
            return self._item_page(session, import_id, limit, offset, status)

    def _import_document(self, kb_id, payload, chunks, raw_file, snapshot, snapshot_dir, run_id, ordinal):
        path = relative_path(payload["source_path"])
        source_key = hashlib.sha256(path.encode()).hexdigest()
        with self.sessions.begin() as session:
            # Serialize one document's writes within this KB; concurrent imports may interleave between documents.
            required(session, KnowledgeBase, kb_id, lock=True)
            document = session.scalar(select(Document).where(Document.knowledge_base_id == kb_id,
                                                             Document.source_key == source_key).with_for_update())
            if document is None:
                document = Document(knowledge_base_id=kb_id, source_key=source_key, source_path=path,
                                    title=payload.get("title", Path(path).stem), category=payload.get("category", "uncategorized"))
                session.add(document)
                session.flush()
            version = session.scalar(select(DocumentVersion).where(DocumentVersion.document_id == document.id,
                                                                     DocumentVersion.source_sha256 == payload["sha256"]))
            if version is None:
                version = DocumentVersion(document_id=document.id, source_sha256=payload["sha256"],
                                          original_filename=Path(path).name, format=payload["format"],
                                          file_size=raw_file.stat().st_size, storage_uri=raw_file.as_posix())
                session.add(version)
                session.flush()
            build, reused = self.persist_build(session, version, payload, chunks, raw_file, snapshot, snapshot_dir)
            result = {"legacy_doc_id": payload["doc_id"], "source_path": path, "document_id": document.id,
                    "version_id": version.id, "build_id": build.id, "status": "reused" if reused else "imported",
                    "chunks": len(chunks), "eligible_chunks": sum(c["retrieval_enabled"] for c in chunks),
                    "missing_asset_refs": snapshot.missing_assets[payload["doc_id"]]}
            self._append_item(session, run_id, ordinal, result)

    @staticmethod
    def persist_build(session, version, payload, chunks, raw_file, snapshot, snapshot_dir, *, pipeline_config=None):
        """Shared build/chunk writer; caller owns the transaction and its success record."""
        output = fingerprint({"document": payload, "chunks": chunks})
        build = session.scalar(select(DocumentBuild).where(DocumentBuild.document_version_id == version.id,
                                                            DocumentBuild.output_fingerprint == output))
        reused = build is not None
        if build is None:
            config_keys = ("parser", "parser_adapter", "table_processing", "table_view_version", "schema_version")
            known = {key: payload[key] for key in config_keys if key in payload}
            build = DocumentBuild(document_version_id=version.id, legacy_doc_id=payload["doc_id"],
                                  output_fingerprint=output,
                                  pipeline_config_json=pipeline_config or {"origin": "existing_snapshot", "recorded": known,
                                                "unrecorded_parameters": "unknown"},
                                  document_payload_json=payload,
                                  artifact_manifest_json={"snapshot_uri": snapshot_dir.as_posix(),
                                                          "snapshot_fingerprint": snapshot.fingerprint,
                                                          "files_sha256": snapshot.hashes,
                                                          "missing_asset_refs": snapshot.missing_assets[payload["doc_id"]],
                                                          "raw_uri": raw_file.as_posix()},
                                  status="failed" if payload["status"] == "failed" else "ready" if chunks else "empty")
            session.add(build)
            session.flush()
            # Batch INSERT; original payload is kept in addition to indexed scalar columns.
            if chunks:
                session.execute(Chunk.__table__.insert(), [{"build_id": build.id, "legacy_chunk_id": c["chunk_id"],
                    "chunk_index": c["chunk_index"], "body": c["body"], "text": c["text"],
                    "token_count": c["token_count"], "structure_type": c.get("structure_type", c.get("content_type", "body")),
                    "retrieval_enabled": c["retrieval_enabled"], "payload_json": c} for c in chunks])
        return build, reused

    @staticmethod
    def _validate_activation(session, document, build):
        if build_document(session, build) != document.id:
            raise ManagementError('BUILD_OWNERSHIP')
        total = chunk_count(session, build.id)
        if build.status != "ready" or not total:
            raise ManagementError('BUILD_NOT_READY')
        if total != build.document_payload_json.get("chunk_count", total):
            raise ManagementError('BUILD_INCOMPLETE')

    def activate_document(self, document_id, build_id):
        with self.sessions.begin() as session:
            document = required(session, Document, document_id, lock=True)
            build = required(session, DocumentBuild, build_id)
            self._validate_activation(session, document, build)
            previous = document.active_build_id
            category = build.document_payload_json.get("category", document.category)
            changed = previous != build.id or document.category != category
            document.active_build_id = build.id
            document.title = build.document_payload_json.get("title", document.title)
            document.category = build.document_payload_json.get("category", document.category)
            from .index_sync import mark_dirty, state_row, target
            sync_target = (mark_dirty(session, document.knowledge_base_id) if changed else
                           target(state_row(session, document.knowledge_base_id)))
            return {"document_id": document.id, "previous_build_id": previous, "active_build_id": build.id,
                    "sync_target": sync_target}

    def activate_import(self, import_id):
        with self.sessions.begin() as session:
            run = required(session, ImportRun, import_id, lock=True)
            if run.status != "succeeded":
                raise ManagementError('IMPORT_NOT_COMPLETE')
            entries = session.execute(select(ImportRunItem.document_id, ImportRunItem.build_id, ImportRunItem.status)
                                      .where(ImportRunItem.import_run_id == import_id)
                                      .order_by(ImportRunItem.document_id, ImportRunItem.ordinal)).all()
            if (not entries or len(entries) != run.summary_json.get("documents")
                    or any(entry.status not in ("imported", "reused") or entry.document_id is None
                           or entry.build_id is None for entry in entries)
                    or len({entry.document_id for entry in entries}) != len(entries)):
                raise ManagementError("IMPORT_ITEM_INCOMPLETE")
            results = []
            changed = False
            for entry in entries:
                document = required(session, Document, entry.document_id, lock=True)
                if document.knowledge_base_id != run.knowledge_base_id:
                    raise ManagementError('IMPORT_OWNERSHIP')
                build = required(session, DocumentBuild, entry.build_id)
                self._validate_activation(session, document, build)
                results.append({"document_id": document.id, "previous_build_id": document.active_build_id,
                                "active_build_id": build.id})
                changed = changed or document.active_build_id != build.id or document.category != build.document_payload_json.get("category", document.category)
                document.active_build_id = build.id
                document.title = build.document_payload_json.get("title", document.title)
                document.category = build.document_payload_json.get("category", document.category)
            from .index_sync import mark_dirty, state_row, target
            sync_target = (mark_dirty(session, run.knowledge_base_id) if changed else
                           target(state_row(session, run.knowledge_base_id)))
            return {"import_id": import_id, "activated": len(results), "results": results, "sync_target": sync_target}
