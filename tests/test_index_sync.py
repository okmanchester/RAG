"""Queue interfaces, safe ES retry classification and unified worker scheduling."""
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from elasticsearch import ApiError, ConnectionTimeout
from elastic_transport import ApiResponseMeta, NodeConfig

from rag.manage import dispatch, parser
from rag.management.errors import ManagementError, public_error
from rag.management.worker import run_worker
from rag.search.elastic import safe_es, ESStore


class IndexSyncUnitTests(unittest.TestCase):
    def test_db_only_status_and_controls_do_not_load_es_configuration(self):
        for arguments in (["index", "status", "--kb-id", "1", "--db-only", "--limit", "2"],
                          ["index", "pause", "--kb-id", "1"], ["index", "resume", "--kb-id", "1"],
                          ["index", "retry", "--kb-id", "1"]):
            args = parser().parse_args(arguments)
            with patch("rag.management.index_sync.IndexSyncService") as service, \
                    patch("rag.search.config.SearchSettings.from_env", side_effect=AssertionError("ES loaded")):
                dispatch(Mock(), args)
                self.assertTrue(service.return_value.show.called or service.return_value.control.called)
        self.assertEqual(parser().parse_args(["worker", "run"] ).kind, "all")

    def test_es_retryability_uses_status_and_exception_types(self):
        for status, retryable in ((401, False), (400, False), (429, True), (503, True), (500, True)):
            meta = ApiResponseMeta(status, "1.1", {}, .1, NodeConfig("http", "localhost", 9200))
            @safe_es
            def request():
                raise ApiError("secret-token", meta, {"error": "private payload"})
            with self.assertRaises(ManagementError) as caught:
                request()
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertNotIn("secret-token", str(public_error(caught.exception)))
        @safe_es
        def timeout():
            raise ConnectionTimeout("secret-token")
        with self.assertRaises(ManagementError) as caught:
            timeout()
        self.assertTrue(caught.exception.retryable)

    def test_bulk_mixed_permanent_and_transient_failures_do_not_retry(self):
        store = ESStore(Mock(), "namespace")
        for statuses, retryable in (((429, 503), True), ((429, 400), False)):
            with patch("rag.search.elastic.streaming_bulk", return_value=iter(
                    (False, {"index": {"status": status}}) for status in statuses)):
                with self.assertRaises(ManagementError) as caught:
                    store.bulk("index", [])
            self.assertEqual(caught.exception.retryable, retryable)

    def test_worker_alternates_ready_queues_without_loading_search_for_processing(self):
        with patch("rag.management.worker.ProcessingService") as processing, \
                patch("rag.management.worker.IndexSyncService") as indexing, \
                patch("rag.management.worker.print"):
            check = Mock(side_effect=[None, None, None, RuntimeError("stop")])
            processing.return_value.worker_lock.return_value = nullcontext(check)
            processing.return_value.claim.return_value = {"id": 1, "kb_id": 7, "attempt_count": 2}
            indexing.return_value.claim.return_value = {"knowledge_base_id": 8, "desired_revision": 3, "attempt_count": 4}
            order = []
            processing.return_value.execute.side_effect = lambda *a: order.append("processing") or {"status": "succeeded"}
            indexing.return_value.execute.side_effect = lambda *a: order.append("index") or {"status": "succeeded"}
            with patch("rag.management.worker.event") as events:
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    run_worker(Mock())
                completed = [call.kwargs for call in events.call_args_list if call.args[1] == "task_completed"]
            self.assertEqual(order, ["processing", "index", "processing"])
            self.assertEqual(completed[0]["kb_id"], 7)
            self.assertEqual(completed[0]["task_id"], 1)
            self.assertEqual(completed[1]["kb_id"], 8)
            self.assertEqual(completed[1]["revision"], 3)
            self.assertNotIn("task_id", completed[1])

    def test_processing_only_worker_does_not_claim_index_queue(self):
        with patch("rag.management.worker.ProcessingService") as processing, \
                patch("rag.management.worker.IndexSyncService") as indexing:
            processing.return_value.worker_lock.return_value = nullcontext(lambda: None)
            processing.return_value.claim.return_value = None
            self.assertEqual(run_worker(Mock(), kind="processing", once=True), {"status": "idle"})
            indexing.return_value.claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
