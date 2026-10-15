"""Opt-in durable queue tests on a disposable MySQL database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from rag.management.errors import ManagementError
from rag.management.index_sync import IndexSyncService, database_now
from rag.management.models import IndexSyncState
from rag.search.corpus import ManagedCorpus
import test_management_mysql as mysql_tests


class IndexFixture:
    @classmethod
    def setUpClass(cls):
        mysql_tests.MySQLManagementTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        mysql_tests.MySQLManagementTests.tearDownClass.__func__(cls)

    def setUp(self):
        mysql_tests.MySQLManagementTests.setUp(self)
        self.indexing = IndexSyncService(self.engine)
        run = self.service.import_corpus(self.kb, self.corpus, self.raw)
        self.document = run["results_json"][0]["document_id"]
        self.build = run["results_json"][0]["build_id"]

    def state(self):
        return self.indexing.show(self.kb)["sync_task"]

    def due(self):
        with self.service.sessions.begin() as session:
            session.get(IndexSyncState, self.kb).scheduled_at = database_now(session) - timedelta(seconds=1)

    def claim(self):
        self.due()
        return self.indexing.claim(self.kb)


@unittest.skipUnless(os.environ.get("RAG_RUN_MYSQL_TESTS") == "1", "enable disposable MySQL tests")
class IndexSyncMySQLTests(IndexFixture, unittest.TestCase):
    def test_trigger_coalescing_and_noop(self):
        before = self.state()
        self.service.activate_document(self.document, self.build)
        first = self.state()
        self.assertEqual(first["desired_revision"], before["desired_revision"] + 1)
        self.service.activate_document(self.document, self.build)
        self.assertEqual(self.state()["desired_revision"], first["desired_revision"])
        self.service.set_status("document", self.document, "disabled")
        changed = self.state()
        self.assertEqual(changed["desired_revision"], first["desired_revision"] + 1)
        self.assertEqual(changed["scheduled_at"], first["scheduled_at"])
        self.service.set_status("document", self.document, "disabled")
        self.service.kb_update(self.kb, name="name")
        self.assertEqual(self.state()["desired_revision"], changed["desired_revision"])

    def test_activation_and_queue_rollback_together(self):
        before = self.state()
        from rag.management.index_sync import mark_dirty
        def fail(session, kb_id):
            mark_dirty(session, kb_id)
            raise RuntimeError("abort")
        with patch("rag.management.index_sync.mark_dirty", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "abort"):
                self.service.activate_document(self.document, self.build)
        self.assertIsNone(self.service.show("document", self.document)["active_build_id"])
        self.assertEqual(self.state()["desired_revision"], before["desired_revision"])

    def test_batch_activation_triggers_once_and_repeated_batch_is_noop(self):
        mysql_tests.MySQLManagementTests.add_second_document(self)
        run = self.service.import_corpus(self.kb, self.corpus, self.raw)
        before = self.state()["desired_revision"]
        self.service.activate_import(run["id"])
        self.assertEqual(self.state()["desired_revision"], before + 1)
        self.service.activate_import(run["id"])
        self.assertEqual(self.state()["desired_revision"], before + 1)

    def test_pending_not_due_and_paused_are_not_claimed(self):
        self.assertIsNone(self.indexing.claim(self.kb))
        self.indexing.control(self.kb, "pause")
        self.due()
        self.assertIsNone(self.indexing.claim(self.kb))
        self.indexing.control(self.kb, "resume")
        self.assertIsNotNone(self.indexing.claim(self.kb))

    def test_new_changes_keep_execution_ownership(self):
        task = self.claim()
        self.service.activate_document(self.document, self.build)
        with self.service.sessions() as session:
            row = session.get(IndexSyncState, self.kb)
            self.assertEqual(row.execution_token, task["execution_token"])
            self.assertEqual(row.status, "running")
            self.assertGreater(row.desired_revision, task["desired_revision"])

    def test_concurrent_claim_has_one_owner(self):
        self.due()
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda _: self.indexing.claim(self.kb), range(2)))
        self.assertEqual(sum(t is not None for t in claimed), 1)

    def test_expired_execution_is_interrupted_and_fenced(self):
        task = self.claim()
        with self.service.sessions.begin() as session:
            session.get(IndexSyncState, self.kb).lease_expires_at = database_now(session) - timedelta(seconds=1)
        replacement = self.indexing.claim(self.kb)
        self.assertNotEqual(replacement["execution_token"], task["execution_token"])
        with self.assertRaises(ManagementError) as caught:
            self.indexing.renew(task)
        self.assertEqual(caught.exception.code, "INDEX_SYNC_LEASE_LOST")
        attempts = self.indexing.show(self.kb)["attempts"]["items"]
        self.assertEqual(attempts[1]["status"], "interrupted")

    def test_transient_errors_have_bounded_retries_and_manual_retry(self):
        for cycle in range(1, 4):
            task = self.claim()
            result = self.indexing.fail(task, ManagementError("SEARCH_UNAVAILABLE", retryable=True))
            self.assertEqual(result["status"], "pending" if cycle < 3 else "failed")
            if cycle < 3:
                self.assertIsNone(self.indexing.claim(self.kb))
        self.indexing.control(self.kb, "retry")
        self.assertEqual(self.state()["cycle_attempts"], 0)
        self.assertEqual(self.state()["attempt_count"], 3)
        self.assertIsNotNone(self.indexing.claim(self.kb))

    def test_permanent_error_fails_and_new_change_requeues(self):
        result = self.indexing.fail(self.claim(), ManagementError("SEARCH_ERROR"))
        self.assertEqual(result["status"], "failed")
        self.service.activate_document(self.document, self.build)
        self.assertEqual(self.state()["status"], "pending")
        self.assertIsNone(self.indexing.claim(self.kb))

    def test_lock_contention_does_not_consume_failure_budget(self):
        result = self.indexing.fail(self.claim(), ManagementError("INDEX_BUSY"))
        self.assertEqual((result["status"], result["cycle_attempts"]), ("pending", 0))
        self.assertEqual(self.indexing.show(self.kb)["attempts"]["items"][0]["status"], "deferred")

    def test_snapshot_reads_revision_and_selection_in_same_rr_view(self):
        self.service.activate_document(self.document, self.build)
        task = self.claim()
        corpus = ManagedCorpus(self.engine)
        original = corpus.selection
        def change(*args, **kwargs):
            self.service.set_status("document", self.document, "disabled")
            return original(*args, **kwargs)
        with patch.object(corpus, "selection", side_effect=change):
            selection, revision = self.indexing.snapshot(type("Search", (), {"corpus": corpus})(), task)
        self.assertEqual(selection.build_ids, [self.build])
        self.assertEqual(revision, task["desired_revision"])
        self.assertGreater(self.state()["desired_revision"], revision)

    def test_fixed_error_and_attempt_pagination(self):
        self.indexing.fail(self.claim(), RuntimeError("secret-token"))
        self.indexing.control(self.kb, "retry")
        self.indexing.fail(self.claim(), ManagementError("INDEX_CORRUPT"))
        result = self.indexing.show(self.kb, limit=1, offset=1)
        self.assertEqual(result["attempts"]["total"], 2)
        self.assertEqual(len(result["attempts"]["items"]), 1)
        self.assertNotIn("secret-token", str(result))
        self.assertNotIn("execution_token", str(result))

    def test_missing_es_config_fails_index_task_without_exception_payload(self):
        def configuration():
            raise ManagementError("ELASTIC_PASSWORD_REQUIRED")
        result = self.indexing.execute(self.claim(), configuration)
        self.assertEqual((result["status"], result["error_code"]), ("failed", "ELASTIC_PASSWORD_REQUIRED"))

    def test_renewal_extends_database_clock_lease(self):
        task = self.claim()
        with self.service.sessions.begin() as session:
            session.get(IndexSyncState, self.kb).lease_expires_at = database_now(session) + timedelta(seconds=10)
        self.indexing.renew(task)
        with self.service.sessions() as session:
            self.assertGreater((session.get(IndexSyncState, self.kb).lease_expires_at - database_now(session)).total_seconds(), 100)

    def test_actual_index_process_termination_is_recoverable(self):
        from rag.management.config import ROOT
        self.due()
        script = self.root / "crash_index_worker.py"
        script.write_text('''import json,time
from rag.management.config import Settings
from rag.management.index_sync import IndexSyncService
s=Settings.from_env(); q=IndexSyncService(s.engine()); task=q.claim()
print(json.dumps({'kb_id':task['knowledge_base_id']}),flush=True)
time.sleep(120)
''', encoding="utf-8")
        env = {**os.environ, "MYSQL_DATABASE": self.database, "PYTHONPATH": str(ROOT)}
        child = subprocess.Popen([sys.executable, str(script)], cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            self.assertEqual(json.loads(child.stdout.readline())["kb_id"], self.kb)
        finally:
            child.kill()
            child.communicate(timeout=15)
        self.assertEqual(self.state()["status"], "running")
        with self.service.sessions.begin() as session:
            session.get(IndexSyncState, self.kb).lease_expires_at = database_now(session) - timedelta(seconds=1)
        self.assertIsNotNone(self.indexing.claim(self.kb))
        self.assertEqual(self.indexing.show(self.kb)["attempts"]["items"][1]["status"], "interrupted")

    def test_migration_seeds_existing_kb_and_preserves_authority(self):
        from alembic import command
        from rag.management.config import migration_config
        self.service.activate_document(self.document, self.build)
        with self.engine.begin() as connection:
            command.downgrade(migration_config(connection), "0003_processing_tasks")
        self.service.db_upgrade()
        self.assertEqual(self.state()["desired_revision"], 1)
        self.assertEqual(self.state()["applied_revision"], 0)
        self.assertEqual(self.service.show("document", self.document)["active_build_id"], self.build)

    def test_migration_refuses_to_drop_attempt_history(self):
        from alembic import command
        from rag.management.config import migration_config
        self.claim()
        with self.assertRaisesRegex(RuntimeError, "pre-upgrade backup"):
            with self.engine.begin() as connection:
                command.downgrade(migration_config(connection), "0003_processing_tasks")
        self.assertEqual(self.service.db_check()["migration_heads"], ["0004_index_sync"])


if __name__ == "__main__":
    unittest.main()
