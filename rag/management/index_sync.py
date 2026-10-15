"""Transactional, coalesced search projection queue. No ES/model imports at submission."""
from __future__ import annotations

from datetime import timedelta
from contextlib import nullcontext
import threading
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from .errors import ManagementError, public_error, stored_error
from .models import IndexSyncState, IndexSyncAttempt, KnowledgeBase
from .repository import record, required

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 15
HEARTBEAT_RETRY_DELAYS = (1, 2)
RETRY_DELAYS = (30, 120)
DEBOUNCE_SECONDS = 5


def database_now(session):
    return session.scalar(select(func.utc_timestamp(6)))


def state_row(session, kb_id, *, lock=False):
    statement = select(IndexSyncState).where(IndexSyncState.knowledge_base_id == kb_id)
    row = session.scalar(statement.with_for_update() if lock else statement)
    if row is None:
        raise ManagementError("RECORD_NOT_FOUND")
    return row


def public_record(item):
    value = record(item)
    value.pop("execution_token", None)
    if value.get("error_code"):
        value.update(stored_error(value["error_code"], None))
    return value


def target(row):
    return {"kb_id": row.knowledge_base_id, "desired_revision": row.desired_revision,
            "status": row.status, "paused": row.paused}


def initialize(session, kb_id):
    row = IndexSyncState(knowledge_base_id=kb_id,
                         scheduled_at=database_now(session) + timedelta(seconds=DEBOUNCE_SECONDS))
    session.add(row)
    session.flush()
    return target(row)


def mark_dirty(session, kb_id):
    """Caller has already changed authority in this same transaction."""
    row = state_row(session, kb_id, lock=True)
    row.desired_revision += 1
    if row.status != "running":
        if row.status != "pending":
            row.scheduled_at = database_now(session) + timedelta(seconds=DEBOUNCE_SECONDS)
        row.status, row.cycle_attempts, row.error_code, row.finished_at = "pending", 0, None, None
    return target(row)


class IndexSyncService:
    def __init__(self, engine):
        self.engine = engine
        self.sessions = sessionmaker(engine, expire_on_commit=False)

    def show(self, kb_id, *, limit=100, offset=0):
        from .service import validate_page
        validate_page(limit, offset)
        with self.sessions() as session:
            row = state_row(session, kb_id)
            statement = select(IndexSyncAttempt).where(IndexSyncAttempt.knowledge_base_id == kb_id)
            total = session.scalar(select(func.count()).select_from(statement.subquery()))
            attempts = session.scalars(statement.order_by(IndexSyncAttempt.ordinal.desc()).limit(limit).offset(offset))
            return {"kb_id": kb_id, "sync_task": public_record(row),
                    "attempts": {"total": total, "limit": limit, "offset": offset,
                                 "items": [public_record(a) for a in attempts]}}

    def control(self, kb_id, action, *, invalidate=False):
        if action not in ("pause", "resume", "retry"):
            raise ManagementError("INVALID_RESOURCE")
        with self.sessions.begin() as session:
            row = state_row(session, kb_id, lock=True)
            if action == "pause":
                row.paused = True
                if invalidate:
                    mark_dirty(session, kb_id)
            else:
                if action == "retry" and row.status != "failed":
                    raise ManagementError("INDEX_SYNC_NOT_FAILED")
                if action == "resume":
                    row.paused = False
                if row.status != "running":
                    row.status, row.cycle_attempts, row.error_code, row.finished_at = "pending", 0, None, None
                row.scheduled_at = database_now(session)
            session.flush()
            return {"kb_id": kb_id, "sync_task": public_record(row)}

    @staticmethod
    def attempt(session, row):
        return session.scalar(select(IndexSyncAttempt).where(IndexSyncAttempt.knowledge_base_id == row.knowledge_base_id,
                              IndexSyncAttempt.ordinal == row.attempt_count))

    def claim(self, kb_id=None, *, manual=False):
        with self.sessions.begin() as session:
            now = database_now(session)
            expired_query = select(IndexSyncState).where(IndexSyncState.status == "running",
                                IndexSyncState.lease_expires_at <= now)
            if kb_id is not None:
                expired_query = expired_query.where(IndexSyncState.knowledge_base_id == kb_id)
            for row in session.scalars(expired_query.order_by(IndexSyncState.knowledge_base_id)
                                       .limit(100).with_for_update(skip_locked=True)):
                attempt = self.attempt(session, row)
                attempt.status, attempt.error_code, attempt.finished_at = "interrupted", "INDEX_SYNC_INTERRUPTED", now
                row.status, row.execution_token, row.lease_expires_at = "pending", None, None
                row.scheduled_at, row.error_code = now, "INDEX_SYNC_INTERRUPTED"
                row.cycle_attempts = max(0, row.cycle_attempts - 1)
            session.flush()
            if manual:
                row = state_row(session, kb_id, lock=True)
                if row.status == "running":
                    raise ManagementError("INDEX_BUSY")
                row.cycle_attempts = 0
            else:
                query = select(IndexSyncState).where(IndexSyncState.status == "pending", IndexSyncState.paused.is_(False),
                                                      IndexSyncState.scheduled_at <= now)
                if kb_id is not None:
                    query = query.where(IndexSyncState.knowledge_base_id == kb_id)
                row = session.scalar(query.order_by(IndexSyncState.scheduled_at, IndexSyncState.knowledge_base_id)
                                     .limit(1).with_for_update(skip_locked=True))
                if row is None:
                    return None
            row.status, row.error_code, row.finished_at = "running", None, None
            row.execution_token = uuid.uuid4().hex
            row.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            row.attempt_count += 1
            row.cycle_attempts += 1
            session.add(IndexSyncAttempt(knowledge_base_id=row.knowledge_base_id, ordinal=row.attempt_count,
                target_revision=row.desired_revision, execution_token=row.execution_token, manual=manual, started_at=now))
            session.flush()
            return {**record(row), "manual": manual}

    def owned(self, session, task):
        row = state_row(session, task["knowledge_base_id"], lock=True)
        if (row.status != "running" or row.execution_token != task["execution_token"] or
                row.lease_expires_at <= database_now(session)):
            raise ManagementError("INDEX_SYNC_LEASE_LOST")
        return row

    def renew(self, task):
        with self.sessions.begin() as session:
            row = self.owned(session, task)
            row.lease_expires_at = database_now(session) + timedelta(seconds=LEASE_SECONDS)

    def snapshot(self, search, task):
        # Plain state and corpus reads share the SAME RR snapshot. Locking state reads
        # would observe latest values rather than that snapshot on MySQL.
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                state = connection.execute(select(IndexSyncState.__table__).where(
                    IndexSyncState.knowledge_base_id == task["knowledge_base_id"])).mappings().one()
                if (state["status"] != "running" or state["execution_token"] != task["execution_token"] or
                        state["lease_expires_at"] <= database_now(connection)):
                    raise ManagementError("INDEX_SYNC_LEASE_LOST")
                selection = search.corpus.selection(task["knowledge_base_id"],
                    allow_disabled=not task["manual"], connection=connection)
                revision = state["desired_revision"]
        with self.sessions.begin() as session:
            row = self.owned(session, task)
            self.attempt(session, row).target_revision = revision
        return selection, revision

    def fail(self, task, exc):
        from .processing import temporary_error
        code = public_error(exc)["error_code"]
        with self.sessions.begin() as session:
            row = self.owned(session, task)
            attempt = self.attempt(session, row)
            now = database_now(session)
            newer = row.desired_revision != attempt.target_revision
            deferred = code == "INDEX_BUSY"
            # A KB disabled after manual preflight still needs its automatic empty
            # projection. Do not turn that pending target into a permanent failure.
            superseded = (code == "INDEX_SYNC_SUPERSEDED" or
                          task["manual"] and code == "RESOURCE_DISABLED")
            attempt.status = "deferred" if deferred else "superseded" if superseded else "failed"
            attempt.error_code, attempt.finished_at = code, now
            row.error_code = code
            if deferred or superseded or newer:
                row.status = "pending"
                row.cycle_attempts = 0 if newer or superseded else max(0, row.cycle_attempts - 1)
                row.scheduled_at = now + timedelta(seconds=DEBOUNCE_SECONDS if deferred else 0)
            elif (getattr(exc, "retryable", False) or temporary_error(exc)) and row.cycle_attempts <= len(RETRY_DELAYS):
                row.status = "pending"
                row.scheduled_at = now + timedelta(seconds=RETRY_DELAYS[row.cycle_attempts - 1])
            else:
                row.status, row.finished_at = "failed", now
            row.execution_token, row.lease_expires_at = None, None
            session.flush()
            return {"kind": "index", "kb_id": row.knowledge_base_id, **public_record(row)}

    def execute(self, task, search_factory, lock_check=lambda: None, *, force=False, propagate=False, index_check=None):
        stop = threading.Event()
        heartbeat_errors = []

        def heartbeat():
            from .processing import temporary_error
            while not stop.wait(HEARTBEAT_SECONDS):
                for retry in range(len(HEARTBEAT_RETRY_DELAYS) + 1):
                    try:
                        self.renew(task)
                        break
                    except Exception as exc:
                        if retry == len(HEARTBEAT_RETRY_DELAYS) or not temporary_error(exc):
                            heartbeat_errors.append(exc)
                            return
                        if stop.wait(HEARTBEAT_RETRY_DELAYS[retry]):
                            return

        def check():
            if heartbeat_errors:
                raise heartbeat_errors[0]
            try:
                lock_check()
            except ManagementError as exc:
                if exc.code == "TASK_LEASE_LOST":
                    raise ManagementError("INDEX_SYNC_LEASE_LOST") from None
                raise

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        completed = False
        try:
            check()
            search = search_factory()
            with (nullcontext(index_check) if index_check else search.corpus.index_lock(task["knowledge_base_id"])) as index_check:
                def guarded_check():
                    check()
                    index_check()

                selection, revision = self.snapshot(search, task)
                from rag.search.elastic import RETRIEVAL_CONFIG
                from rag.search.shared import digest
                index = search.store.current(selection.kb_id)
                meta = search.store.metadata(index) if index else None
                matches = (meta is not None and meta.get("state") == "ready" and
                    meta.get("selection_sha256") == selection.fingerprint and
                    meta.get("model_sha256") == digest(search.encoder.identity) and
                    meta.get("retrieval_sha256") == digest(RETRIEVAL_CONFIG))
                if matches and not force:
                    search.store.validate(selection.kb_id, index)
                else:
                    def candidate(metadata):
                        with self.sessions.begin() as session:
                            row = self.owned(session, task)
                            self.attempt(session, row).generation = metadata["generation"]
                        metadata["sync_revision"] = revision
                    index, meta = search.build(selection, guarded_check, candidate)
                    search.store.validate(selection.kb_id, index)
                guarded_check()
                # Only the publication boundary holds the queue row, never inference.
                # Management writes cannot commit a new revision between this check
                # and the alias operation; a crash after alias commit is reconciled above.
                with self.sessions.begin() as session:
                    row = self.owned(session, task)
                    if row.desired_revision != revision or (row.paused and not task["manual"]):
                        raise ManagementError("INDEX_SYNC_SUPERSEDED")
                    guarded_check()
                    search.store.publish(selection.kb_id, index, validated=True)
                    guarded_check()
                    self.owned(session, task)
                    attempt = self.attempt(session, row)
                    attempt.status, attempt.generation = "succeeded", meta["generation"]
                    attempt.finished_at = database_now(session)
                    row.applied_revision, row.generation = revision, meta["generation"]
                    row.status, row.error_code, row.finished_at = "succeeded", None, attempt.finished_at
                    row.execution_token, row.lease_expires_at, row.cycle_attempts = None, None, 0
                    session.flush()
                    result = public_record(row)
                completed = True
            # A management commit immediately following publication is visible here;
            # it remains pending instead of being reported as the latest completed target.
            with self.sessions() as session:
                result = public_record(state_row(session, selection.kb_id))
            return {"kind": "index", "kb_id": selection.kb_id, **result, "synced_generation": meta["generation"]}
        except Exception as exc:
            if completed:
                raise
            # A lost lease cannot write a failure over its successor's task.
            if isinstance(exc, ManagementError) and exc.code == "INDEX_SYNC_LEASE_LOST":
                raise
            result = self.fail(task, exc)
            if propagate:
                raise
            return result
        finally:
            stop.set()
            thread.join()

    def sync(self, search, kb_id):
        # Take the per-KB lock before a manual claim and pass its checked ownership
        # into execute, rather than trying to acquire it on another connection.
        with search.corpus.index_lock(kb_id) as check:
            # Preserve the manual entrypoint's active-KB contract before claiming
            # anything, so a rejected request cannot consume the automatic queue.
            search.corpus.selection(kb_id)
            task = self.claim(kb_id, manual=True)
            return self.execute(task, lambda: search, check, force=True, propagate=True, index_check=check)
