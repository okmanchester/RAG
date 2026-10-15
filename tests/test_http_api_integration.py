"""Disposable real MySQL/ES HTTP workflows; parser and embedding are local test doubles."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from rag.api.app import create_app
from rag.api.runtime import Runtime
from rag.management.config import ROOT
from rag.management.errors import ManagementError
from rag.management.index_sync import database_now
from rag.management.models import (Chunk, Document, DocumentBuild, DocumentVersion, ImportRun, ImportRunItem,
    IndexSyncAttempt, IndexSyncState, KnowledgeBase, ProcessingAttempt, ProcessingTask)
from rag.management.processing import ProcessingService
from rag.management.service import ManagementService
from rag.management.worker import run_worker
from rag.search.config import SearchSettings
from rag.search.service import SearchService
from test_chunking import fake_parse, middle, node
import test_management_mysql as mysql_tests
from test_processing import config_fixture
from test_search_integration import FixtureEncoder


class HTTPFixture:
    @classmethod
    def setUpClass(cls):
        mysql_tests.MySQLManagementTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        mysql_tests.MySQLManagementTests.tearDownClass.__func__(cls)

    def setUp(self):
        # Only this class's disposable database, including durable tasks from the previous case.
        with self.engine.begin() as connection:
            connection.execute(update(Document).values(active_build_id=None))
            for model in (IndexSyncAttempt, IndexSyncState, ProcessingAttempt, ProcessingTask, ImportRunItem,
                          Chunk, DocumentBuild, DocumentVersion, ImportRun, Document, KnowledgeBase):
                connection.execute(delete(model))
        self.temporary = tempfile.TemporaryDirectory(prefix="http-integration-", dir=ROOT / "tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = ManagementService(self.engine, self.root / "storage")
        self.kb = self.service.kb_create("http", "HTTP测试")["id"]
        self.options = config_fixture(self.root)
        self.processing = ProcessingService(self.service)
        self.runtime = Runtime(self.service, processing=self.processing, processing_options=self.options,
                               search_factory=self.make_search)
        self.client = self.enterContext(TestClient(create_app(runtime=self.runtime, tmp_root=self.root),
                                                   base_url="http://127.0.0.1:8000"))

    def make_search(self):
        raise AssertionError("MySQL-only HTTP tests must never load search dependencies")

    def upload(self, path="policy/sample.pdf", data=b"fixture pdf"):
        return self.client.post(f"/api/v1/knowledge-bases/{self.kb}/documents", data={"source_path": path},
                                files={"file": ("ignored.pdf", data, "application/pdf")})

    def process(self, *, text="这是可检索的测试正文。", empty=False, failure=None):
        raw = middle([]) if empty else middle([node("doc_title", "测试标题", level=1), node("text", text)])
        with patch("rag.chunk.parse", side_effect=failure or fake_parse(raw)), \
             patch("rag.splitter.TokenCounter", return_value=len):
            return run_worker(self.service, once=True, kind="processing", _processing=self.processing)


@unittest.skipUnless(os.environ.get("RAG_RUN_MYSQL_TESTS") == "1", "enable disposable MySQL HTTP tests")
class HTTPMySQLTests(HTTPFixture, unittest.TestCase):
    def test_upload_dedup_concurrency_and_new_version(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(pool.map(lambda _: self.upload(), range(3)))
        self.assertTrue(all(r.status_code == 202 for r in responses), [r.text for r in responses])
        uploads = [r.json() for r in responses]
        self.assertEqual(len({r["document_id"] for r in uploads}), 1)
        self.assertEqual(len({r["version_id"] for r in uploads}), 1)
        self.assertEqual(len({r["task_id"] for r in uploads}), 1)
        changed = self.upload(data=b"new source bytes").json()
        self.assertEqual(changed["document_id"], uploads[0]["document_id"])
        self.assertNotEqual(changed["version_id"], uploads[0]["version_id"])
        self.assertNotEqual(changed["task_id"], uploads[0]["task_id"])
        self.assertIsNone(changed["active_build_id"])
        versions = self.client.get(f"/api/v1/documents/{changed['document_id']}/versions?limit=1&offset=1").json()
        self.assertEqual(versions["total"], 2)
        self.assertEqual(len(versions["items"]), 1)
        self.assertNotIn("storage_uri", str(versions))
        self.assertEqual(self.processing.list(self.kb)["total"], 2)

    def test_registration_and_enqueued_task_roll_back_together(self):
        submit = self.processing._submit
        def abort(*args):
            submit(*args)
            raise RuntimeError("private password payload")
        with patch.object(self.processing, "_submit", side_effect=abort):
            response = self.upload()
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotIn("private", response.text)
        self.assertEqual(self.service.list_records("document", kb_id=self.kb)["total"], 0)
        self.assertEqual(self.processing.list(self.kb)["total"], 0)
        # Failure while adding a new version also preserves the previous registered version.
        first = self.upload().json()
        with patch.object(self.processing, "_submit", side_effect=abort):
            self.assertEqual(self.upload(data=b"new bytes").status_code, 500)
        self.assertEqual(self.client.get(f"/api/v1/documents/{first['document_id']}/versions").json()["total"], 1)
        self.assertEqual(self.processing.list(self.kb)["total"], 1)
        self.assertFalse(list(self.root.glob("http-upload-*")))

    def test_worker_progress_explicit_activation_noop_and_ownership(self):
        upload = self.upload().json()
        stages = []
        renew = self.processing.renew
        def observe(task_id, token, stage=None):
            renew(task_id, token, stage)
            if stage:
                current = self.client.get(f"/api/v1/processing-tasks/{task_id}").json()
                self.assertEqual(current["stage"], stage)
                self.assertEqual(current["status"], "running")
                stages.append(stage)
        with patch.object(self.processing, "renew", side_effect=observe):
            result = self.process()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(set(stages), {"validation", "parsing", "chunking", "publishing", "persisting"})
        doc_id, build_id = upload["document_id"], result["build_id"]
        detail = self.client.get(f"/api/v1/documents/{doc_id}").json()
        self.assertIsNone(detail["active_build_id"])
        builds = self.client.get(f"/api/v1/documents/{doc_id}/builds").json()["items"]
        self.assertTrue(builds[0]["can_activate"])
        self.assertFalse(builds[0]["is_active"])
        activated = self.client.put(f"/api/v1/documents/{doc_id}/active-build", json={"build_id": build_id})
        self.assertEqual(activated.status_code, 200, activated.text)
        again = self.client.put(f"/api/v1/documents/{doc_id}/active-build", json={"build_id": build_id})
        self.assertEqual(again.json()["sync_target"], activated.json()["sync_target"])
        other = self.upload(path="policy/other.pdf").json()["document_id"]
        wrong = self.client.put(f"/api/v1/documents/{other}/active-build", json={"build_id": build_id})
        self.assertEqual((wrong.status_code, wrong.json()["error_code"]), (409, "BUILD_OWNERSHIP"))
        for status in ("disabled", "active"):
            response = self.client.patch(f"/api/v1/documents/{doc_id}/status", json={"status": status})
            self.assertEqual(response.status_code, 200, response.text)
        reused = self.upload()
        self.assertEqual(reused.status_code, 200)
        self.assertTrue(reused.json()["reused_task"])
        self.assertEqual(reused.json()["active_build_id"], build_id)

    def test_empty_build_is_successful_but_cannot_activate(self):
        uploaded = self.upload().json()
        result = self.process(empty=True)
        self.assertEqual(result["status"], "succeeded")
        task = self.client.get(f"/api/v1/processing-tasks/{uploaded['task_id']}").json()
        self.assertEqual(task["build_status"], "empty")
        self.assertFalse(task["can_activate"])
        builds = self.client.get(f"/api/v1/documents/{uploaded['document_id']}/builds").json()["items"]
        self.assertEqual(builds[0]["chunk_count"], 0)
        self.assertFalse(builds[0]["can_activate"])
        response = self.client.put(f"/api/v1/documents/{uploaded['document_id']}/active-build",
                                   json={"build_id": result["build_id"]})
        self.assertEqual(response.status_code, 409)

    def test_failure_reason_manual_retry_and_attempt_history(self):
        uploaded = self.upload().json()
        self.assertEqual(self.process(failure=RuntimeError("private credentials"))["status"], "failed")
        url = f"/api/v1/processing-tasks/{uploaded['task_id']}"
        failed = self.client.get(url)
        self.assertNotIn("private", failed.text)
        self.assertEqual(failed.json()["error_code"], "PROCESSING_FAILED")
        reused = self.upload()
        self.assertEqual(reused.status_code, 200)
        self.assertEqual(reused.json()["status"], "failed")
        self.assertEqual(self.client.post(url + "/retry").status_code, 202)
        self.assertEqual(self.process()["status"], "succeeded")
        task = self.client.get(url + "?limit=1&offset=1").json()
        self.assertEqual(task["attempts"]["total"], 2)
        self.assertEqual(task["attempts"]["items"][0]["ordinal"], 2)
        self.assertEqual(self.client.post(url + "/retry").status_code, 409)
        versions = self.client.get(f"/api/v1/documents/{uploaded['document_id']}/versions").json()
        self.assertEqual(versions["total"], 1)

    def test_new_server_config_resubmits_and_disabled_resources_reject_upload(self):
        first = self.upload().json()
        self.runtime.processing_options["maximum"] = 500
        response = self.client.post(f"/api/v1/versions/{first['version_id']}/processing-tasks")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertNotEqual(response.json()["task_id"], first["task_id"])
        self.assertFalse(response.json()["reused"])
        self.client.patch(f"/api/v1/documents/{first['document_id']}/status", json={"status": "disabled"})
        self.assertEqual(self.upload().status_code, 409)
        self.service.set_status("kb", self.kb, "disabled")
        self.assertEqual(self.upload(path="other.pdf").status_code, 409)

    def test_api_model_identity_prerequisite_is_explicit_and_atomic(self):
        self.runtime.processing_options["model_path"] = self.root / "missing-tokenizer"
        response = self.upload()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error_code"], "PROCESSING_MODELS_MISSING")
        self.assertNotIn(str(self.root), response.text)
        self.assertEqual(self.service.list_records("document", kb_id=self.kb)["total"], 0)
        self.assertEqual(self.processing.list(self.kb)["total"], 0)
        self.assertEqual(self.client.get("/api/v1/knowledge-bases").status_code, 200)


@unittest.skipUnless(os.environ.get("RAG_RUN_MYSQL_TESTS") == "1" and os.environ.get("RAG_RUN_ES_TESTS") == "1",
                     "enable disposable MySQL/ES HTTP tests")
class HTTPMySQLESTests(HTTPFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.es_client = SearchSettings.from_env().client()
            cls.es_client.info()
        except BaseException:
            super().tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            from rag.search.corpus import ManagedCorpus
            prefix = f"rag-http-test-kb-{ManagedCorpus(cls.engine).namespace}-"
            for name in cls.es_client.indices.get_mapping(index=prefix + "*", allow_no_indices=True):
                if not name.startswith(prefix) or "-g-" not in name:
                    raise RuntimeError("refusing to delete an index outside the HTTP test namespace")
                cls.es_client.indices.delete(index=name)
            cls.es_client.close()
        finally:
            super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.search = SearchService(self.engine, self.es_client, encoder=FixtureEncoder(), prefix="rag-http-test")

    def make_search(self):
        return self.search

    def index(self):
        with self.service.sessions.begin() as session:
            session.get(IndexSyncState, self.kb).scheduled_at = database_now(session) - timedelta(seconds=1)
        with patch("rag.search.service.SearchService", return_value=self.search):
            return run_worker(self.service, kind="index", once=True, _processing=self.processing)

    def query(self):
        return self.client.post(f"/api/v1/knowledge-bases/{self.kb}/search", json={"query": "测试正文", "mode": "hybrid"})

    def activate(self, doc_id, build_id):
        return self.client.put(f"/api/v1/documents/{doc_id}/active-build", json={"build_id": build_id})

    def test_upload_worker_activation_auto_index_search_and_disable(self):
        upload = self.upload().json()
        processed = self.process()
        self.assertEqual(processed["status"], "succeeded")
        self.assertIsNone(self.client.get(f"/api/v1/documents/{upload['document_id']}").json()["active_build_id"])
        self.assertEqual(self.activate(upload["document_id"], processed["build_id"]).status_code, 200)
        self.assertEqual(self.query().status_code, 503)  # Missing alias, not an empty successful query.
        self.assertEqual(self.index()["status"], "succeeded")
        response = self.query()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["sync_lag"])
        hit = response.json()["contexts"][0]
        self.assertEqual(hit["version_id"], upload["version_id"])
        self.assertEqual(hit["source_path"], "policy/sample.pdf")
        self.assertEqual(hit["title"], "测试标题")
        self.assertTrue(hit["sources"])
        self.assertEqual(hit["sources"][0]["page"], 1)
        self.assertEqual(hit["ranks"], {"dense": 1, "bm25": 1})
        status = self.client.get(f"/api/v1/knowledge-bases/{self.kb}/index-status?db_only=false").json()
        self.assertTrue(status["index_checked"])
        self.assertFalse(status["sync_lag"])
        self.assertEqual(status["sync_task"]["desired_revision"], status["sync_task"]["applied_revision"])
        self.client.patch(f"/api/v1/documents/{upload['document_id']}/status", json={"status": "disabled"})
        disabled = self.query().json()
        self.assertEqual(disabled["contexts"], [])
        self.assertTrue(disabled["sync_lag"])
        self.assertEqual(self.index()["status"], "succeeded")
        self.assertFalse(self.query().json()["sync_lag"])
        self.service.set_status("kb", self.kb, "disabled")
        self.assertEqual(self.query().status_code, 409)

    def test_build_switch_never_returns_old_material_while_index_lags(self):
        first = self.upload().json()
        build = self.process()["build_id"]
        self.activate(first["document_id"], build)
        self.index()
        self.assertTrue(self.query().json()["contexts"])
        new = self.upload(data=b"new source bytes").json()
        new_build = self.process(text="新版测试正文。")["build_id"]
        self.assertEqual(self.query().json()["contexts"][0]["build_id"], build)
        self.activate(new["document_id"], new_build)
        lagged = self.query().json()
        self.assertEqual(lagged["contexts"], [])
        self.assertTrue(lagged["sync_lag"])
        self.index()
        hit = self.query().json()["contexts"][0]
        self.assertEqual(hit["build_id"], new_build)
        self.assertEqual(hit["version_id"], new["version_id"])

    def test_index_failure_retry_and_es_outage_preserve_database_status(self):
        upload = self.upload().json()
        self.activate(upload["document_id"], self.process()["build_id"])
        retry_url = f"/api/v1/knowledge-bases/{self.kb}/index-sync/retry"
        self.assertEqual(self.client.post(retry_url).status_code, 409)
        with patch.object(self.search.store, "bulk", side_effect=ManagementError("SEARCH_UNAVAILABLE", retryable=True)):
            self.assertEqual(self.index()["status"], "pending")  # Automatic transient retry is scheduled.
        # A permanent failure ends the cycle and permits explicit retry.
        with patch.object(self.search.store, "bulk", side_effect=ManagementError("INDEX_CORRUPT")):
            self.assertEqual(self.index()["status"], "failed")
        status_url = f"/api/v1/knowledge-bases/{self.kb}/index-status"
        failed = self.client.get(status_url).json()
        self.assertEqual(failed["sync_task"]["error_code"], "INDEX_CORRUPT")
        self.assertEqual(failed["attempts"]["total"], 2)
        self.assertEqual(self.client.post(retry_url).status_code, 202)
        self.assertEqual(self.index()["status"], "succeeded")
        with patch.object(self.search.store, "current", side_effect=ManagementError("SEARCH_UNAVAILABLE")):
            self.assertEqual(self.query().status_code, 503)
            self.assertEqual(self.client.get(status_url + "?db_only=false").status_code, 503)
            database_only = self.client.get(status_url)
            self.assertEqual(database_only.status_code, 200)
            self.assertIsNone(database_only.json()["sync_lag"])


if __name__ == "__main__":
    unittest.main()
