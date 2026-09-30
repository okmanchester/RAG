import json
import tempfile
import unittest
import uuid
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rag_baseline.benchmark.core import digest, write_json, write_jsonl
from rag_baseline.benchmark.first_run import (DenseIndex, extract_citations, publish_scores, run_baseline,
                                              select_smoke_ids, verify_uploaded)


class FirstRunTests(unittest.TestCase):
    def test_smoke_selection_covers_answer_categories_cross_document_and_boundary(self):
        rows = [
            {"id": "a", "split": "dev", "category": "policy", "expected_behavior": "answer", "evidence_span": "single_unit"},
            {"id": "b", "split": "dev", "category": "technical", "expected_behavior": "answer", "evidence_span": "single_unit"},
            {"id": "c", "split": "dev", "category": "technical", "expected_behavior": "answer", "evidence_span": "cross_document"},
            {"id": "d", "split": "dev", "category": "policy", "expected_behavior": "correct_premise", "evidence_span": "within_document"},
            {"id": "e", "split": "dev", "category": "policy", "expected_behavior": "answer", "evidence_span": "within_document"},
            {"id": "f", "split": "dev", "category": "technical", "expected_behavior": "answer", "evidence_span": "within_document"},
            {"id": "holdout", "split": "holdout", "category": "policy", "expected_behavior": "answer", "evidence_span": "single_unit"},
        ]
        selected = select_smoke_ids(rows)
        self.assertEqual(set(selected), {"a", "b", "c", "d", "e", "f"})
        self.assertEqual(selected, select_smoke_ids(list(reversed(rows))))

    def test_citations_only_map_valid_context_numbers(self):
        contexts = [{"id": "chunk-a"}, {"id": "chunk-b"}]
        self.assertEqual(extract_citations("应当检查[1][2]。另一项没有证据[9]。", contexts),
                         [{"claim": "应当检查", "context_ids": ["chunk-a", "chunk-b"]}])

    def test_score_upload_contains_no_answer_or_context(self):
        with tempfile.TemporaryDirectory() as root:
            output, evaluation = Path(root) / "smoke", Path(root) / "eval"
            write_json(output / "trace_map.json", {"question-1": "trace-1"})
            write_json(output / "run_manifest.json", {"identity": {"pipeline_version": "test"}})
            write_jsonl(evaluation / "scores.jsonl", [{"id": "question-1", "metrics": {"faithfulness": 0.5,
                        "missing": None}, "judgment": {"reason": "private answer and context"}}])
            client = Mock()
            client.api.scores_v3.get_many_v3.return_value.data = []
            client.api.scores.create.return_value.id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                f"{digest({'pipeline_version': 'test'})}:question-1:faithfulness"))
            self.assertEqual(publish_scores(output, evaluation, client), 1)
            payload = json.dumps(client.api.scores.create.call_args.kwargs)
            self.assertNotIn("private answer and context", payload)
            self.assertEqual(client.api.scores.create.call_args.kwargs["name"], "faithfulness")
            self.assertEqual(client.api.scores.create.call_args.kwargs["trace_id"], "trace-1")

    def test_index_only_contains_retrieval_enabled_chunks(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            model = base / "model"
            model.mkdir()
            (model / "config.json").write_text("{}")
            (model / "pytorch_model.bin").write_bytes(b"test")
            release = base / "release"
            write_jsonl(release / "documents.jsonl", [{"doc_id": "doc", "sha256": "sha", "blocks": []}])
            write_jsonl(release / "chunks.jsonl", [
                {"chunk_id": "body", "doc_id": "doc", "text": "body", "sources": [], "retrieval_enabled": True},
                {"chunk_id": "toc", "doc_id": "doc", "text": "toc", "sources": [], "retrieval_enabled": False},
            ])
            encoder = Mock()
            encoder.encode.side_effect = [np.array([[1., 0.]], dtype="float32"), np.array([[1., 0.]], dtype="float32")]
            with patch("rag_baseline.benchmark.first_run.MODEL_PATH", str(model)), \
                 patch("sentence_transformers.SentenceTransformer", return_value=encoder):
                index = DenseIndex(release, base / "cache")
                self.assertEqual(len(index.chunks), 1)
                self.assertEqual(index.search("question")[0]["id"], "body")

    def test_experiment_receives_id_only_and_resume_skips_model(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            release, output = base / "release", base / "run"
            write_json(release / "manifest.json", {"gold_sha256": "gold"})
            write_jsonl(release / "dev" / "queries.jsonl", [{"id": "q1", "user_input": "private question"}])
            index = Mock(identity={"index_version": 1})
            index.search.return_value = [{"id": "c1", "text": "private context", "doc_id": "doc",
                                          "document_sha256": "sha", "sources": []}]
            langfuse = Mock()
            langfuse.start_as_current_observation.side_effect = lambda **kwargs: nullcontext(Mock())
            def experiment(**kwargs):
                self.assertEqual(kwargs["data"], [{"input": "q1"}])
                result = kwargs["task"](item=kwargs["data"][0])
                self.assertEqual(set(result), {"question_id", "status"})
                return SimpleNamespace(item_results=[SimpleNamespace(item=kwargs["data"][0], trace_id="trace-1")])
            langfuse.run_experiment.side_effect = experiment
            with patch("openai.OpenAI"), patch("rag_baseline.benchmark.first_run.answer_question",
                                                return_value=("private answer[1]。", {"total_tokens": 3})) as answer, \
                 patch("rag_baseline.benchmark.first_run.validate_run"):
                run_baseline(release, ["q1"], output, index, langfuse)
                run_baseline(release, ["q1"], output, index, langfuse)
            self.assertEqual(answer.call_count, 1)
            self.assertEqual(json.loads((output / "trace_map.json").read_text()), {"q1": "trace-1"})

    def test_remote_verification_uses_current_read_apis_and_checks_privacy(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            release, output = base / "release", base / "smoke"
            write_jsonl(release / "dev" / "queries.jsonl", [{"id": "q1", "user_input": "private question"}])
            write_json(output / "trace_map.json", {"q1": "trace-1"})
            write_json(output / "run_manifest.json", {"identity": {"pipeline_version": "test"}})
            write_jsonl(output / "runs.jsonl", [{"id": "q1", "response": "private answer",
                                                  "retrieved_contexts": [{"text": "private context"}]}])
            write_jsonl(output / "evaluation" / "scores.jsonl", [{"id": "q1", "metrics": {"faithfulness": 0.5}}])
            score_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                     f"{digest({'pipeline_version': 'test'})}:q1:faithfulness"))
            remote = SimpleNamespace(id=score_id, subject=SimpleNamespace(kind="trace", id="trace-1"),
                                     name="faithfulness", value=0.5)
            client = Mock()
            client.api.observations.get_many.return_value.data = [Mock(model_dump=Mock(return_value={
                "trace_id": "trace-1", "input": "q1", "output": {"status": "ok"}}))]
            client.api.scores_v3.get_many_v3.return_value.data = [remote]
            self.assertEqual(verify_uploaded(output, release, client)["status"], "complete")
            client.api.observations.get_many.assert_called_once()
            client.api.scores_v3.get_many_v3.assert_called_once()
            client.api.observations.get_many.return_value.data[0].model_dump.return_value["input"] = "private question"
            with self.assertRaisesRegex(ValueError, "private content"):
                verify_uploaded(output, release, client)


if __name__ == "__main__":
    unittest.main()
