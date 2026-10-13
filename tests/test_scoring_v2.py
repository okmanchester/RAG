import asyncio
import copy
import io
import json
import math
import os
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

os.environ["RAGAS_DO_NOT_TRACK"] = "true"

from rag.benchmark.core import digest, file_hash, write_json, write_jsonl
from rag.benchmark.evaluation import (evidence_hit, evaluate_release, load_faithfulness_cache,
                                              protocol_comparison, summarize_scores)
from rag.benchmark.first_run import publish_scores, remote_score_identity
from rag.benchmark.generation import JUDGE_CONFIG, Probe, dependency_versions
from rag.benchmark.scoring_v2 import (JudgmentV2, JudgmentRepair, apply_judgment_repair,
                                             contains_quote, extract_citations_v2, grounding_issues,
                                             match_text, score_judgment_v2, validate_metrics)


class ScoringV2Tests(unittest.TestCase):
    def setUp(self):
        self.item = {"answer_points": ["应当检查"], "expected_behavior": "answer"}
        self.row = {"response": "应当检查[1]。还须记录。", "retrieved_contexts": [{"id": "c1", "text": "应当检查并记录。"}], "citations": []}
        self.data = {"point_verdicts": ["correct"], "point_response_quotes": ["应当检查"], "citation_verdicts": [True],
                     "claims": [{"claim": "检查", "response_quote": "应当检查", "citation_index": 0, "citation_supported": True,
                                 "supports": [{"citation_number": 1, "context_id": "c1", "support_quote": "应当检查"}]},
                                {"claim": "记录", "response_quote": "还须记录", "citation_index": None, "citation_supported": False, "supports": []}],
                     "boundary_correct": None, "reason": "test"}

    def score(self, data=None):
        return score_judgment_v2(self.item, self.row, JudgmentV2(**(data or self.data)))

    def test_two_of_ten_and_credit_cases(self):
        for verdicts, accuracy, completeness, missing, all_correct in (
            (["correct"]*2+["missing"]*8, 1, .2, .8, 0),
            (["correct", "partial", "incorrect", "missing"], .5, .375, .25, 0),
            (["missing"]*3, 0, 0, 1, 0),
            (["correct"]*3, 1, 1, 0, 1),
        ):
            self.item["answer_points"] = ["p"]*len(verdicts)
            data = copy.deepcopy(self.data)
            data["point_verdicts"] = verdicts
            data["point_response_quotes"] = ["" if v == "missing" else "应当检查" for v in verdicts]
            result = self.score(data)
            for metric, value in (("answer_point_accuracy", accuracy), ("answer_point_completeness", completeness),
                                  ("answer_point_missing_rate", missing), ("answer_all_points_correct", all_correct)):
                self.assertEqual(result[metric], value)

    def test_binding_instead_of_trusting_boolean(self):
        self.assertEqual(self.score()["citation_coverage"], .5)
        cases = []
        for quote_field, quote in (("point_response_quotes", "虚构回答"),):
            data = copy.deepcopy(self.data); data[quote_field] = [quote]; cases.append(data)
        for field, value in (("response_quote", "虚构回答"), ("citation_index", None)):
            data = copy.deepcopy(self.data); data["claims"][0][field] = value; cases.append(data)
        for field, value in (("citation_number", 99), ("context_id", "c2"), ("support_quote", "不存在的支持原文")):
            data = copy.deepcopy(self.data); data["claims"][0]["supports"][0][field] = value; cases.append(data)
        data = copy.deepcopy(self.data); data["claims"].append(data["claims"][0]); cases.append(data)
        data = copy.deepcopy(self.data); data["claims"][0]["citation_supported"] = False; cases.append(data)
        data = copy.deepcopy(self.data); data["claims"][0]["supports"] = []; cases.append(data)
        data = copy.deepcopy(self.data); data["point_verdicts"] = ["missing"]; cases.append(data)
        data = copy.deepcopy(self.data); data["point_response_quotes"] = []; cases.append(data)
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.score(data)

    def test_bad_references_remain_in_denominator(self):
        self.row["response"] = "应当检查[1]。还须记录[999]。"
        citations = extract_citations_v2(self.row["response"], self.row["retrieved_contexts"])
        self.assertEqual(citations[1]["invalid_numbers"], [999])
        self.data["citation_verdicts"] = [True, False]
        self.data["claims"][1]["citation_index"] = 1
        self.assertEqual(self.score()["citation_support"], .5)
        self.data["citation_verdicts"][1] = True
        with self.assertRaises(ValueError):
            self.score()

    def test_actual_position_and_multi_reference(self):
        self.row["response"] = "应当检查[1][2]。还须记录[2]。"
        self.row["retrieved_contexts"].append({"id": "c2", "text": "应当检查并记录。"})
        self.data["citation_verdicts"] = [True, False]
        self.data["claims"][0]["supports"][0].update(citation_number=2, context_id="c2")
        self.assertEqual(self.score()["citation_coverage"], .5)
        self.data["claims"][0]["citation_index"] = 1
        with self.assertRaisesRegex(ValueError, "attachment"):
            self.score()

    def test_response_quote_can_include_actual_citation_marker(self):
        self.data["claims"][0]["response_quote"] = "应当检查[1]。"
        self.assertEqual(self.score()["citation_coverage"], .5)
        self.data["claims"][0]["response_quote"] = "应当检查[2]"
        with self.assertRaises(ValueError):
            self.score()

    def test_targeted_model_repair_validated_as_complete_judgment(self):
        original = JudgmentV2(**dict(self.data, point_response_quotes=["虚构回答"]))
        repair = JudgmentRepair(point_fixes=[{"index": 0, "verdict": "correct", "response_quote": "应当检查"}],
                                claim_fixes=[], citation_fixes=[], boundary_fix=None, reason="修复")
        fixed = apply_judgment_repair(original, repair)
        self.assertEqual(score_judgment_v2(self.item, self.row, fixed)["answer_point_accuracy"], 1)
        repair.point_fixes[0].index = 3
        with self.assertRaisesRegex(ValueError, "out of range"):
            apply_judgment_repair(original, repair)

    def test_repair_preserves_boundary_unless_explicitly_changed(self):
        original = JudgmentV2(**dict(self.data, boundary_correct=True))
        repair = JudgmentRepair(point_fixes=[], claim_fixes=[], citation_fixes=[], boundary_fix=None, reason="未改边界")
        self.assertTrue(apply_judgment_repair(original, repair).boundary_correct)
        repair = JudgmentRepair(point_fixes=[], claim_fixes=[], citation_fixes=[], boundary_fix={"value": None}, reason="非边界")
        self.assertIsNone(apply_judgment_repair(original, repair).boundary_correct)

    def test_same_words_at_different_citation_positions_are_distinct(self):
        self.row = {"response": "甲指标：单位台[1]。乙指标：单位台[2]。",
                    "retrieved_contexts": [{"id": "a", "text": "甲指标单位台"}, {"id": "b", "text": "乙指标单位台"}], "citations": []}
        self.data.update(point_response_quotes=["单位台"], citation_verdicts=[True, True], claims=[
            {"claim": f"{label}指标单位为台", "response_quote": "单位台", "citation_index": ci, "citation_supported": True,
             "supports": [{"citation_number": ci+1, "context_id": identifier, "support_quote": f"{label}指标单位台"}]}
            for ci, label, identifier in ((0, "甲", "a"), (1, "乙", "b"))])
        self.assertEqual(self.score()["citation_coverage"], 1)

    def test_sentence_end_reference_covers_semicolon_clauses(self):
        self.row["response"] = "应当检查；还须记录[1]。"
        self.data["claims"][1].update(citation_index=0, citation_supported=True,
            supports=[{"citation_number": 1, "context_id": "c1", "support_quote": "记录"}])
        self.assertEqual(self.score()["citation_coverage"], 1)

    def test_all_errors_returned_with_verbatim_repair_candidates(self):
        self.data["point_response_quotes"] = ["原文不存在"]
        self.data["claims"][0]["supports"][0]["support_quote"] = "检查后授权"
        issues = grounding_issues(self.item, self.row, JudgmentV2(**self.data))
        self.assertEqual(len(issues), 2)
        for issue in issues:
            self.assertTrue(issue["verbatim_candidates"])
        self.data["citation_verdicts"] = [True, True, True]
        with self.assertRaisesRegex(ValueError, "cardinality"):
            self.score()

    def test_empty_claims_zero_and_boundary(self):
        data = copy.deepcopy(self.data); data["claims"] = []; data["citation_verdicts"] = [False]
        self.assertEqual(self.score(data)["citation_coverage"], 0)
        self.item["expected_behavior"] = "abstain"
        data["boundary_correct"] = True
        self.assertEqual(self.score(data), {"boundary_behavior_accuracy": 1})
        data["boundary_correct"] = None
        with self.assertRaisesRegex(ValueError, "Boundary"):
            self.score(data)

    def test_semantic_punctuation_and_unicode(self):
        for text, quote in (("9.5", "95"), ("-5", "5"), ("5%", "5"), ("5", "5%"), ("95", "5"), ("9.5", "5")):
            with self.subTest(text=text, quote=quote):
                self.assertFalse(contains_quote(text, quote))
        self.assertTrue(contains_quote("9.5", "9.5"))
        self.assertTrue(contains_quote("e\u0301\r\n甲", "é\n甲"))
        self.assertEqual(match_text("-5%！"), "-5%！")
        evidence = {"doc_id": "d", "document_sha256": "h", "text": "数值9.5%", "block_id": 1, "block_char_start": 0, "block_char_end": 6}
        contexts = [{"doc_id": "d", "document_sha256": "h", "text": "数值95%"}]
        self.assertTrue(evidence_hit(evidence, contexts, 1))
        self.assertFalse(evidence_hit(evidence, contexts, 2))

    def test_metric_range_and_strict_schema(self):
        for value in (-.1, 1.1, math.nan, math.inf, True):
            with self.assertRaises(ValueError):
                validate_metrics({"x": value})
        self.data["citation_verdicts"] = ["true"]
        with self.assertRaises(ValueError):
            JudgmentV2(**self.data)

    def test_v1_remote_ids_and_v2_isolation(self):
        old_id, old_name = remote_score_identity("run", {}, "q", "faithfulness")
        self.assertEqual(old_id, str(uuid.uuid5(uuid.NAMESPACE_URL, "run:q:faithfulness")))
        self.assertEqual(old_name, "faithfulness")
        new_id, new_name = remote_score_identity("run", {"metric_version": 2, "rubric": "a"}, "q", "faithfulness")
        another, _ = remote_score_identity("run", {"metric_version": 2, "rubric": "b"}, "q", "faithfulness")
        self.assertNotEqual(old_id, new_id); self.assertNotEqual(new_id, another)
        self.assertEqual(new_name, "benchmark.v2/faithfulness")

    def test_v2_publishing_uses_namespace_and_idempotent_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); evaluation = root / "evaluation"
            write_json(root / "run_manifest.json", {"identity": {"run": 1}})
            write_json(root / "trace_map.json", {"q": "trace"})
            write_json(evaluation / "manifest.json", {"identity": {"metric_version": 2}})
            write_jsonl(evaluation / "scores.jsonl", [{"id": "q", "metrics": {"faithfulness": .5}}])
            client = Mock(); client.api.scores_v3.get_many_v3.return_value.data = []
            client.api.scores.create.side_effect = lambda **kwargs: Mock(id=kwargs["id"])
            self.assertEqual(publish_scores(root, evaluation, client), 1)
            args = client.api.scores.create.call_args.kwargs
            self.assertEqual(args["name"], "benchmark.v2/faithfulness")
            expected, _ = remote_score_identity(digest({"run": 1}), {"metric_version": 2}, "q", "faithfulness")
            self.assertEqual(args["id"], expected)


class EvaluationV2Tests(unittest.TestCase):
    def setUp(self):
        from tests.test_benchmark import BenchmarkTests
        self.fixture = BenchmarkTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.items = [self.fixture.item()]
        self.items[0]["split"] = "dev"
        self.rows = [{"id": self.items[0]["id"], "response": "完成检查。", "error": None,
                      "run_config": {"test": 1}, "retrieved_contexts": [], "citations": []}]
        self.runs = self.root / "runs.jsonl"; write_jsonl(self.runs, self.rows)
        self.data = {"point_verdicts": ["correct"], "point_response_quotes": ["完成检查"], "citation_verdicts": [],
                     "claims": [], "boundary_correct": None, "reason": "test"}

    def evaluate(self, outputs, **kwargs):
        from ragas.metrics.collections import Faithfulness
        from ragas.llms.base import InstructorBaseRagasLLM
        llm = Mock(spec=InstructorBaseRagasLLM)
        llm.agenerate = AsyncMock(side_effect=[Probe(ok=True), *outputs])
        client = Mock(); client.close = AsyncMock()
        faith = AsyncMock(return_value=Mock(value=1.))
        kwargs.setdefault('judge_protocol', 'verbatim')
        with patch("rag.benchmark.evaluation.load_release", return_value=(self.items, {"gold_sha256": "gold"})), \
             patch("rag.benchmark.evaluation.build_llm", return_value=(llm, client)), \
             patch.object(Faithfulness, "ascore", new=faith), redirect_stdout(io.StringIO()):
            report = asyncio.run(evaluate_release(self.fixture.source, self.runs, self.root / "evaluation", **kwargs))
        return report, llm, faith

    def test_invalid_judge_repair_and_resume(self):
        invalid = dict(self.data, point_response_quotes=["虚构原文"])
        repair = JudgmentRepair(point_fixes=[{"index": 0, "verdict": "correct", "response_quote": "完成检查"}],
                                claim_fixes=[], citation_fixes=[], boundary_fix=None, reason="修复")
        report, llm, _ = self.evaluate([JudgmentV2(**invalid), repair])
        self.assertEqual(report["overall"]["statuses"], {"ok": 1})
        self.assertEqual(llm.agenerate.await_count, 3)
        saved = json.loads((self.root / "evaluation" / "items" / f"{self.items[0]['id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved["judge_attempts"]), 2)
        self.assertIn("quote absent", saved["judge_attempts"][0]["error"])
        _, llm, _ = self.evaluate([])
        self.assertEqual(llm.agenerate.await_count, 1)  # Probe only, no new judgment.
        with self.assertRaisesRegex(ValueError, "changed"):
            self.evaluate([], metric_version=1)

    def test_persistent_failure_preserves_independent_scores_and_retry(self):
        report, _, _ = self.evaluate([RuntimeError("invalid schema"), RuntimeError("still invalid")])
        self.assertEqual(report["overall"]["statuses"], {"judge_error": 1})
        self.assertEqual(report["overall"]["metrics"]["faithfulness"]["completion_rate"], 1)
        saved = json.loads((self.root / "evaluation" / "scores.jsonl").read_text(encoding="utf-8"))
        self.assertIn("still invalid", saved["metric_errors"]["judgment"])
        report, _, _ = self.evaluate([JudgmentV2(**self.data)], retry_failed=True)
        self.assertEqual(report["overall"]["statuses"], {"ok": 1})
        self.assertEqual(len(list((self.root / "evaluation" / "attempt_history").rglob("*.json"))), 1)

    def test_faithfulness_reuse_and_mismatch_checks(self):
        source = self.root / "old"
        generation_hash = file_hash(Path(__file__).parents[1] / "rag" / "benchmark" / "generation.py")
        identity = {"gold_sha256": "gold", "runs_sha256": digest(self.rows), "split": "dev", "judge": JUDGE_CONFIG,
                    "dependencies": dependency_versions(), "implementation_sha256": {"generation.py": generation_hash}}
        write_json(source / "manifest.json", {"identity": identity, "status": "complete"})
        write_jsonl(source / "scores.jsonl", [{"id": self.items[0]["id"], "status": "ok", "metrics": {"faithfulness": .75}}])
        report, _, faith = self.evaluate([JudgmentV2(**self.data)], reuse_faithfulness_from=source)
        faith.assert_not_awaited()
        self.assertEqual(report["overall"]["metrics"]["faithfulness"]["mean_scored"], .75)
        self.assertEqual(report["identity"]["faithfulness_reuse"]["reused_count"], 1)
        write_jsonl(source / "scores.jsonl", [{"id": self.items[0]["id"], "status": "judge_error", "metric_errors": {"judgment": "bad"}, "metrics": {"faithfulness": .75}}])
        values, _ = load_faithfulness_cache(source, identity, self.items, {r["id"]: r for r in self.rows})
        self.assertEqual(values[self.items[0]["id"]], .75)
        for field in ("runs_sha256", "judge", "dependencies", "gold_sha256"):
            changed = dict(identity, **{field: "changed"})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                load_faithfulness_cache(source, changed, self.items, {r["id"]: r for r in self.rows})
        write_jsonl(source / "scores.jsonl", [{"id": self.items[0]["id"], "status": "ok", "metrics": {"faithfulness": 1.1}}])
        with self.assertRaisesRegex(ValueError, "successful"):
            load_faithfulness_cache(source, identity, self.items, {r["id"]: r for r in self.rows})

    def test_protocol_comparison_has_explicit_label(self):
        left, right = self.root / "v1", self.root / "v2"
        identity = {"gold_sha256": "gold", "runs_sha256": "runs", "split": "dev", "judge": JUDGE_CONFIG}
        for folder, version, value in ((left, 1, 1.), (right, 2, .5)):
            write_json(folder / "report.json", {"identity": dict(identity, metric_version=version)})
            write_jsonl(folder / "scores.jsonl", [{"id": "q", "status": "ok", "metrics": {"citation_coverage": value}}])
        report = protocol_comparison(left, right, self.root / "comparison.json")
        self.assertEqual(report["comparison_kind"], "scoring_protocol_difference")
        self.assertEqual(report["metrics"]["citation_coverage"]["mean_delta"], -.5)


if __name__ == "__main__":
    unittest.main()
