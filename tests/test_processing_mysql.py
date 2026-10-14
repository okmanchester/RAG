"""Opt-in disposable MySQL tests for registration, leases, atomic results and recovery."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from sqlalchemy import func, select, text

from rag.management.config import ROOT, Settings, migration_config
from rag.management.errors import ManagementError
from rag.management.models import Document, DocumentBuild, DocumentVersion, ImportRun, ProcessingTask, ProcessingAttempt
from rag.management.processing import ProcessingService
from rag.management.service import ManagementService
from rag.management.storage import load_snapshot
from scripts.backup_knowledge_base import container_sql
from test_chunking import fake_parse, middle, node
from test_processing import config_fixture


@unittest.skipUnless(os.environ.get("RAG_RUN_MYSQL_TESTS") == "1", "set RAG_RUN_MYSQL_TESTS=1 for disposable MySQL tests")
class ProcessingMySQLTests(unittest.TestCase):
    def setUp(self):
        self.database = "rag_test_processing_" + uuid.uuid4().hex[:12]
        self.env_file = ROOT / ".env.mysql"
        settings = Settings.from_env(self.env_file)
        container_sql(self.env_file, f"CREATE DATABASE `{self.database}` CHARACTER SET utf8mb4;\n"
                      f"GRANT ALL PRIVILEGES ON `{self.database}`.* TO '{settings.user}'@'%';")
        self.addCleanup(self.drop_database)
        self.engine = replace(settings, database=self.database).engine()
        self.addCleanup(self.engine.dispose)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "tmp", prefix="processing-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.management = ManagementService(self.engine, self.root / "storage")
        self.management.db_upgrade()
        self.processing = ProcessingService(self.management)
        self.kb = self.management.kb_create("test", "处理测试")["id"]
        self.raw = self.root / "raw"
        (self.raw / "policy").mkdir(parents=True)
        (self.raw / "policy/sample.pdf").write_bytes(b"PDF fixture")
        self.options = config_fixture(self.root)
        self.registration = self.processing.register(self.kb, self.raw, "policy/sample.pdf")

    def drop_database(self):
        if not self.database.startswith("rag_test_processing_"):
            raise RuntimeError("unexpected database")
        container_sql(self.env_file, f"DROP DATABASE `{self.database}`;")

    def submit(self, **options):
        return self.processing.submit(self.registration["version_id"], **{**self.options, **options})["task_id"]

    def execute(self, task, empty=False, side_effect=None):
        raw = middle([]) if empty else middle([node("doc_title", "测试标题", level=1), node("text", "这是政策测试正文。")])
        with patch("rag.chunk.parse", side_effect=side_effect or fake_parse(raw)), patch("rag.splitter.TokenCounter", return_value=len):
            return self.processing.execute(task)

    def test_registration_duplicate_updates_and_concurrency(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.processing.register(self.kb, self.raw, "policy/./sample.pdf"), range(2)))
        self.assertTrue(all(r["version_id"] == self.registration["version_id"] and r["reused"] for r in results))
        self.submit()
        result = self.execute(self.processing.claim())
        self.management.activate_document(self.registration["document_id"], result["build_id"])
        (self.raw / "policy/sample.pdf").write_bytes(b"updated PDF")
        registered = self.processing.register(self.kb, self.raw, "policy/sample.pdf")
        self.assertEqual(registered["document_id"], self.registration["document_id"])
        self.assertNotEqual(registered["version_id"], self.registration["version_id"])
        self.assertEqual(registered["active_build_id"], result["build_id"])
        self.assertEqual(self.management.list_records("version", document_id=registered["document_id"])["total"], 2)

    def test_submit_dedup_and_configuration_changes(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: self.submit(), range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertNotEqual(ids[0], self.submit(maximum=500))
        self.assertEqual(self.processing.list(self.kb, limit=1)["total"], 2)
        self.assertEqual(len(self.processing.list(self.kb, limit=1, offset=1)["items"]), 1)

    def test_success_is_atomic_and_does_not_activate_or_create_import(self):
        task_id = self.submit()
        result = self.execute(self.processing.claim())
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["can_activate"])
        self.assertEqual(result["attempts"]["items"][0]["status"], "succeeded")
        with self.management.sessions() as session:
            self.assertIsNone(session.get(Document, self.registration["document_id"]).active_build_id)
            self.assertEqual(session.scalar(select(func.count()).select_from(ImportRun)), 0)
        self.assertEqual(self.submit(), task_id)
        with self.assertRaises(ManagementError):
            self.processing.retry(task_id)
        self.assertNotIn("secret-token", json.dumps(result, default=str))
        self.assertNotIn("processing-test-", json.dumps(self.management.show("build", result["build_id"])["document_payload_json"]))

    def test_empty_document_saves_unactivatable_build(self):
        self.submit()
        result = self.execute(self.processing.claim(), empty=True)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["build_status"], "empty")
        self.assertFalse(result["can_activate"])
        with self.assertRaises(ManagementError):
            self.management.activate_document(self.registration["document_id"], result["build_id"])

    def test_failure_retry_keeps_history_and_previous_build(self):
        self.submit()
        initial = self.execute(self.processing.claim())
        self.management.activate_document(self.registration["document_id"], initial["build_id"])
        task_id = self.submit(maximum=500)
        result = self.execute(self.processing.claim(), side_effect=RuntimeError("secret-token"))
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["build_id"])
        self.processing.retry(task_id)
        result = self.execute(self.processing.claim())
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["attempts"]["total"], 2)
        self.assertEqual(self.management.show("document", self.registration["document_id"])["active_build_id"], initial["build_id"])

    def test_temporary_failure_schedule_and_three_attempt_limit(self):
        task_id = self.submit()
        for ordinal, delay in ((1, 30), (2, 120), (3, None)):
            task = self.processing.claim()
            with self.management.sessions() as session:
                before = self.processing.now(session)
            self.processing.fail(task_id, task["execution_token"], TimeoutError("secret-token"))
            result = self.processing.show(task_id)
            self.assertEqual(result["status"], "pending" if delay else "failed")
            if delay:
                self.assertGreaterEqual((result["scheduled_at"] - before).total_seconds(), delay)
                self.assertIsNone(self.processing.claim())
                with self.management.sessions.begin() as session:
                    session.get(ProcessingTask, task_id).scheduled_at = self.processing.now(session)
        self.assertEqual(result["attempt_count"], 3)
        self.processing.retry(task_id)
        result = self.execute(self.processing.claim())
        self.assertEqual(result["attempt_count"], 4)
        self.assertEqual(result["status"], "succeeded")

    def test_expired_lease_requeues_and_fences_old_worker(self):
        task_id = self.submit()
        old = self.processing.claim()
        self.processing.renew(task_id, old["execution_token"], "parsing")
        with self.management.sessions.begin() as session:
            session.get(ProcessingTask, task_id).lease_expires_at = self.processing.now(session) - timedelta(seconds=1)
        new = self.processing.claim()
        self.assertNotEqual(new["execution_token"], old["execution_token"])
        for operation in (lambda: self.processing.renew(task_id, old["execution_token"]),
                          lambda: self.processing.fail(task_id, old["execution_token"], RuntimeError()),
                          lambda: self.processing.complete(task_id, old["execution_token"], None, None)):
            with self.assertRaises(ManagementError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "TASK_LEASE_LOST")
        result = self.execute(new)
        self.assertEqual(result["attempts"]["items"][0]["status"], "interrupted")
        self.assertEqual(result["status"], "succeeded")

    def test_worker_exclusion_idle_and_single_execution(self):
        with self.processing.worker_lock():
            with self.assertRaises(ManagementError) as caught:
                with self.processing.worker_lock():
                    pass
            self.assertEqual(caught.exception.code, "WORKER_BUSY")
        self.assertEqual(self.processing.run(once=True), {"status": "idle"})
        self.submit()
        self.submit(maximum=500)
        with patch.object(self.processing, "process", side_effect=RuntimeError("secret-token")):
            self.assertEqual(self.processing.run(once=True)["status"], "failed")
        self.assertEqual(self.processing.list(self.kb, "pending")["total"], 1)

    def test_heartbeat_renews_during_model_execution(self):
        import time
        self.submit()
        task = self.processing.claim()
        parser = fake_parse(middle([node("text", "正文。")]))
        def slow(*args, **kwargs):
            time.sleep(.2)
            return parser(*args, **kwargs)
        with patch("rag.management.processing.HEARTBEAT_SECONDS", .05), \
                patch.object(self.processing, "renew", wraps=self.processing.renew) as renewal:
            result = self.execute(task, side_effect=slow)
        self.assertEqual(result["status"], "succeeded")
        self.assertGreater(renewal.call_count, 5)

    def heartbeat_fault(self, exception, count, *, recover=False, fail_write=False):
        """Exercise the actual worker/pipeline boundary with a deterministic heartbeat fault."""
        import threading
        from sqlalchemy.exc import OperationalError
        self.submit()
        second_id = self.submit(maximum=500) if fail_write else None
        original = self.processing.renew
        observed, recovered, allow_faults = threading.Event(), threading.Event(), threading.Event()
        calls, threads = [], []
        def renewal(task_id, token, stage=None):
            if stage is not None:
                return original(task_id, token, stage)
            if not allow_faults.is_set():
                return original(task_id, token)
            calls.append(task_id)
            threads[:] = [threading.current_thread()]
            if len(calls) <= count:
                if len(calls) == count:
                    observed.set()
                if isinstance(exception, ManagementError) and exception.code == "TASK_LEASE_LOST":
                    with self.management.sessions.begin() as session:
                        session.get(ProcessingTask, task_id).lease_expires_at = self.processing.now(session) - timedelta(seconds=1)
                    return original(task_id, token)
                raise exception
            result = original(task_id, token)
            recovered.set()
            return result
        parser = fake_parse(middle([node("text", "正文。")]))
        def processing(*args, **kwargs):
            allow_faults.set()
            self.assertTrue((recovered if recover else observed).wait(3), "heartbeat fault was not exercised")
            if not recover:
                threads[0].join(timeout=3)
                self.assertFalse(threads[0].is_alive())
            return parser(*args, **kwargs)
        with patch("rag.management.processing.HEARTBEAT_SECONDS", .01), \
                patch("rag.management.processing.HEARTBEAT_RETRY_DELAYS", (.01, .02)), \
                patch.object(self.processing, "renew", side_effect=renewal), \
                patch.object(self.processing, "fail", wraps=self.processing.fail) as failure:
            if fail_write:
                failure.side_effect = OperationalError(None, None, Exception(2013, "secret-token"))
                with patch("rag.chunk.parse", side_effect=processing), patch("rag.splitter.TokenCounter", return_value=len):
                    with self.assertRaises(OperationalError):
                        self.processing.run(once=False)
                self.assertEqual(self.processing.show(second_id)["attempt_count"], 0)
                self.assertEqual(self.processing.show(second_id)["status"], "pending")
                result = self.processing.show(calls[0])
            else:
                result = self.execute(self.processing.claim(), side_effect=processing)
            return result, len(calls), failure.call_count

    def test_single_transient_heartbeat_failure_recovers_without_restarting_task(self):
        from sqlalchemy.exc import OperationalError
        result, calls, failures = self.heartbeat_fault(OperationalError(None, None, Exception(2013, "secret-token")), 1, recover=True)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["attempt_count"], 1)
        self.assertEqual(result["attempts"]["items"][0]["status"], "succeeded")
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(failures, 0)

    def test_exhausted_heartbeat_retries_schedule_task_retry(self):
        from sqlalchemy.exc import OperationalError
        result, calls, failures = self.heartbeat_fault(OperationalError(None, None, Exception(2013, "secret-token")), 3)
        self.assertEqual(calls, 3)
        self.assertEqual(failures, 1)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["error_code"], "PROCESSING_TEMPORARY")
        self.assertEqual(result["attempts"]["items"][0]["status"], "failed")
        self.assertIsNone(result["build_id"])
        self.assertIsNone(self.processing.claim())

    def test_actual_lease_loss_is_not_retried_or_written_by_old_execution(self):
        result, calls, failures = self.heartbeat_fault(ManagementError("TASK_LEASE_LOST"), 1)
        self.assertEqual(calls, 1)
        self.assertEqual(failures, 0)
        self.assertEqual(result["status"], "running")
        self.assertIsNone(result["build_id"])
        result = self.execute(self.processing.claim())
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["attempts"]["items"][0]["status"], "interrupted")

    def test_permanent_heartbeat_error_is_recorded_without_retry(self):
        result, calls, failures = self.heartbeat_fault(RuntimeError("secret-token"), 1)
        self.assertEqual((calls, failures), (1, 1))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "PROCESSING_FAILED")
        self.assertNotIn("secret-token", json.dumps(result, default=str))

    def test_worker_exits_if_heartbeat_failure_cannot_be_persisted(self):
        from sqlalchemy.exc import OperationalError
        result, calls, failures = self.heartbeat_fault(OperationalError(None, None, Exception(2013, "secret-token")), 3, fail_write=True)
        self.assertEqual((calls, failures), (3, 1))
        self.assertEqual(result["status"], "running")

    def test_successful_task_is_not_reused_after_orchestration_change(self):
        from rag.management.storage import file_hash
        first_id = self.submit()
        first = self.execute(self.processing.claim())
        def changed(path):
            return "f" * 64 if Path(path).as_posix().endswith("management/service.py") else file_hash(path)
        with patch("rag.management.processing.file_hash", side_effect=changed):
            second_id = self.submit()
            self.assertNotEqual(first_id, second_id)
            self.assertEqual(self.submit(), second_id)
            second = self.execute(self.processing.claim())
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(first["build_id"], second["build_id"])
        self.assertEqual(self.processing.show(first_id)["status"], "succeeded")

    def test_pending_old_identity_requires_resubmission(self):
        from rag.management.storage import fingerprint
        task_id = self.submit()
        with self.management.sessions.begin() as session:
            task = session.get(ProcessingTask, task_id)
            old_config = dict(task.config_json)
            old_config["format_version"] = 1
            old_config["implementation_sha256"] = {k:v for k,v in old_config["implementation_sha256"].items()
                                                   if not k.startswith("management/")}
            task.config_json = old_config
            task.config_sha256 = fingerprint(old_config)
        result = self.execute(self.processing.claim())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "PROCESSING_IDENTITY_CHANGED")
        self.assertNotEqual(self.submit(), task_id)

    def test_concurrent_claims_do_not_duplicate_work(self):
        self.submit()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.processing.claim(), range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_cli_register_process_query_and_retry_exit_codes(self):
        def cli(arguments):
            env = {**os.environ, "MYSQL_DATABASE": self.database, "RAG_STORAGE_ROOT": str(self.management.storage_root)}
            result = subprocess.run([sys.executable, "-m", "rag.manage", *arguments], env=env, cwd=ROOT,
                                    capture_output=True, text=True, encoding="utf-8", timeout=30)
            return result.returncode, json.loads(result.stdout or result.stderr)
        code, registration = cli(["document", "register", "--kb-id", str(self.kb), "--raw-root", str(self.raw),
                                  "--source-path", "policy/sample.pdf"])
        self.assertEqual(code, 0)
        code, task = cli(["document", "process", "--version-id", str(registration["version_id"]),
                         "--model-path", str(self.options["model_path"]), "--mineru-config", str(self.options["config_path"])])
        self.assertEqual(code, 0)
        code, result = cli(["task", "show", "--task-id", str(task["task_id"])])
        self.assertEqual(result["status"], "pending")
        code, result = cli(["task", "retry", "--task-id", str(task["task_id"])])
        self.assertEqual(code, 1)
        self.assertEqual(result["error_code"], "TASK_RETRY_NOT_ALLOWED")
        code, result = cli(["task", "list", "--kb-id", str(self.kb)])
        self.assertEqual(result["total"], 1)

    def test_changed_environment_and_corrupt_storage_fail_without_build(self):
        task_id = self.submit()
        (self.options["model_path"] / "tokenizer.json").write_text('{"changed":true}')
        result = self.execute(self.processing.claim())
        self.assertEqual(result["error_code"], "PROCESSING_IDENTITY_CHANGED")
        with self.management.sessions() as session:
            Path(session.get(DocumentVersion, self.registration["version_id"]).storage_uri).write_bytes(b"corrupt")
        self.submit(maximum=500)
        result = self.execute(self.processing.claim())
        self.assertEqual(result["error_code"], "STORED_RAW_CHECKSUM")
        self.assertEqual(self.processing.show(task_id)["attempt_count"], 1)

    def test_key_required_is_fixed_error(self):
        config_path = self.options["config_path"]
        data = json.loads(config_path.read_text())
        data["llm_aided"]["features"] = {"cross_page_table_cell_merge": True}
        config_path.write_text(json.dumps(data))
        self.submit()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            result = self.execute(self.processing.claim())
        self.assertEqual(result["error_code"], "PROCESSING_KEY_REQUIRED")

    def test_publish_then_transaction_failure_rolls_back_build_and_success(self):
        task_id = self.submit()
        original = self.management.persist_build
        def fail_after_insert(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("secret-token")
        with patch.object(self.management, "persist_build", side_effect=fail_after_insert):
            result = self.execute(self.processing.claim())
        self.assertEqual(result["status"], "failed")
        self.assertTrue(list((self.management.storage_root / "snapshots").iterdir()))
        with self.management.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(DocumentBuild)), 0)
        self.processing.retry(task_id)
        result = self.execute(self.processing.claim())
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(list((self.management.storage_root / "snapshots").iterdir())), 1)

    def test_missing_asset_fails_before_publication(self):
        self.submit()
        parser = fake_parse(middle([node("text", "正文")]))
        def missing(*args, **kwargs):
            blocks, meta = parser(*args, **kwargs)
            meta["artifact_files_sha256"] = {"missing.png": "a" * 64}
            # process_document calculates hashes of real artifacts; absent source references must still be rejected.
            blocks[0]["assets"] = ["missing.png"]
            return blocks, meta
        result = self.execute(self.processing.claim(), side_effect=missing)
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["build_id"])

    def test_disabled_resources_and_registration_path_validation(self):
        for source in ("../sample.pdf", "D:/private/sample.pdf", "policy/a.txt"):
            with self.assertRaises(ManagementError):
                self.processing.register(self.kb, self.raw, source)
        self.submit()
        self.management.set_status("document", self.registration["document_id"], "disabled")
        result = self.execute(self.processing.claim())
        self.assertEqual(result["error_code"], "RESOURCE_DISABLED")
        with self.assertRaises(ManagementError):
            self.submit(maximum=500)

    def test_actual_process_termination_is_recoverable(self):
        task_id = self.submit()
        script = self.root / "crash_worker.py"
        script.write_text("""import json,time
from rag.management.config import Settings
from rag.management.service import ManagementService
from rag.management.processing import ProcessingService
s=Settings.from_env(); e=s.engine(); p=ProcessingService(ManagementService(e,s.storage_root))
t=p.claim(); p.renew(t['id'],t['execution_token'],'parsing')
print(json.dumps({'task_id':t['id']}),flush=True)
time.sleep(120)
""", encoding="utf-8")
        env = {**os.environ, "MYSQL_DATABASE": self.database, "PYTHONPATH": str(ROOT),
               "RAG_STORAGE_ROOT": str(self.management.storage_root)}
        child = subprocess.Popen([sys.executable, str(script)], env=env, cwd=ROOT, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            self.assertEqual(json.loads(child.stdout.readline())["task_id"], task_id)
        finally:
            child.kill()
            child.communicate(timeout=15)
        with self.management.sessions.begin() as session:
            session.get(ProcessingTask, task_id).lease_expires_at = self.processing.now(session) - timedelta(seconds=1)
        result = self.execute(self.processing.claim())
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["attempts"]["items"][0]["status"], "interrupted")

    def test_backup_restore_clears_leases_and_preserves_completed_results(self):
        from scripts.backup_knowledge_base import backup, restore
        first_id = self.submit()
        succeeded = self.execute(self.processing.claim())
        second_id = self.submit(maximum=500)
        self.processing.claim()
        target = "rag_test_processing_" + uuid.uuid4().hex[:12]
        try:
            with patch.dict(os.environ, {"MYSQL_DATABASE": self.database, "RAG_STORAGE_ROOT": str(self.management.storage_root)}):
                backup(self.env_file, self.root / "backup")
                restore(self.env_file, self.root / "backup", target, self.root / "restore")
            engine = replace(Settings.from_env(self.env_file), database=target).engine()
            try:
                restored = ProcessingService(ManagementService(engine, self.root / "restore"))
                self.assertEqual(restored.show(first_id)["build_id"], succeeded["build_id"])
                self.assertEqual(restored.show(first_id)["status"], "succeeded")
                pending = restored.show(second_id)
                self.assertEqual(pending["status"], "pending")
                self.assertIsNone(pending["lease_expires_at"])
                self.assertEqual(pending["attempts"]["items"][0]["status"], "interrupted")
                self.assertEqual(self.processing.show(second_id)["status"], "running")
            finally:
                engine.dispose()
        finally:
            container_sql(self.env_file, f"DROP DATABASE IF EXISTS `{target}`;")

    def test_migration_matches_metadata_and_blocks_task_history_loss(self):
        from alembic import command
        from alembic.autogenerate import compare_metadata
        from alembic.migration import MigrationContext
        from rag.management.models import Base
        with self.engine.connect() as connection:
            self.assertEqual(compare_metadata(MigrationContext.configure(connection), Base.metadata), [])
        with self.engine.begin() as connection:
            command.downgrade(migration_config(connection), "0002_import_run_items")
        self.management.db_upgrade()
        self.submit()
        with self.assertRaisesRegex(RuntimeError, "pre-upgrade backup"):
            with self.engine.begin() as connection:
                command.downgrade(migration_config(connection), "0002_import_run_items")
        self.assertEqual(self.management.db_check()["business_tables"], 9)


if __name__ == "__main__":
    unittest.main()
