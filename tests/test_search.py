"""Projection, fusion and dependency failure contracts without live services."""
import copy
import unittest
from unittest.mock import Mock, patch

from elasticsearch import ConnectionTimeout

from rag.management.errors import ManagementError, public_error
from rag.manage import parser
from rag.search.config import SearchSettings
from rag.search.corpus import ManagedCorpus, Selection
from rag.search.elastic import ESStore, rrf
from rag.search.shared import context


class SearchTests(unittest.TestCase):
    def test_rrf_shared_evidence_outranks_single_branch_and_has_diagnostics(self):
        def hit(identifier):
            return {"_id": str(identifier), "_score": 99, "_source": {"legacy_chunk_id": str(identifier)}}
        dense, lexical = [hit(1), hit(2)], [hit(3), hit(2)]
        before = copy.deepcopy((dense, lexical))
        result = rrf(dense, lexical)
        self.assertEqual([h["_id"] for h in result], ["2", "1", "3"])
        self.assertEqual(result[0]["ranks"], {"dense": 2, "bm25": 2})
        self.assertAlmostEqual(result[0]["_score"], 2 / 62)
        self.assertEqual((dense, lexical), before)

    def test_mysql_identity_is_not_legacy_identity(self):
        selection = Selection(1, "active", ({"build_id": 7, "category": "policy"},))
        row = {"id": 42, "document_id": 3, "build_id": 7, "legacy_doc_id": "old-doc",
               "legacy_chunk_id": "old-chunk", "source_sha256": "a" * 64,
               "structure_type": "table", "text": "text", "payload_json": {"sources": []}}
        result = ManagedCorpus.projection(row, selection)
        self.assertEqual((result["chunk_id"], result["legacy_chunk_id"]), (42, "old-chunk"))
        self.assertEqual(result["kb_id"], 1)

    def test_shared_context_preserves_all_source_kinds_without_mutating_payload(self):
        chunk = {"chunk_id": "legacy", "doc_id": "doc", "text": "text", "structure_type": "table",
                 "sources": [{"block_id": 1}], "context_sources": [{"block_id": 2}],
                 "derived_sources": [{"source_refs": [{"block_id": 3}]}], "asset_refs": ["image.png"]}
        saved = copy.deepcopy(chunk)
        value = context(chunk, "sha", identifier="mysql:42")
        self.assertEqual(value["sources"], [{"block_id": 1}, {"block_id": 2}, {"block_id": 3}])
        self.assertEqual(value["asset_refs"], ["image.png"])
        self.assertEqual(chunk, saved)

    def test_bulk_partial_failure_is_not_success_or_publication(self):
        client = Mock()
        store = ESStore(client, "test")
        with patch("rag.search.elastic.streaming_bulk", return_value=iter([(True, {}), (False, {})])):
            with self.assertRaisesRegex(ManagementError, "failed items"):
                store.bulk("candidate", [])
        client.indices.update_aliases.assert_not_called()

    def test_dependency_errors_never_expose_secret_exception_text(self):
        client = Mock()
        client.indices.get_alias.side_effect = ConnectionTimeout("password=secret private-host")
        with self.assertRaises(ManagementError) as caught:
            ESStore(client, "test").current(1)
        self.assertEqual(public_error(caught.exception)["error_code"], "SEARCH_UNAVAILABLE")
        self.assertNotIn("secret", str(public_error(caught.exception)))

    def test_generation_input_cannot_select_another_kb_or_wildcard(self):
        for value in ("*", "../other", "ABC", "rag-kb-1", "a" * 33):
            with self.subTest(value=value), self.assertRaises(ManagementError):
                ESStore(Mock(), "test").generation_name(1, value)

    def test_config_rejects_credential_urls_and_remote_plaintext(self):
        for url in ("http://user:secret@localhost:9200", "http://remote:9200", "http://localhost:9200?password=x"):
            with patch.dict("os.environ", {"ELASTIC_PASSWORD": "secret", "ELASTICSEARCH_URL": url}), self.assertRaises(ManagementError):
                SearchSettings.from_env("nonexistent")

    def test_cli_defaults_and_filters(self):
        args = parser().parse_args(["search", "--kb-id", "1", "--query", "测试", "--category", "policy"])
        self.assertEqual((args.mode, args.top_k, args.category), (None, 10, "policy"))
        args = parser().parse_args(["index", "rollback", "--kb-id", "1", "--generation", "a" * 32])
        self.assertEqual(args.action, "rollback")
