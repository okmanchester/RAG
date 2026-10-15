"""Opt-in tests against disposable MySQL databases and ES index namespaces."""
import copy
import json
import os
import unittest
from unittest.mock import patch

import numpy as np
from elasticsearch import ConnectionTimeout

from rag.management.errors import ManagementError
from rag.search.config import SearchSettings
from rag.search.service import SearchService
from rag.search.shared import SearchFilters
import test_management_mysql as mysql_tests
from test_management_storage import write_corpus
from test_search_selection import summaries
from rag.search.quality import default_mode_gate
from scripts.evaluate_managed_retrieval import promote_default


class FixtureEncoder:
    identity = {"dimensions": 2, "model": "test-local", "normalize_embeddings": True}

    def documents(self, texts):
        return np.array([[0., 1.] if "新版" in value else [1., 0.] for value in texts], dtype="float32")

    def query(self, question):
        return self.documents([question])[0]


@unittest.skipUnless(os.environ.get("RAG_RUN_MYSQL_TESTS") == "1" and os.environ.get("RAG_RUN_ES_TESTS") == "1",
                     "set RAG_RUN_MYSQL_TESTS=1 and RAG_RUN_ES_TESTS=1 for isolated integration tests")
class ManagedSearchIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mysql_tests.MySQLManagementTests.setUpClass.__func__(cls)
        try:
            cls.client = SearchSettings.from_env().client()
            cls.client.info()
            cls.search = SearchService(cls.engine, cls.client, encoder=FixtureEncoder(), prefix="rag-test")
        except BaseException:
            mysql_tests.MySQLManagementTests.tearDownClass.__func__(cls)
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            pattern = f"rag-test-kb-{cls.search.corpus.namespace}-*"
            names = list(cls.client.indices.get_mapping(index=pattern, allow_no_indices=True))
            for name in names:
                if not name.startswith(pattern[:-1]) or "-g-" not in name:
                    raise RuntimeError("refusing to delete an index outside this test namespace")
                cls.client.indices.delete(index=name)
            cls.client.close()
        finally:
            mysql_tests.MySQLManagementTests.tearDownClass.__func__(cls)

    def setUp(self):
        mysql_tests.MySQLManagementTests.setUp(self)
        run = self.service.import_corpus(self.kb, self.corpus, self.raw)
        self.service.activate_import(run["id"])
        self.document = run["results_json"][0]["document_id"]
        self.build = run["results_json"][0]["build_id"]
        self.filters = SearchFilters(self.kb)

    def query(self, mode="dense"):
        return self.search.query("正文内容", filters=self.filters, mode=mode)

    def new_build(self):
        docs, chunks = copy.deepcopy(self.docs), copy.deepcopy(self.chunks)
        chunks[0].update(chunk_id="new-legacy", text="新版\n正文内容")
        write_corpus(self.corpus, documents=docs, chunks=chunks)
        run = self.service.import_corpus(self.kb, self.corpus, self.raw)
        return run["results_json"][0]["build_id"]

    def test_missing_index_and_empty_index_are_distinct(self):
        self.assertEqual(self.search.status(self.kb)["state"], "missing")
        with self.assertRaisesRegex(ManagementError, "index is missing"):
            self.query()
        self.service.set_status("document", self.document, "disabled")
        result = self.search.sync(self.kb)
        self.assertEqual(result["chunk_count"], 0)
        self.assertFalse(result["sync_lag"])
        self.assertEqual(self.query()["contexts"], [])

    def test_full_sync_repeat_sync_and_authoritative_sources(self):
        first = self.search.sync(self.kb)
        hit = self.query()["contexts"][0]
        self.assertEqual(hit["legacy_chunk_id"], "legacy_chunk")
        self.assertTrue(hit["id"].startswith("mysql:"))
        self.assertEqual(hit["derived_sources"], self.chunks[0]["derived_sources"])
        second = self.search.sync(self.kb)
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertEqual(first["manifest"]["content_sha256"], second["manifest"]["content_sha256"])
        self.assertFalse(second["sync_lag"])
        self.assertEqual(len(second["generations"]), 2)

    def test_document_and_kb_disable_take_effect_without_sync(self):
        self.search.sync(self.kb)
        self.service.set_status("document", self.document, "disabled")
        result = self.query()
        self.assertEqual(result["contexts"], [])
        self.assertTrue(result["sync_lag"])
        self.service.set_status("document", self.document, "active")
        self.assertEqual(len(self.query()["contexts"]), 1)
        self.service.set_status("kb", self.kb, "archived")
        with self.assertRaisesRegex(ManagementError, "disabled"):
            self.query("bm25")
        self.assertTrue(self.search.status(self.kb)["sync_lag"])

    def test_version_switch_and_index_rollback_never_revive_old_build(self):
        first = self.search.sync(self.kb)
        new = self.new_build()
        self.service.activate_document(self.document, new)
        self.assertEqual(self.query()["contexts"], [])
        self.assertTrue(self.query()["sync_lag"])
        self.search.sync(self.kb)
        self.assertEqual(self.query()["contexts"][0]["build_id"], new)
        self.search.rollback(self.kb, first["generation"])
        self.assertEqual(self.query()["contexts"], [])
        self.assertEqual(self.service.show("document", self.document)["active_build_id"], new)
        self.service.activate_document(self.document, self.build)
        self.assertEqual(self.query()["contexts"][0]["build_id"], self.build)

    def test_kb_isolation_even_when_legacy_ids_are_identical(self):
        self.search.sync(self.kb)
        other = self.service.kb_create("other", "其他") ["id"]
        run = self.service.import_corpus(other, self.corpus, self.raw)
        self.service.activate_import(run["id"])
        self.search.sync(other)
        first = self.query()["contexts"][0]
        second = self.search.query("正文内容", filters=SearchFilters(other))["contexts"][0]
        self.assertEqual(first["legacy_chunk_id"], second["legacy_chunk_id"])
        self.assertNotEqual(first["id"], second["id"])
        with self.assertRaises(ManagementError):
            self.search.rollback(other, self.search.status(self.kb)["generation"])

    def test_toc_exclusion_and_metadata_filters(self):
        docs, chunks = copy.deepcopy(self.docs), copy.deepcopy(self.chunks)
        docs[0]["chunk_count"] = 2
        toc = {**chunks[0], "chunk_id": "toc", "chunk_index": 2, "retrieval_enabled": False}
        chunks.append(toc)
        write_corpus(self.corpus, documents=docs, chunks=chunks)
        run = self.service.import_corpus(self.kb, self.corpus, self.raw)
        self.service.activate_import(run["id"])
        self.assertEqual(self.search.sync(self.kb)["chunk_count"], 1)
        self.assertEqual(self.search.query("正文", filters=SearchFilters(self.kb, "technical"))["contexts"], [])
        self.assertEqual(len(self.query("hybrid")["contexts"]), 1)
        self.assertEqual(self.query("hybrid")["contexts"][0]["ranks"], {"dense": 1, "bm25": 1})

    def test_failure_during_verification_preserves_published_alias(self):
        first = self.search.sync(self.kb)
        with patch.object(self.search.store, "verify", side_effect=ManagementError("INDEX_CORRUPT")):
            with self.assertRaises(ManagementError):
                self.search.sync(self.kb)
        result = self.search.status(self.kb)
        self.assertEqual(result["generation"], first["generation"])
        building = next(g for g in result["generations"] if g["state"] == "failed")
        with self.assertRaises(ManagementError):
            self.search.rollback(self.kb, building["generation"])

    def test_real_bulk_partial_failure_preserves_alias_and_is_recorded(self):
        first = self.search.sync(self.kb)
        docs, chunks = copy.deepcopy(self.docs), copy.deepcopy(self.chunks)
        docs[0]["chunk_count"] = 2
        chunks.append({**chunks[0], "chunk_id": "second", "chunk_index": 2})
        write_corpus(self.corpus, documents=docs, chunks=chunks)
        run = self.service.import_corpus(self.kb, self.corpus, self.raw)
        self.service.activate_import(run["id"])
        projection = self.search.corpus.projection
        def malformed(row, selection):
            doc = projection(row, selection)
            return {**doc, "unexpected_field": "reject"} if row["legacy_chunk_id"] == "second" else doc
        with patch.object(self.search.corpus, "projection", side_effect=malformed):
            with self.assertRaises(ManagementError) as caught:
                self.search.sync(self.kb)
        self.assertEqual(caught.exception.code, "INDEX_BULK_FAILED")
        status = self.search.status(self.kb)
        self.assertEqual(status["generation"], first["generation"])
        failure = next(g for g in status["generations"] if g["state"] == "failed")
        self.assertEqual(failure["error_code"], "INDEX_BULK_FAILED")
        name = self.search.store.generation_name(self.kb, failure["generation"])
        self.client.indices.refresh(index=name)
        self.assertEqual(self.client.count(index=name)["count"], 1)

    def test_interrupted_alias_response_is_reconciled(self):
        real = self.client.indices.update_aliases
        def interrupted(**kwargs):
            real(**kwargs)
            raise ConnectionTimeout("interrupted after commit")
        with patch.object(self.client.indices, "update_aliases", side_effect=interrupted):
            self.assertEqual(self.search.sync(self.kb)["state"], "ready")

    def test_management_change_during_build_is_safe_and_reports_lag(self):
        first = self.search.sync(self.kb)
        encode = self.search.encoder.documents
        def changing(texts):
            self.service.set_status("document", self.document, "disabled")
            return encode(texts)
        with patch.object(self.search.encoder, "documents", side_effect=changing):
            with self.assertRaises(ManagementError) as caught:
                self.search.sync(self.kb)
        self.assertEqual(caught.exception.code, "INDEX_SYNC_SUPERSEDED")
        result = self.search.status(self.kb)
        self.assertEqual(result["generation"], first["generation"])
        self.assertTrue(result["sync_lag"])
        self.assertEqual(self.query()["contexts"], [])

    def test_management_change_during_search_is_rechecked(self):
        self.search.sync(self.kb)
        real = self.search.store.search
        def changing(*args):
            result = real(*args)
            self.service.set_status("document", self.document, "disabled")
            return result
        with patch.object(self.search.store, "search", side_effect=changing):
            self.assertEqual(self.query()["contexts"], [])

    def test_index_lock_and_model_mismatch_fail_closed(self):
        self.search.sync(self.kb)
        with self.search.corpus.index_lock(self.kb), self.assertRaisesRegex(ManagementError, "another index"):
            self.search.sync(self.kb)
        with patch.object(self.search.encoder, "identity", {"dimensions": 2, "model": "different"}):
            with self.assertRaisesRegex(ManagementError, "differs"):
                self.query()

    def test_best_mode_promotion_changes_actual_default_and_rejects_stale_decision(self):
        status = self.search.sync(self.kb)
        report = {"identity": {"kb_id": self.kb, "generation": status["generation"],
                   "selection_sha256": status["selection_sha256"], "index_manifest": status["manifest"]},
                  "reports": summaries(), "gate": default_mode_gate(summaries())}
        path = self.root / "selection_report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        self.assertTrue(promote_default(self.search, report, path))
        result = self.search.query("正文内容", filters=self.filters)
        self.assertEqual(result["mode"], "bm25")
        self.assertEqual(len(result["contexts"]), 1)
        self.assertEqual(self.search.status(self.kb)["manifest"]["default_mode_selection_policy"], 2)
        with self.assertRaises(ManagementError):
            promote_default(self.search, report, path)
