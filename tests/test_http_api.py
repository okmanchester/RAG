"""HTTP contracts and boundaries without a database, model inference, or network calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from rag.api.app import create_app
from rag.api.runtime import Runtime
from rag.api.schemas import SearchRequest
from rag.management.config import ROOT
from rag.management.errors import ManagementError


NOW = datetime(2026, 10, 15, 1, 2, 3)


def page(items):
    return {"total": len(items), "limit": 100, "offset": 0, "items": items}


def document():
    return {"id": 1, "knowledge_base_id": 1, "source_path": "policy/sample.pdf", "title": "测试文档",
            "category": "policy", "status": "active", "active_build_id": None,
            "created_at": NOW, "updated_at": NOW, "source_key": "internal"}


def processing_task():
    return {"id": 3, "document_version_id": 2, "status": "pending", "stage": "validation", "build_id": None,
            "attempt_count": 0, "cycle_attempts": 0, "scheduled_at": NOW, "finished_at": None,
            "created_at": NOW, "updated_at": NOW, "error_code": None, "error": None,
            "config_json": {"model_path": "D:/private/model", "api_key": "secret-value"},
            "config_sha256": "internal", "execution_token": "private-token", "lease_expires_at": NOW}


def sync_task():
    return {"knowledge_base_id": 1, "desired_revision": 2, "applied_revision": 1, "status": "pending",
            "paused": False, "scheduled_at": NOW, "attempt_count": 1, "cycle_attempts": 0,
            "generation": "a" * 32, "error_code": None, "finished_at": None, "created_at": NOW,
            "updated_at": NOW, "execution_token": "private-token", "lease_expires_at": NOW}


def search_result():
    return {"kb_id": 1, "generation": "a" * 32, "mode": "bm25", "sync_lag": True,
            "manifest": {"api_key": "secret-value"}, "contexts": [{
                "id": "mysql:4", "mysql_chunk_id": 4, "document_id": 1, "version_id": 2, "build_id": 5,
                "legacy_chunk_id": "legacy-chunk", "doc_id": "legacy-doc", "document_sha256": "b" * 64,
                "source_path": "policy/sample.pdf", "title": "测试文档", "heading_path": ["第一章"],
                "text": "测试正文", "score": 1.5, "ranks": {}, "structure_type": "body",
                "sources": [{"block_id": 1, "page": 1, "block_char_start": 0, "block_char_end": 4}],
                "derived_sources": [{"operation": "copy", "source_refs": [{"block_id": 1}]}], "asset_refs": []}]}


class HTTPAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="http-test-", dir=ROOT / "tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.management = Mock()
        self.processing = Mock()
        self.search = Mock()
        self.factory = Mock(return_value=self.search)
        self.runtime = Runtime(self.management, processing=self.processing, search_factory=self.factory)
        self.runtime.indexing = Mock()
        self.app = create_app(runtime=self.runtime, tmp_root=self.root, max_upload_bytes=2 * 1024 * 1024)
        self.client = self.enterContext(TestClient(self.app, base_url="http://127.0.0.1:8000"))
        self.submission = {"document_id": 1, "version_id": 2, "source_sha256": "b" * 64,
                           "active_build_id": None, "task_id": 3, "status": "pending", "build_id": None,
                           "reused_version": False, "reused_task": False}
        self.processing.register_and_submit.return_value = self.submission

    def upload(self, path="policy/sample.pdf", data=b"fixture pdf", **kwargs):
        return self.client.post("/api/v1/knowledge-bases/1/documents", data={"source_path": path},
                                files={"file": ("../../untrusted-name.exe", data, "application/octet-stream")}, **kwargs)

    def test_health_and_startup_are_independent_of_es_and_models(self):
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        self.management.db_check.assert_called_once_with()
        self.factory.assert_not_called()
        self.management.db_upgrade.assert_not_called()

    def test_management_page_assets_and_unknown_runtime_need_no_model(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("本地知识库", response.text)
        self.assertEqual(self.client.get("/assets/app.js").status_code, 200)
        with patch("rag.ops.state.state_root", return_value=self.root):
            status = self.client.get("/api/v1/runtime-status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["worker"]["state"], "unknown")
        self.factory.assert_not_called()
        self.assertEqual(self.client.get("/assets/../app.py").status_code, 404)

    def test_upload_stages_under_tmp_uses_logical_path_and_cleans_up(self):
        def register(kb_id, raw_root, path):
            self.assertEqual(kb_id, 1)
            self.assertTrue(raw_root.is_relative_to(self.root))
            self.assertEqual(path, "policy/sample.pdf")
            self.assertEqual((raw_root / path).read_bytes(), b"fixture pdf")
            return self.submission
        self.processing.register_and_submit.side_effect = register
        response = self.upload(path="policy\\./sample.pdf")
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.headers["location"], "/api/v1/processing-tasks/3")
        self.assertEqual(response.json(), self.submission)
        self.assertFalse(list(self.root.glob("http-upload-*")))

    def test_reused_terminal_task_does_not_retry_or_activate(self):
        for status in ("succeeded", "failed"):
            self.processing.register_and_submit.return_value = {**self.submission, "status": status,
                                                               "reused_task": True, "reused_version": True}
            response = self.upload()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["reused_task"])
        self.processing.retry.assert_not_called()
        self.management.activate_document.assert_not_called()

    def test_spooled_file_and_staging_cleanup_on_service_failure(self):
        self.processing.register_and_submit.side_effect = ManagementError("RESOURCE_DISABLED")
        before = set(self.root.rglob("*"))
        response = self.upload(data=b"x" * (1024 * 1024 + 1))
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(set(self.root.rglob("*")), before)

    def test_paths_formats_empty_and_multipart_shape(self):
        for path, expected in (("../outside.pdf", 422), ("D:/outside.pdf", 422), ("/outside.pdf", 422),
                               ("a/../outside.pdf", 422), ("sample.txt", 415)):
            with self.subTest(path=path):
                self.assertEqual(self.upload(path=path).status_code, expected)
        self.assertEqual(self.upload(data=b"").json()["error_code"], "EMPTY_UPLOAD")
        missing = self.client.post("/api/v1/knowledge-bases/1/documents", files={"file": ("a.pdf", b"x")})
        self.assertEqual(missing.status_code, 422)
        extra = self.client.post("/api/v1/knowledge-bases/1/documents", data={"source_path": "x.pdf", "model_path": "secret"},
                                 files={"file": ("x.pdf", b"x")})
        self.assertIn(extra.status_code, (400, 422))
        self.assertEqual(self.client.post("/api/v1/knowledge-bases/1/documents", json={}).status_code, 415)
        self.processing.register_and_submit.assert_not_called()

    def test_file_and_streamed_request_limits(self):
        oversized = self.upload(data=b"x" * (2 * 1024 * 1024 + 1))
        self.assertEqual(oversized.status_code, 413, oversized.text)
        response = self.client.post("/api/v1/knowledge-bases/1/documents", content=iter([b"x" * (4 * 1024 * 1024)]),
            headers={"content-type": "multipart/form-data; boundary=test"})
        self.assertEqual(response.status_code, 413, response.text)
        early = self.client.post("/api/v1/knowledge-bases/1/documents", content=b"x",
                                 headers={"content-length": str(4 * 1024 * 1024)})
        self.assertEqual(early.status_code, 413)
        self.processing.register_and_submit.assert_not_called()
        self.assertFalse(list(self.root.glob("http-upload-*")))

    def test_documents_versions_and_builds_use_public_dtos(self):
        self.management.list_records.return_value = page([document()])
        response = self.client.get("/api/v1/knowledge-bases/1/documents?status=disabled&limit=2&offset=3")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("source_key", response.text)
        self.assertTrue(response.json()["items"][0]["created_at"].endswith("Z"))
        self.management.list_records.assert_called_with("document", kb_id=1, status="disabled", limit=2, offset=3)
        self.management.show.return_value = {**document(), "active_chunk_count": 0, "active_eligible_chunk_count": 0}
        self.assertEqual(self.client.get("/api/v1/documents/1").status_code, 200)
        self.management.list_records.return_value = page([{"id": 2, "document_id": 1, "source_sha256": "a" * 64,
            "original_filename": "sample.pdf", "format": "pdf", "file_size": 20, "created_at": NOW,
            "storage_uri": "D:/private/raw"}])
        response = self.client.get("/api/v1/documents/1/versions")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("storage_uri", response.text)
        self.management.list_builds.return_value = page([{"id": 5, "document_version_id": 2, "legacy_doc_id": "legacy",
            "output_fingerprint": "a" * 64, "status": "ready", "created_at": NOW, "chunk_count": 1,
            "is_active": False, "can_activate": True, "pipeline_config_json": {"api_key": "secret-value"}}])
        response = self.client.get("/api/v1/documents/1/builds")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("secret-value", response.text)

    def test_activation_and_status_return_sync_target(self):
        target = {"kb_id": 1, "desired_revision": 2, "status": "pending", "paused": False}
        self.management.activate_document.return_value = {"document_id": 1, "previous_build_id": None,
                                                          "active_build_id": 5, "sync_target": target}
        response = self.client.put("/api/v1/documents/1/active-build", json={"build_id": 5})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["sync_target"], target)
        self.management.activate_document.assert_called_once_with(1, 5)
        self.management.set_status.return_value = {**document(), "status": "disabled", "sync_target": target}
        self.assertEqual(self.client.patch("/api/v1/documents/1/status", json={"status": "disabled"}).status_code, 200)
        self.assertEqual(self.client.patch("/api/v1/documents/1/status", json={"status": "archived"}).status_code, 422)
        self.assertEqual(self.client.put("/api/v1/documents/1/active-build", json={"build_id": True}).status_code, 422)

    def test_tasks_hide_configuration_and_execution_identity(self):
        task = processing_task()
        self.processing.show.return_value = {**task, "attempts": page([])}
        response = self.client.get("/api/v1/processing-tasks/3?limit=2&offset=1")
        self.assertEqual(response.status_code, 200, response.text)
        for secret in ("config_json", "config_sha256", "execution_token", "lease_expires_at", "secret-value", "D:/private"):
            self.assertNotIn(secret, response.text)
        self.assertFalse(response.json()["can_activate"])
        self.processing.show.assert_called_with(3, limit=2, offset=1)
        self.processing.list.return_value = page([task])
        response = self.client.get("/api/v1/knowledge-bases/1/processing-tasks?status=failed")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("config_json", response.text)

    def test_submit_and_retry_accept_no_client_configuration(self):
        self.processing.submit.return_value = {"task_id": 3, "status": "pending", "build_id": None, "reused": True}
        response = self.client.post("/api/v1/versions/2/processing-tasks")
        self.assertEqual(response.status_code, 202, response.text)
        self.processing.submit.assert_called_with(2)
        for endpoint in ("/versions/2/processing-tasks", "/processing-tasks/3/retry", "/knowledge-bases/1/index-sync/retry"):
            response = self.client.post("/api/v1" + endpoint, json={"model_path": "D:/private"})
            self.assertEqual(response.status_code, 422, response.text)
        self.processing.retry.return_value = {"task_id": 3, "status": "pending"}
        self.assertEqual(self.client.post("/api/v1/processing-tasks/3/retry").status_code, 202)
        self.processing.retry.side_effect = ManagementError("TASK_RETRY_NOT_ALLOWED")
        self.assertEqual(self.client.post("/api/v1/processing-tasks/3/retry").status_code, 409)

    def test_database_only_status_does_not_claim_alias_consistency(self):
        queue = {"kb_id": 1, "sync_task": sync_task(), "attempts": page([])}
        self.runtime.indexing.show.return_value = queue
        response = self.client.get("/api/v1/knowledge-bases/1/index-status")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["sync_lag"])
        self.assertFalse(response.json()["index_checked"])
        self.assertNotIn("execution_token", response.text)
        self.factory.assert_not_called()
        self.search.status.return_value = {**queue, "sync_lag": True, "generation": "a" * 32, "state": "ready",
                                           "chunk_count": 1, "manifest": {"secret-value": "hidden"}}
        response = self.client.get("/api/v1/knowledge-bases/1/index-status?db_only=false")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["index_checked"])
        self.assertTrue(response.json()["sync_lag"])
        self.assertNotIn("manifest", response.text)
        self.runtime.indexing.control.return_value = {"kb_id": 1, "sync_task": sync_task()}
        self.assertEqual(self.client.post("/api/v1/knowledge-bases/1/index-sync/retry").status_code, 202)

    def test_search_returns_references_and_lag_and_reuses_resources(self):
        self.search.query.return_value = search_result()
        for _ in range(2):
            response = self.client.post("/api/v1/knowledge-bases/1/search", json={"query": "正文", "mode": "bm25"})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertTrue(result["sync_lag"])
            self.assertEqual(result["contexts"][0]["sources"][0]["page"], 1)
            self.assertEqual(result["contexts"][0]["heading_path"], ["第一章"])
            self.assertNotIn("manifest", response.text)
        self.factory.assert_called_once_with()
        self.assertEqual(self.search.query.call_args.kwargs, {"mode": "bm25"})
        self.search.query.return_value = {**search_result(), "contexts": []}
        self.assertEqual(self.client.post("/api/v1/knowledge-bases/1/search", json={"query": "无结果"}).json()["contexts"], [])

    def test_validation_and_errors_do_not_expose_input(self):
        for payload in ({"query": " "}, {"query": "x", "top_k": True}, {"query": "x", "top_k": 51},
                        {"query": "x", "api_key": "secret-value"}, {"query": "x", "category": " "}):
            response = self.client.post("/api/v1/knowledge-bases/1/search", json=payload)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn("secret-value", response.text)
        malformed = self.client.post("/api/v1/knowledge-bases/1/search", content='{"secret-value":',
                                      headers={"content-type": "application/json"})
        self.assertEqual(malformed.status_code, 400)
        self.assertNotIn("secret-value", malformed.text)
        for path in ("/api/v1/documents/0", "/api/v1/documents/18446744073709551616",
                     "/api/v1/knowledge-bases/1/documents?limit=1001", "/api/v1/knowledge-bases/1/documents?offset=-1"):
            self.assertEqual(self.client.get(path).status_code, 422)

    def test_service_exceptions_are_safe_and_status_codes_are_stable(self):
        cases = [(ManagementError("RECORD_NOT_FOUND"), 404), (ManagementError("RESOURCE_DISABLED"), 409),
                 (ManagementError("BUILD_OWNERSHIP"), 409), (ManagementError("INDEX_NOT_FOUND"), 503),
                 (ManagementError("SEARCH_UNAVAILABLE"), 503),
                 (OperationalError("secret-value SELECT", {}, Exception("secret-value")), 503),
                 (ValueError("secret-value D:/private raw payload"), 500)]
        with patch("rag.api.errors.event") as log:
            for exc, status in cases:
                self.management.show.side_effect = exc
                response = self.client.get("/api/v1/documents/1")
                self.assertEqual(response.status_code, status, response.text)
                self.assertNotIn("secret-value", response.text)
                self.assertNotIn("secret-value", str(log.mock_calls))
        self.assertEqual(self.client.get("/no-route").status_code, 404)
        self.assertEqual(self.client.delete("/api/v1/documents/1").status_code, 405)

    def test_response_validation_errors_are_contained_without_tracebacks(self):
        self.search.query.return_value = {"raw": "secret-value"}
        with patch("rag.api.errors.event") as log:
            response = self.client.post("/api/v1/knowledge-bases/1/search", json={"query": "正文"})
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotIn("secret-value", response.text)
        self.assertNotIn("secret-value", str(log.mock_calls))

    def test_openapi_defines_all_routes_models_and_uniform_errors(self):
        spec = self.client.get("/openapi.json").json()
        count = 0
        for path, operations in spec["paths"].items():
            if not path.startswith("/api/v1"):
                continue
            for operation in operations.values():
                count += 1
                for status in ("400", "403", "404", "409", "413", "415", "422", "500", "503"):
                    schema = operation["responses"][status]["content"]["application/json"]["schema"]
                    self.assertEqual(schema, {"$ref": "#/components/schemas/Error"})
        self.assertEqual(count, 16)
        upload = spec["paths"]["/api/v1/knowledge-bases/{kb_id}/documents"]["post"]
        self.assertIn("multipart/form-data", upload["requestBody"]["content"])
        self.assertIn("200", upload["responses"])
        self.assertIn("202", upload["responses"])
        self.assertEqual(self.client.get("/docs").status_code, 200)

    def test_concurrent_queries_initialize_once_and_serialize_encoder_use(self):
        active = 0
        maximum = 0

        def query(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            time.sleep(.01)
            active -= 1
            return search_result()
        self.search.query.side_effect = query
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.runtime.query(1, SearchRequest(query="正文")), range(8)))
        self.assertEqual(maximum, 1)
        self.factory.assert_called_once_with()

    def test_lifespan_cleans_tempdir_and_owned_resources(self):
        previous = tempfile.tempdir
        with patch("rag.api.app.Settings.from_env") as settings:
            config = settings.return_value
            config.host, config.port, config.database = "127.0.0.1", 3306, "fixture"
            config.storage_root = self.root / "storage"
            management = Mock()
            with patch("rag.api.app.ManagementService", return_value=management):
                with TestClient(create_app(tmp_root=self.root), base_url="http://127.0.0.1:8000") as client:
                    self.assertNotEqual(tempfile.tempdir, previous)
                    self.assertEqual(client.get("/health").status_code, 200)
                config.engine.return_value.dispose.assert_called_once_with()
        self.assertEqual(tempfile.tempdir, previous)

    def test_startup_failure_is_sanitized_and_disposes_engine(self):
        previous = tempfile.tempdir
        with patch("rag.api.app.Settings.from_env") as settings, patch("rag.api.app.ManagementService") as management:
            management.return_value.db_check.side_effect = RuntimeError("password=secret-value")
            with self.assertRaisesRegex(RuntimeError, "^INTERNAL_ERROR$"):
                with TestClient(create_app(tmp_root=self.root), base_url="http://127.0.0.1:8000"):
                    self.fail("startup should have failed")
            settings.return_value.engine.return_value.dispose.assert_called_once_with()
        self.assertEqual(tempfile.tempdir, previous)

    def test_cross_origin_upload_is_rejected_before_parsing_or_side_effects(self):
        for origin in ("https://untrusted.example", "null", "http://localhost:5173", "http://127.0.0.1:8001"):
            with self.subTest(origin=origin):
                response = self.upload(headers={"origin": origin})
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["error_code"], "ORIGIN_NOT_ALLOWED")
                self.assertNotIn(origin, response.text)
        response = self.upload(headers={"sec-fetch-site": "cross-site"})
        self.assertEqual(response.status_code, 403)
        self.processing.register_and_submit.assert_not_called()
        self.assertFalse(list(self.root.glob("http-upload-*")))

    def test_dns_rebinding_and_invalid_host_are_rejected(self):
        for host in ("rebind.example:8000", "127.0.0.1.evil:8000", "localhost.evil", "user@localhost:8000",
                     "localhost:99999", "localhost/path", "localhost?", "localhost#", "localhost\t"):
            response = self.client.get("/health", headers={"host": host})
            self.assertEqual(response.status_code, 400, (host, response.text))
            self.assertEqual(response.json()["error_code"], "INVALID_HOST")
        response = self.client.get("/health", headers=[("host", "localhost:8000"), ("host", "evil.example")])
        self.assertEqual(response.status_code, 400)
        rebound = self.upload(headers={"host": "evil.example:8000", "origin": "http://evil.example:8000"})
        self.assertEqual(rebound.status_code, 400)
        self.processing.register_and_submit.assert_not_called()

    def test_same_origin_and_nonbrowser_clients_still_work(self):
        response = self.upload(headers={"origin": "http://127.0.0.1:8000", "sec-fetch-site": "same-origin"})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.upload().status_code, 202)
        for host in ("localhost:8000", "LOCALHOST:8000", "127.0.0.1:8000", "[::1]:8000"):
            self.assertEqual(self.client.get("/health", headers={"host": host}).status_code, 200)

    def test_opt_in_cors_allows_only_configured_loopback_browser(self):
        app = create_app(runtime=self.runtime, tmp_root=self.root,
                         allowed_browser_origins=["http://localhost:5173"])
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            headers = {"origin": "http://localhost:5173"}
            preflight = client.options("/api/v1/knowledge-bases/1/search", headers={**headers,
                "access-control-request-method": "POST", "access-control-request-headers": "content-type"})
            self.assertEqual(preflight.status_code, 200, preflight.text)
            self.assertEqual(preflight.headers["access-control-allow-origin"], headers["origin"])
            self.assertNotIn("access-control-allow-credentials", preflight.headers)
            self.search.query.return_value = search_result()
            response = client.post("/api/v1/knowledge-bases/1/search", json={"query": "正文"}, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers["access-control-allow-origin"], headers["origin"])
            self.assertEqual(response.headers["access-control-expose-headers"], "Location")
            self.search.query.side_effect = ValueError("secret-value")
            with patch("rag.api.errors.event"):
                failure = client.post("/api/v1/knowledge-bases/1/search", json={"query": "正文"}, headers=headers)
            self.assertEqual(failure.status_code, 500)
            self.assertEqual(failure.headers["access-control-allow-origin"], headers["origin"])
            self.assertNotIn("secret-value", failure.text)
            rejected = client.options("/health", headers={**headers, "access-control-request-method": "DELETE"})
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(rejected.json()["error_code"], "INVALID_CORS_PREFLIGHT")
            self.assertEqual(int(rejected.headers["content-length"]), len(rejected.content))
            untrusted = client.get("/health", headers={"origin": "http://localhost:5174"})
            self.assertEqual(untrusted.status_code, 403)
            self.assertNotIn("access-control-allow-origin", untrusted.headers)
            bad_host = client.options("/health", headers={**headers, "host": "evil.example",
                                                          "access-control-request-method": "GET"})
            self.assertEqual(bad_host.status_code, 400)

    def test_browser_origin_and_json_limit_configuration_validation(self):
        for origin in ("*", "null", "https://evil.example", "http://localhost:5173/", "http://localhost:5173#",
                       "http://localhost:5173?", "http://localhost\n", "http://localhost:0"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                create_app(allowed_browser_origins=[origin])
        for limit in (0, -1, True):
            with self.assertRaises(ValueError):
                create_app(max_json_bytes=limit)
        with patch.dict("os.environ", {"RAG_API_BROWSER_ORIGINS": "http://localhost:5173"}):
            with TestClient(create_app(runtime=self.runtime, tmp_root=self.root),
                            base_url="http://127.0.0.1:8000") as client:
                self.assertEqual(client.get("/health", headers={"origin": "http://localhost:5173"}).status_code, 200)

    def test_json_body_limit_is_independent_of_upload_limit(self):
        response = self.client.post("/api/v1/knowledge-bases/1/search", json={"query": "x" * (64 * 1024)})
        self.assertEqual(response.status_code, 413, response.text)
        response = self.client.post("/api/v1/knowledge-bases/1/search", content=iter([b'{"query":"', b'x' * 65536, b'"}']),
                                    headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 413, response.text)
        self.factory.assert_not_called()
        self.assertEqual(self.upload(data=b"x" * (128 * 1024)).status_code, 202)


if __name__ == "__main__":
    unittest.main()
