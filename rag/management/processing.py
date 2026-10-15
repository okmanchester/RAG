"""Durable single-document processing. Models are loaded only by the worker."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import tempfile
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from .config import ROOT
from .errors import ManagementError, stored_error
from .models import Document, DocumentVersion, KnowledgeBase, ProcessingTask, ProcessingAttempt
from .repository import record, required
from .service import ManagementService, validate_page
from .storage import contained, file_hash, fingerprint, load_snapshot, publish_raw, relative_path

TASK_STATES = ("pending", "running", "succeeded", "failed")
STAGES = ("validation", "parsing", "chunking", "publishing", "persisting")
LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 15
HEARTBEAT_RETRY_DELAYS = (1, 2)
RETRY_DELAYS = (30, 120)


@contextmanager
def native_workspace(work):
    """Keep third-party temporary files and spawned PDF renderers inside the job workspace."""
    previous = tempfile.tempdir
    environment = {name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")}
    tempfile.tempdir = str(work)
    os.environ.update({name: str(work) for name in environment})
    try:
        yield
    finally:
        try:
            renderer = sys.modules.get("docvortex.document.pdf.render_session")
            if renderer is not None:
                renderer.shutdown_pdf_render_sessions()
        finally:
            tempfile.tempdir = previous
            for name, value in environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def processing_config(model_path=None, maximum=400, overlap=50, config_path=None):
    from rag.chunk import versions
    from rag.parsers import DEFAULT_CONFIG, load_settings, llm_options
    from rag.splitter import DEFAULT_MODEL
    from rag.table_llm import options
    if type(maximum) is not int or type(overlap) is not int or not 0 <= overlap < maximum:
        raise ManagementError("INVALID_PROCESSING_CONFIG")
    config_path = Path(config_path or DEFAULT_CONFIG).resolve()
    model = Path(model_path or DEFAULT_MODEL).resolve()
    try:
        settings = load_settings(config_path)
        # Whitelist effective non-secret values; never fingerprint/persist raw YAML or api_key.
        aided = llm_options(settings)
        if aided["base_url"] not in ("https://api.deepseek.com", "https://api.deepseek.com/v1"):
            raise ManagementError("INVALID_PROCESSING_CONFIG")
        native = Path(settings["model"]["base_dir"]).resolve()
        table = options(settings)
        if not model.is_dir() or not native.is_dir():
            raise ManagementError("PROCESSING_MODELS_MISSING")
        tokenizer = {p.name: file_hash(p) for p in sorted(model.iterdir()) if p.is_file() and
                     p.name in {"tokenizer.json", "tokenizer_config.json", "vocab.txt", "special_tokens_map.json",
                                "config.json", "sentence_bert_config.json"}}
        if not tokenizer:
            raise ManagementError("PROCESSING_MODELS_MISSING")
        # Large native weights use available file identity, not a multi-GB hash on every poll.
        native_files = {p.relative_to(native).as_posix(): {"size": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns}
                        for p in sorted(native.rglob("*")) if p.is_file() and p.suffix.lower() in
                        {".json", ".yaml", ".yml", ".txt", ".model", ".bin", ".safetensors", ".onnx", ".pth", ".pt", ".gguf"}}
        implementation = {name: file_hash(ROOT / "rag" / name) for name in
                          ("chunk.py", "parsers.py", "splitter.py", "structure.py", "tables.py", "table_llm.py", "retrieval.py",
                           "management/processing.py", "management/service.py", "management/storage.py",
                           "management/repository.py", "management/models.py", "management/config.py", "management/errors.py")}
        return {"format_version": 2, "config_path": config_path.as_posix(), "model_path": model.as_posix(),
                "max_tokens": maximum, "overlap_tokens": overlap, "page_range": "",
                "settings": {"model": {"base_dir": native.as_posix(), "source": "local", "small_backend": "torch",
                             "vlm": {"engine": "llama-cpp", "server_url": "", "max_concurrency": 1}},
                             "llm_aided": aided, "table_processing": table},
                "versions": versions(), "tokenizer_files_sha256": tokenizer,
                "native_model_files": native_files, "implementation_sha256": implementation}
    except ManagementError:
        raise
    except (OSError, KeyError, TypeError, ValueError):
        raise ManagementError("INVALID_PROCESSING_CONFIG") from None


def temporary_error(exc):
    """Classify by exception types/status codes, never by untrusted message text."""
    if isinstance(exc, DBAPIError):
        args = getattr(exc.orig, "args", ())
        return exc.connection_invalidated or bool(args and args[0] in (1205, 1213, 2002, 2003, 2006, 2013))
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    import urllib.error
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 429, 500, 502, 503, 504)
    if isinstance(exc, urllib.error.URLError):
        return True
    try:
        import httpx
        import openai
        if isinstance(exc, (httpx.TransportError, openai.APIConnectionError, openai.APITimeoutError)):
            return True
        if isinstance(exc, (httpx.HTTPStatusError, openai.APIStatusError)):
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else exc.status_code
            return status in (408, 429, 500, 502, 503, 504)
    except ImportError:
        pass
    return False


class ProcessingService:
    def __init__(self, management: ManagementService):
        self.management = management
        self.engine, self.sessions, self.storage_root = management.engine, management.sessions, management.storage_root

    @staticmethod
    def now(session):
        # Lease decisions use the database clock across processes/hosts.
        return session.scalar(select(func.utc_timestamp(6)))

    @staticmethod
    def active(session, document):
        kb = required(session, KnowledgeBase, document.knowledge_base_id)
        if kb.status != "active" or document.status != "active":
            raise ManagementError("RESOURCE_DISABLED")

    def _prepare_registration(self, raw_root, source_path):
        path = relative_path(source_path)
        source = contained(Path(raw_root), path)
        if Path(path).suffix.lower() not in (".pdf", ".docx"):
            raise ManagementError("INVALID_DOCUMENT_FORMAT")
        if not source.is_file():
            raise ManagementError("RAW_FILE_MISSING")
        sha = file_hash(source)
        raw = publish_raw(Path(raw_root), path, sha, self.storage_root)
        return path, sha, raw

    def register(self, kb_id, raw_root, source_path):
        prepared = self._prepare_registration(raw_root, source_path)
        with self.sessions.begin() as session:
            return self._register(session, kb_id, *prepared)

    def _register(self, session, kb_id, path, sha, raw):
        key = hashlib.sha256(path.encode()).hexdigest()
        kb = required(session, KnowledgeBase, kb_id, lock=True)
        if kb.status != "active":
            raise ManagementError("RESOURCE_DISABLED")
        doc = session.scalar(select(Document).where(Document.knowledge_base_id == kb_id,
                             Document.source_key == key).with_for_update())
        if doc is None:
            doc = Document(knowledge_base_id=kb_id, source_key=key, source_path=path, title=Path(path).stem,
                           category=Path(path).parts[0] if len(Path(path).parts) > 1 else "uncategorized")
            session.add(doc)
            session.flush()
        self.active(session, doc)
        version = session.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc.id,
                                  DocumentVersion.source_sha256 == sha))
        reused = version is not None
        if version is None:
            version = DocumentVersion(document_id=doc.id, source_sha256=sha, original_filename=Path(path).name,
                format=Path(path).suffix.lower()[1:], file_size=raw.stat().st_size, storage_uri=raw.as_posix())
            session.add(version)
            session.flush()
        return {"document_id": doc.id, "version_id": version.id, "source_sha256": sha,
                "reused": reused, "active_build_id": doc.active_build_id}

    def register_and_submit(self, kb_id, raw_root, source_path, **options):
        """Commit source registration and durable task creation together; files stay immutable."""
        config = processing_config(**options)
        prepared = self._prepare_registration(raw_root, source_path)
        with self.sessions.begin() as session:
            registration = self._register(session, kb_id, *prepared)
            task = self._submit(session, registration["version_id"], config)
            reused_version = registration.pop("reused")
            return {**registration, "reused_version": reused_version,
                    "task_id": task["task_id"], "status": task["status"], "build_id": task["build_id"],
                    "reused_task": task["reused"]}

    def submit(self, version_id, **options):
        config = processing_config(**options)
        with self.sessions.begin() as session:
            return self._submit(session, version_id, config)

    def _submit(self, session, version_id, config):
        identity = fingerprint(config)
        version = required(session, DocumentVersion, version_id, lock=True)
        self.active(session, required(session, Document, version.document_id))
        task = session.scalar(select(ProcessingTask).where(ProcessingTask.document_version_id == version.id,
                               ProcessingTask.config_sha256 == identity))
        reused = task is not None
        if task is None:
            task = ProcessingTask(document_version_id=version.id, config_json=config, config_sha256=identity,
                                  scheduled_at=self.now(session))
            session.add(task)
            session.flush()
        return {"task_id": task.id, "status": task.status, "build_id": task.build_id, "reused": reused}

    @staticmethod
    def public_task(task):
        result = record(task)
        result.pop("execution_token", None)
        result["error"] = stored_error(task.error_code, None)["error"] if task.error_code else None
        return result

    def show(self, task_id, limit=100, offset=0):
        validate_page(limit, offset)
        with self.sessions() as session:
            task = required(session, ProcessingTask, task_id)
            result = self.public_task(task)
            statement = select(ProcessingAttempt).where(ProcessingAttempt.task_id == task_id)
            total = session.scalar(select(func.count()).select_from(statement.subquery()))
            attempts = []
            for item in session.scalars(statement.order_by(ProcessingAttempt.ordinal).limit(limit).offset(offset)):
                entry = record(item)
                entry.pop("execution_token")
                entry["error"] = stored_error(item.error_code, None)["error"] if item.error_code else None
                attempts.append(entry)
            result["attempts"] = {"total": total, "limit": limit, "offset": offset, "items": attempts}
            if task.build_id is not None:
                from .models import DocumentBuild
                build = required(session, DocumentBuild, task.build_id)
                result["build_status"] = build.status
                result["can_activate"] = build.status == "ready" and build.document_payload_json["chunk_count"] > 0
            return result

    def list(self, kb_id, status=None, limit=100, offset=0):
        validate_page(limit, offset)
        if status is not None and status not in TASK_STATES:
            raise ManagementError("INVALID_STATUS_FILTER")
        with self.sessions() as session:
            required(session, KnowledgeBase, kb_id)
            statement = select(ProcessingTask).join(DocumentVersion).join(Document).where(Document.knowledge_base_id == kb_id)
            if status is not None:
                statement = statement.where(ProcessingTask.status == status)
            total = session.scalar(select(func.count()).select_from(statement.subquery()))
            return {"total": total, "limit": limit, "offset": offset,
                    "items": [self.public_task(t) for t in session.scalars(statement.order_by(ProcessingTask.id).limit(limit).offset(offset))]}

    def retry(self, task_id):
        with self.sessions.begin() as session:
            task = required(session, ProcessingTask, task_id, lock=True)
            if task.status != "failed":
                raise ManagementError("TASK_RETRY_NOT_ALLOWED")
            task.status, task.stage, task.error_code = "pending", "validation", None
            task.cycle_attempts, task.finished_at = 0, None
            task.scheduled_at = self.now(session)
            return {"task_id": task.id, "status": task.status}

    @staticmethod
    def attempt(session, task):
        return session.scalar(select(ProcessingAttempt).where(ProcessingAttempt.task_id == task.id,
                              ProcessingAttempt.ordinal == task.attempt_count))

    def claim(self):
        with self.sessions.begin() as session:
            now = self.now(session)
            expired = session.scalars(select(ProcessingTask).where(ProcessingTask.status == "running",
                       ProcessingTask.lease_expires_at <= now).order_by(ProcessingTask.id).limit(100).with_for_update(skip_locked=True))
            for task in expired:
                attempt = self.attempt(session, task)
                attempt.status, attempt.error_code, attempt.finished_at = "interrupted", "TASK_INTERRUPTED", now
                task.status, task.execution_token, task.lease_expires_at = "pending", None, None
                task.scheduled_at, task.error_code = now, "TASK_INTERRUPTED"
            session.flush()
            task = session.scalar(select(ProcessingTask).where(ProcessingTask.status == "pending",
                                  ProcessingTask.scheduled_at <= now).order_by(ProcessingTask.scheduled_at, ProcessingTask.id)
                                  .limit(1).with_for_update(skip_locked=True))
            if task is None:
                return None
            task.status, task.stage, task.error_code = "running", "validation", None
            task.execution_token = uuid.uuid4().hex
            task.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            task.attempt_count += 1
            task.cycle_attempts += 1
            session.add(ProcessingAttempt(task_id=task.id, ordinal=task.attempt_count,
                        execution_token=task.execution_token, started_at=now))
            session.flush()
            kb_id = session.scalar(select(Document.knowledge_base_id).join(DocumentVersion,
                DocumentVersion.document_id == Document.id).where(DocumentVersion.id == task.document_version_id))
            return {**record(task), "kb_id": kb_id}

    def owned(self, session, task_id, token):
        task = required(session, ProcessingTask, task_id, lock=True)
        if task.status != "running" or task.execution_token != token or task.lease_expires_at <= self.now(session):
            raise ManagementError("TASK_LEASE_LOST")
        return task

    def renew(self, task_id, token, stage=None):
        if stage is not None and stage not in STAGES:
            raise ManagementError("INVALID_PROCESSING_CONFIG")
        with self.sessions.begin() as session:
            task = self.owned(session, task_id, token)
            task.lease_expires_at = self.now(session) + timedelta(seconds=LEASE_SECONDS)
            if stage:
                task.stage = stage
                self.attempt(session, task).stage = stage

    def renew_heartbeat(self, task_id, token, stop):
        """Retry transient I/O; only the DB ownership check can confirm a lost lease."""
        for attempt in range(len(HEARTBEAT_RETRY_DELAYS) + 1):
            if stop.is_set():
                return False
            try:
                self.renew(task_id, token)
                return True
            except Exception as exc:
                if not temporary_error(exc) or attempt == len(HEARTBEAT_RETRY_DELAYS):
                    raise
                if stop.wait(HEARTBEAT_RETRY_DELAYS[attempt]):
                    return False

    def fail(self, task_id, token, exc):
        transient = temporary_error(exc)
        code = exc.code if isinstance(exc, ManagementError) else "PROCESSING_TEMPORARY" if transient else "PROCESSING_FAILED"
        with self.sessions.begin() as session:
            task = self.owned(session, task_id, token)
            now = self.now(session)
            attempt = self.attempt(session, task)
            attempt.status, attempt.error_code, attempt.finished_at = "failed", code, now
            if transient and task.cycle_attempts < 3:
                task.status = "pending"
                task.scheduled_at = now + timedelta(seconds=RETRY_DELAYS[task.cycle_attempts - 1])
            else:
                task.status, task.finished_at = "failed", now
            task.error_code, task.execution_token, task.lease_expires_at = code, None, None

    def complete(self, task_id, token, snapshot, snapshot_dir):
        with self.sessions.begin() as session:
            task = self.owned(session, task_id, token)
            version = required(session, DocumentVersion, task.document_version_id)
            document = required(session, Document, version.document_id, lock=True)
            payload = snapshot.documents[0]
            if (len(snapshot.documents) != 1 or payload["source_path"] != document.source_path or
                    payload["sha256"] != version.source_sha256 or payload["status"] not in ("ok", "empty") or
                    snapshot.missing_assets[payload["doc_id"]]):
                raise ManagementError("TASK_RESULT_MISMATCH")
            raw = Path(version.storage_uri)
            if file_hash(raw) != version.source_sha256:
                raise ManagementError("STORED_RAW_CHECKSUM")
            build, _ = self.management.persist_build(session, version, payload, snapshot.chunks_by_doc[payload["doc_id"]],
                raw, snapshot, snapshot_dir, pipeline_config={"origin": "managed_task", "recorded": task.config_json,
                                                             "config_sha256": task.config_sha256})
            # Recheck clock after validation/writes; publication belongs to a live owner.
            if task.lease_expires_at <= self.now(session):
                raise ManagementError("TASK_LEASE_LOST")
            task.build_id, task.status, task.finished_at = build.id, "succeeded", self.now(session)
            task.execution_token, task.lease_expires_at, task.error_code = None, None, None
            attempt = self.attempt(session, task)
            attempt.status, attempt.finished_at = "succeeded", task.finished_at
            session.flush()

    @contextmanager
    def worker_lock(self):
        with self.engine.connect() as connection:
            database = connection.scalar(text("SELECT DATABASE()"))
            name = "rag-worker:" + hashlib.sha256(database.encode()).hexdigest()[:48]
            owner = connection.scalar(text("SELECT CONNECTION_ID()"))
            if connection.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": name}) != 1:
                raise ManagementError("WORKER_BUSY")
            def check():
                if connection.scalar(text("SELECT IS_USED_LOCK(:name)"), {"name": name}) != owner:
                    raise ManagementError("TASK_LEASE_LOST")
            try:
                yield check
            finally:
                if not connection.invalidated:
                    connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": name})

    def execute(self, task, lock_check=lambda: None):
        task_id, token = task["id"], task["execution_token"]
        stop, lost = threading.Event(), threading.Event()
        heartbeat_errors = []
        def heartbeat():
            while not stop.wait(HEARTBEAT_SECONDS):
                try:
                    if not self.renew_heartbeat(task_id, token, stop):
                        return
                except Exception as exc:
                    if isinstance(exc, ManagementError) and exc.code == "TASK_LEASE_LOST":
                        lost.set()
                    else:
                        heartbeat_errors.append(exc)
                    return
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        def stage(value):
            if lost.is_set():
                raise ManagementError("TASK_LEASE_LOST")
            if heartbeat_errors:
                raise heartbeat_errors[0]
            lock_check()
            self.renew(task_id, token, value)
        try:
            stage("validation")
            self.process(task, stage)
        except Exception as exc:
            lease_lost = isinstance(exc, ManagementError) and exc.code == "TASK_LEASE_LOST"
            if not lost.is_set() and not lease_lost:
                try:
                    self.fail(task_id, token, exc)
                except ManagementError as failure:
                    if failure.code != "TASK_LEASE_LOST":
                        raise
        finally:
            stop.set()
            thread.join(timeout=HEARTBEAT_SECONDS + 1)
        return self.show(task_id)

    def process(self, task, stage):
        from rag.chunk import process_document
        from rag.splitter import TokenCounter
        config = task["config_json"]
        current = processing_config(config["model_path"], config["max_tokens"], config["overlap_tokens"], config["config_path"])
        if fingerprint(current) != task["config_sha256"]:
            raise ManagementError("PROCESSING_IDENTITY_CHANGED")
        settings = config["settings"]
        if (any(settings["llm_aided"]["features"].values()) or settings["table_processing"]["llm_enabled"]) and not os.environ.get("DEEPSEEK_API_KEY", "").strip():
            raise ManagementError("PROCESSING_KEY_REQUIRED")
        with self.sessions() as session:
            version = required(session, DocumentVersion, task["document_version_id"])
            document = required(session, Document, version.document_id)
            self.active(session, document)
            raw, relative, sha = Path(version.storage_uri), document.source_path, version.source_sha256
        if file_hash(raw) != sha:
            raise ManagementError("STORED_RAW_CHECKSUM")
        root = ROOT / "tmp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="processing-", dir=root) as temporary:
            work = Path(temporary)
            input_path = contained(work / "input", relative)
            input_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(raw, input_path)
            if file_hash(input_path) != sha:
                raise ManagementError("RAW_CHECKSUM")
            # Execute the whitelisted snapshot, never a mutable config read after identity validation.
            effective_config = work / "mineru.yaml"
            native_settings = {**settings, "llm_aided": {**settings["llm_aided"], "api_key": "${DEEPSEEK_API_KEY:-}"}}
            effective_config.write_text(json.dumps(native_settings, ensure_ascii=False), encoding="utf-8")
            output = work / "output"
            output.mkdir()
            with native_workspace(work):
                payload, chunks = process_document(input_path, relative, output, TokenCounter(config["model_path"]),
                    config["max_tokens"], config["overlap_tokens"], config_path=effective_config, stage=stage)
            (output / "documents.jsonl").write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
            (output / "chunks.jsonl").write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks), encoding="utf-8", newline="\n")
            (output / "report.json").write_text(json.dumps({"artifacts_sha256": {
                name: file_hash(output / name) for name in ("documents.jsonl", "chunks.jsonl")}}), encoding="utf-8")
            stage("publishing")
            snapshot = load_snapshot(output)
            if any(snapshot.missing_assets.values()):
                raise ManagementError("MISSING_DECLARED_ASSET")
            published = snapshot.publish(self.storage_root)
            stage("persisting")
            self.complete(task["id"], task["execution_token"], snapshot, published)

    def run(self, once=False):
        """Compatibility entrypoint: processing only, using the shared scheduler."""
        from .worker import run_worker
        result = run_worker(self.management, once=once, kind="processing", _processing=self)
        result.pop("kind", None)
        return result
