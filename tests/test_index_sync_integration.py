"""Opt-in automatic generation publication and recovery with real MySQL/ES."""
import copy
import json
import os
from pathlib import Path
import time
import threading
import unittest
import uuid
from unittest.mock import patch

from rag.management.errors import ManagementError
from rag.management.worker import run_worker
from rag.search.config import SearchSettings
from rag.search.service import SearchService
from rag.search.shared import SearchFilters
from test_index_sync_mysql import IndexFixture
from test_search_integration import FixtureEncoder
from test_management_storage import write_corpus


@unittest.skipUnless(os.environ.get("RAG_RUN_MYSQL_TESTS") == "1" and os.environ.get("RAG_RUN_ES_TESTS") == "1",
                     "enable disposable MySQL/ES tests")
class IndexSyncIntegrationTests(IndexFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.client = SearchSettings.from_env().client()
            cls.client.info()
            cls.search = SearchService(cls.engine, cls.client, encoder=FixtureEncoder(), prefix="rag-test")
        except BaseException:
            super().tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            pattern = f"rag-test-kb-{cls.search.corpus.namespace}-*"
            for name in cls.client.indices.get_mapping(index=pattern, allow_no_indices=True):
                if not name.startswith(pattern[:-1]) or "-g-" not in name:
                    raise RuntimeError("outside isolated namespace")
                cls.client.indices.delete(index=name)
            cls.client.close()
        finally:
            super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.service.activate_document(self.document, self.build)
        self.filters = SearchFilters(self.kb)

    def auto(self):
        task = self.claim()
        self.assertIsNotNone(task)
        return self.indexing.execute(task, lambda: self.search)

    def query(self):
        return self.search.query("正文内容", filters=self.filters)

    def new_build(self):
        docs, chunks = copy.deepcopy(self.docs), copy.deepcopy(self.chunks)
        chunks[0].update(chunk_id="new-legacy", text="新版\n正文内容")
        write_corpus(self.corpus, documents=docs, chunks=chunks)
        run = self.service.import_corpus(self.kb, self.corpus, self.raw)
        return run["results_json"][0]["build_id"]

    def test_activation_worker_and_search_new_version(self):
        self.auto()
        new = self.new_build()
        self.service.activate_document(self.document, new)
        self.assertEqual(self.query()["contexts"], [])
        self.due()
        with patch("rag.search.service.SearchService", return_value=self.search):
            result = run_worker(self.service, kind="all", once=True)
        self.assertEqual((result["kind"], result["status"]), ("index", "succeeded"))
        self.assertEqual(self.query()["contexts"][0]["build_id"], new)
        self.assertFalse(self.query()["sync_lag"])

    def test_duplicate_reconciliation_does_not_reembed_or_create_generation(self):
        first = self.auto()
        self.indexing.control(self.kb, "resume")
        with patch.object(self.search.encoder, "documents", side_effect=AssertionError("embedding repeated")):
            second = self.auto()
        self.assertEqual(first["synced_generation"], second["synced_generation"])
        self.assertEqual(len(self.search.status(self.kb)["generations"]), 1)

    def test_crash_after_alias_publication_before_state_commit_is_reconciled(self):
        from sqlalchemy.orm import Session
        # Fail the final DB commit once, AFTER the alias was confirmed. Failure
        # recording works, so the interrupted cross-system boundary is observable.
        broken = []
        # sessionmaker.begin commits via SessionTransaction rather than Session.commit.
        from sqlalchemy import event
        def before_commit(session):
            if not broken and any(getattr(row, "status", None) == "succeeded" for row in session.identity_map.values()):
                broken.append(True)
                raise ConnectionError("interrupted commit")
        event.listen(Session, "before_commit", before_commit)
        try:
            result = self.auto()
        finally:
            event.remove(Session, "before_commit", before_commit)
        self.assertTrue(broken)
        self.assertEqual(result["status"], "pending")
        published = self.search.store.current(self.kb)
        with patch.object(self.search.encoder, "documents", side_effect=AssertionError("embedding repeated")):
            result = self.auto()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.search.store.current(self.kb), published)

    def test_changed_target_during_build_is_not_published(self):
        first = self.auto()
        new = self.new_build()
        self.service.activate_document(self.document, new)
        real = self.search.encoder.documents
        def change(texts):
            self.service.activate_document(self.document, self.build)
            return real(texts)
        with patch.object(self.search.encoder, "documents", side_effect=change):
            result = self.auto()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.search.status(self.kb)["generation"], first["synced_generation"])
        self.assertEqual(self.indexing.show(self.kb)["attempts"]["items"][0]["status"], "superseded")
        self.assertEqual(self.auto()["status"], "succeeded")
        self.assertEqual(self.query()["contexts"][0]["build_id"], self.build)

    def test_pause_during_build_prevents_publication_until_resume(self):
        first = self.auto()
        self.service.activate_document(self.document, self.new_build())
        real = self.search.encoder.documents
        def pause(texts):
            self.indexing.control(self.kb, "pause")
            return real(texts)
        with patch.object(self.search.encoder, "documents", side_effect=pause):
            result = self.auto()
        self.assertTrue(result["paused"])
        self.assertEqual(self.search.status(self.kb)["generation"], first["synced_generation"])
        self.assertIsNone(self.indexing.claim(self.kb))
        self.indexing.control(self.kb, "resume")
        self.assertEqual(self.auto()["status"], "succeeded")

    def test_disable_kb_publishes_empty_and_reenable_restores(self):
        self.auto()
        self.service.set_status("kb", self.kb, "disabled")
        self.assertEqual(self.auto()["status"], "succeeded")
        status = self.search.status(self.kb)
        self.assertEqual(status["chunk_count"], 0)
        self.assertFalse(status["sync_lag"])
        with self.assertRaises(ManagementError):
            self.query()
        self.service.set_status("kb", self.kb, "active")
        self.auto()
        self.assertEqual(len(self.query()["contexts"]), 1)

    def test_rollback_pauses_even_when_alias_publication_fails(self):
        first = self.auto()
        self.service.activate_document(self.document, self.new_build())
        self.auto()
        with patch.object(self.search.store, "publish", side_effect=ManagementError("INDEX_PUBLISH_FAILED")):
            with self.assertRaises(ManagementError):
                self.search.rollback(self.kb, first["synced_generation"])
        self.assertTrue(self.state()["paused"])

    def test_index_rollback_survives_management_change_until_explicit_resume(self):
        first = self.auto()
        new = self.new_build()
        self.service.activate_document(self.document, new)
        self.auto()
        self.search.rollback(self.kb, first["synced_generation"])
        self.service.set_status("document", self.document, "disabled")
        self.service.set_status("document", self.document, "active")
        self.due()
        self.assertIsNone(self.indexing.claim(self.kb))
        self.assertTrue(self.query()["sync_lag"])
        self.assertEqual(self.query()["contexts"], [])
        self.indexing.control(self.kb, "resume")
        self.auto()
        self.assertEqual(self.query()["contexts"][0]["build_id"], new)

    def test_disabled_kb_invalid_current_build_does_not_bypass_target_validation(self):
        from rag.management.models import DocumentBuild
        first = self.auto()
        new = self.new_build()
        self.service.activate_document(self.document, new)
        self.auto()
        self.service.set_status("kb", self.kb, "disabled")
        with self.service.sessions.begin() as session:
            session.get(DocumentBuild, new).status = "failed"
        # Invalid hidden current material does not block removing its projection.
        self.assertEqual(self.auto()["status"], "succeeded")
        self.assertEqual(self.search.status(self.kb)["chunk_count"], 0)
        with patch.object(self.search.store, "validate", wraps=self.search.store.validate) as validate:
            status = self.search.rollback(self.kb, first["synced_generation"])
        validate.assert_called_once_with(self.kb, self.search.store.generation_name(self.kb, first["synced_generation"]))
        self.assertEqual(status["generation"], first["synced_generation"])
        self.assertTrue(status["sync_task"]["paused"])
        with self.assertRaises(ManagementError) as caught:
            self.query()
        self.assertEqual(caught.exception.code, "RESOURCE_DISABLED")

    def test_disabled_or_archived_kb_rollback_rejects_corrupt_target_before_pausing(self):
        first = self.auto()
        chunk_id = self.query()["contexts"][0]["mysql_chunk_id"]
        self.service.activate_document(self.document, self.new_build())
        self.auto()
        target = self.search.store.generation_name(self.kb, first["synced_generation"])
        # Restore read-only after tampering, so rejection exercises actual content
        # verification rather than just the write-block check.
        self.client.indices.put_settings(index=target, settings={"index.blocks.write": False})
        self.client.update(index=target, id=str(chunk_id), doc={"text": "corrupted target"})
        self.client.indices.put_settings(index=target, settings={"index.blocks.write": True})
        for status in ("disabled", "archived"):
            with self.subTest(status=status):
                self.service.set_status("kb", self.kb, status)
                queue = self.state()
                index = self.search.store.current(self.kb)
                with self.assertRaises(ManagementError) as caught:
                    self.search.rollback(self.kb, first["synced_generation"])
                self.assertEqual(caught.exception.code, "INDEX_CORRUPT")
                self.assertEqual(self.search.store.current(self.kb), index)
                self.assertEqual(self.state(), queue)
                self.assertFalse(self.state()["paused"])

    def test_manual_sync_preserves_pause_and_updates_queue_progress(self):
        self.indexing.control(self.kb, "pause")
        result = self.search.sync(self.kb)
        self.assertTrue(result["sync_task"]["paused"])
        self.assertEqual(result["sync_task"]["applied_revision"], result["sync_task"]["desired_revision"])

    def test_manual_sync_rejects_disabled_and_archived_kb_without_consuming_queue(self):
        self.auto()
        for status in ("disabled", "archived"):
            with self.subTest(status=status):
                self.service.set_status("kb", self.kb, status)
                before = self.state()
                index = self.search.store.current(self.kb)
                generations = self.search.store.generations(self.kb)
                with patch.object(self.search, "build", side_effect=AssertionError("manual build")), \
                        patch.object(self.search.store, "publish", side_effect=AssertionError("manual publication")):
                    with self.assertRaises(ManagementError) as caught:
                        self.search.sync(self.kb)
                self.assertEqual(caught.exception.code, "RESOURCE_DISABLED")
                self.assertEqual(self.state(), before)
                self.assertEqual(self.search.store.current(self.kb), index)
                self.assertEqual(self.search.store.generations(self.kb), generations)
                self.assertEqual(self.auto()["status"], "succeeded")
                self.assertEqual(self.search.status(self.kb)["chunk_count"], 0)
                self.service.set_status("kb", self.kb, "active")
                self.auto()

    def test_manual_sync_rejects_disabled_kb_when_index_is_missing(self):
        self.service.set_status("kb", self.kb, "disabled")
        before = self.state()
        with self.assertRaises(ManagementError) as caught:
            self.search.sync(self.kb)
        self.assertEqual(caught.exception.code, "RESOURCE_DISABLED")
        self.assertEqual(self.state(), before)
        self.assertIsNone(self.search.store.current(self.kb))
        self.assertEqual(self.search.store.generations(self.kb), [])

    def test_kb_disabled_after_manual_preflight_keeps_automatic_sync_pending(self):
        first = self.auto()
        selection = self.search.corpus.selection
        def disable_after_preflight(kb_id, **kwargs):
            result = selection(kb_id, **kwargs)
            if kwargs.get("connection") is None and not kwargs.get("allow_disabled", False):
                self.service.set_status("kb", kb_id, "disabled")
            return result
        with patch.object(self.search.corpus, "selection", side_effect=disable_after_preflight):
            with self.assertRaises(ManagementError) as caught:
                self.search.sync(self.kb)
        self.assertEqual(caught.exception.code, "RESOURCE_DISABLED")
        self.assertEqual(self.state()["status"], "pending")
        self.assertEqual(self.state()["cycle_attempts"], 0)
        self.assertEqual(self.search.status(self.kb)["generation"], first["synced_generation"])
        attempt = self.indexing.show(self.kb)["attempts"]["items"][0]
        self.assertEqual((attempt["manual"], attempt["status"]), (True, "superseded"))
        self.assertIsNone(attempt["generation"])
        self.assertEqual(self.auto()["status"], "succeeded")
        self.assertEqual(self.search.status(self.kb)["chunk_count"], 0)

    def test_model_and_retrieval_fingerprints_prevent_incorrect_reuse(self):
        first = self.auto()
        self.indexing.control(self.kb, "resume")
        changed = {**self.search.encoder.identity, "model": "changed-model"}
        with patch.object(self.search.encoder, "identity", changed):
            second = self.auto()
        self.assertNotEqual(first["synced_generation"], second["synced_generation"])

    def test_lost_lease_after_embedding_cannot_publish(self):
        from sqlalchemy import text
        self.auto()
        self.service.activate_document(self.document, self.new_build())
        first_index = self.search.store.current(self.kb)
        real = self.search.encoder.documents
        def expire(texts):
            with self.engine.begin() as connection:
                connection.execute(text("UPDATE index_sync_states SET lease_expires_at=UTC_TIMESTAMP(6) "
                                        "WHERE knowledge_base_id=:kb"), {"kb": self.kb})
            return real(texts)
        with patch.object(self.search.encoder, "documents", side_effect=expire):
            with self.assertRaises(ManagementError) as caught:
                self.auto()
        self.assertEqual(caught.exception.code, "INDEX_SYNC_LEASE_LOST")
        self.assertEqual(self.search.store.current(self.kb), first_index)
        self.assertEqual(self.auto()["status"], "succeeded")
        self.assertEqual(self.indexing.show(self.kb)["attempts"]["items"][1]["status"], "interrupted")

    @unittest.skipUnless(os.environ.get("RAG_RUN_BGE_TESTS") == "1", "enable local BGE acceptance separately")
    def test_local_bge_real_acceptance(self):
        from rag.management.config import ROOT
        from rag.search.shared import BGEEncoder
        started = time.monotonic()
        self.search = SearchService(self.engine, self.client, encoder=BGEEncoder(), prefix="rag-test")
        first = self.auto()
        new = self.new_build()
        self.service.activate_document(self.document, new)
        self.assertEqual(self.query()["contexts"], [])
        self.due()
        with patch("rag.search.service.SearchService", return_value=self.search):
            result = run_worker(self.service, once=True)
        self.assertEqual(result["status"], "succeeded")
        hit = self.query()
        self.assertEqual(hit["contexts"][0]["build_id"], new)
        self.assertFalse(hit["sync_lag"])
        self.assertEqual(self.search.encoder.identity["dimensions"], 1024)
        self.indexing.control(self.kb, "resume")
        with patch.object(self.search.encoder, "documents", side_effect=AssertionError("repeated embedding")):
            reconciled = self.auto()
        self.assertEqual(reconciled["synced_generation"], result["synced_generation"])
        output = ROOT / "tmp" / ("index_sync_bge_acceptance_" + uuid.uuid4().hex[:12])
        output.mkdir()
        report = {"status": "passed", "fixture": "synthetic single-document, one eligible chunk per build",
                  "services": "isolated real MySQL and Elasticsearch", "dimensions": 1024,
                  "model": self.search.encoder.identity, "old_build_id": self.build, "new_build_id": new,
                  "first_generation": first["synced_generation"], "new_generation": result["synced_generation"],
                  "sync_lag": hit["sync_lag"], "worker_kind": result["kind"],
                  "reconciliation_without_embedding": True, "elapsed_seconds": time.monotonic() - started}
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("BGE acceptance report: " + str(output / "report.json"))

    def heartbeat_fault(self, exception, *, recover=False):
        armed, observed = threading.Event(), threading.Event()
        thread = []
        renewal = self.indexing.renew
        calls = []
        def renew(task):
            if not armed.is_set():
                return renewal(task)
            calls.append(True)
            thread[:] = [threading.current_thread()]
            if len(calls) == 1 or not recover:
                if isinstance(exception, ManagementError) and exception.code == "INDEX_SYNC_LEASE_LOST":
                    from sqlalchemy import text
                    with self.engine.begin() as connection:
                        connection.execute(text("UPDATE index_sync_states SET lease_expires_at=UTC_TIMESTAMP(6) "
                                                "WHERE knowledge_base_id=:kb"), {"kb": self.kb})
                observed.set()
                raise exception
            result = renewal(task)
            observed.set()
            return result
        encode = self.search.encoder.documents
        def waiting(texts):
            armed.set()
            self.assertTrue(observed.wait(3))
            if recover:
                deadline = time.monotonic() + 3
                while len(calls) < 2 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertGreaterEqual(len(calls), 2)
            else:
                thread[0].join(timeout=3)
                self.assertFalse(thread[0].is_alive())
            return encode(texts)
        with patch("rag.management.index_sync.HEARTBEAT_SECONDS", .01), \
                patch("rag.management.index_sync.HEARTBEAT_RETRY_DELAYS", (.01, .02)), \
                patch.object(self.indexing, "renew", side_effect=renew), \
                patch.object(self.search.encoder, "documents", side_effect=waiting):
            if isinstance(exception, ManagementError) and exception.code == "INDEX_SYNC_LEASE_LOST":
                with self.assertRaises(ManagementError) as caught:
                    self.auto()
                self.assertEqual(caught.exception.code, "INDEX_SYNC_LEASE_LOST")
                result = self.state()
            else:
                result = self.auto()
        return result, len(calls)

    def test_transient_index_heartbeat_recovers_without_restarting(self):
        result, calls = self.heartbeat_fault(ConnectionError("secret-token"), recover=True)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["attempt_count"], 1)
        self.assertGreaterEqual(calls, 2)

    def test_exhausted_index_heartbeat_retries_schedule_retry(self):
        result, calls = self.heartbeat_fault(ConnectionError("secret-token"))
        self.assertEqual(calls, 3)
        self.assertEqual(result["status"], "pending")
        self.assertIsNone(self.search.store.current(self.kb))

    def test_permanent_index_heartbeat_error_fails_without_exception_text(self):
        result, calls = self.heartbeat_fault(RuntimeError("secret-token"))
        self.assertEqual(calls, 1)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("secret-token", str(result))

    def test_actual_index_heartbeat_lease_loss_is_fenced(self):
        result, calls = self.heartbeat_fault(ManagementError("INDEX_SYNC_LEASE_LOST"))
        self.assertEqual(calls, 1)
        self.assertEqual(result["status"], "running")
        self.assertIsNone(self.search.store.current(self.kb))
        self.assertEqual(self.auto()["status"], "succeeded")

    def test_management_commit_after_publication_remains_pending(self):
        from sqlalchemy import event
        from sqlalchemy.orm import Session
        from rag.management.models import IndexSyncState
        changed = []
        def after_commit(session):
            if not changed and any(isinstance(row, IndexSyncState) and row.status == "succeeded"
                                   for row in session.identity_map.values()):
                changed.append(True)
                self.service.set_status("document", self.document, "disabled")
        event.listen(Session, "after_commit", after_commit)
        try:
            result = self.auto()
        finally:
            event.remove(Session, "after_commit", after_commit)
        self.assertTrue(changed)
        self.assertEqual(result["status"], "pending")
        self.assertLess(result["applied_revision"], result["desired_revision"])
        self.assertEqual(self.query()["contexts"], [])
        self.assertEqual(self.auto()["status"], "succeeded")
        self.assertFalse(self.query()["sync_lag"])

    def test_unacknowledged_alias_response_checks_actual_publication(self):
        real = self.client.indices.update_aliases
        def no_ack(**kwargs):
            real(**kwargs)
            return {"acknowledged": False}
        with patch.object(self.client.indices, "update_aliases", side_effect=no_ack):
            result = self.auto()
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(self.query()["sync_lag"])

    def test_unconfirmed_alias_publication_is_retryable_and_records_candidate(self):
        with patch.object(self.client.indices, "update_aliases", return_value={"acknowledged": False}):
            result = self.auto()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["error_code"], "INDEX_PUBLISH_FAILED")
        attempt = self.indexing.show(self.kb)["attempts"]["items"][0]
        self.assertIsNotNone(attempt["generation"])
        self.assertIsNone(self.search.store.current(self.kb))
        self.assertEqual(self.auto()["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
