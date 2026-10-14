"""Default selection: complete paired evidence, dominance, and safe publication."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import Mock

from rag.management.errors import ManagementError
from rag.search.quality import METRICS, default_mode_gate
from scripts.evaluate_managed_retrieval import promote_default


def summaries(dense=.5, bm25=.8, hybrid=.7, numpy=.5):
    def summary(value):
        def group(count):
            return {"metrics": {name: {"eligible_count": count, "scored_count": count, "mean_scored": value}
                                for name in METRICS}}
        return {"overall": group(120), "slices": {"category": {"policy": group(60), "technical": group(60)}}}
    return {mode: summary(value) for mode, value in (("numpy", numpy), ("dense", dense), ("bm25", bm25), ("hybrid", hybrid))}


class DefaultSelectionTests(unittest.TestCase):
    def test_bm25_wins_over_a_hybrid_that_improves_numpy(self):
        for current in ("dense", "hybrid"):
            gate = default_mode_gate(summaries(), current)
            self.assertTrue(gate["passed"])
            self.assertEqual(gate["recommended_default"], "bm25")

    def test_any_mode_can_win_and_current_best_is_retained(self):
        for best in ("dense", "bm25", "hybrid"):
            values = {mode: .6 for mode in ("dense", "bm25", "hybrid")}
            values[best] = .9
            current = "bm25" if best == "dense" else "dense"
            self.assertEqual(default_mode_gate(summaries(**values), current)["recommended_default"], best)
            gate = default_mode_gate(summaries(**values), best)
            self.assertFalse(gate["passed"])
            self.assertEqual(gate["recommended_default"], best)

    def test_overall_tradeoffs_do_not_have_an_arbitrary_scalar_winner(self):
        reports = summaries()
        reports["hybrid"]["overall"]["metrics"]["all_evidence@10"]["mean_scored"] = .9
        gate = default_mode_gate(reports, "hybrid")
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["recommended_default"], "hybrid")
        self.assertEqual(gate["best_candidates"], [])

    def test_category_regression_prevents_overall_winner_promotion(self):
        reports = summaries()
        reports["bm25"]["slices"]["category"]["technical"]["metrics"]["evidence_recall@5"]["mean_scored"] = .4
        gate = default_mode_gate(reports)
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["recommended_default"], "dense")

    def test_missing_invalid_or_incomplete_candidate_keeps_current_default(self):
        for invalid in (None, float("nan"), float("inf"), -1, 2, True):
            reports = summaries()
            reports["dense"]["overall"]["metrics"]["all_evidence@5"]["mean_scored"] = invalid
            gate = default_mode_gate(reports, "hybrid")
            self.assertFalse(gate["passed"])
            self.assertEqual(gate["recommended_default"], "hybrid")
        for mutate in (lambda r: r.pop("bm25"),
                       lambda r: r["hybrid"]["overall"]["metrics"]["all_evidence@10"].update(scored_count=119),
                       lambda r: r["dense"]["slices"]["category"].pop("technical")):
            reports = summaries()
            mutate(reports)
            self.assertFalse(default_mode_gate(reports)["passed"])

    def test_inconsistent_denominators_prevent_selection(self):
        reports = summaries()
        reports["bm25"]["overall"]["metrics"]["all_evidence@5"].update(eligible_count=119, scored_count=119)
        self.assertFalse(default_mode_gate(reports)["passed"])
        reports = summaries()
        for group, count in ((reports["bm25"]["overall"], 118),
                             (reports["bm25"]["slices"]["category"]["policy"], 59),
                             (reports["bm25"]["slices"]["category"]["technical"], 59)):
            for metric in group["metrics"].values():
                metric.update(eligible_count=count, scored_count=count)
        self.assertFalse(default_mode_gate(reports)["passed"])

    def test_ties_keep_current_when_best_otherwise_prefer_no_embedding(self):
        reports = summaries(bm25=.8, hybrid=.8)
        self.assertEqual(default_mode_gate(reports)["recommended_default"], "bm25")
        for current in ("bm25", "hybrid"):
            self.assertFalse(default_mode_gate(reports, current)["passed"])
            self.assertEqual(default_mode_gate(reports, current)["recommended_default"], current)

    def test_category_only_gain_is_not_strict_overall_improvement(self):
        reports = summaries(dense=.8, bm25=.8, hybrid=.8)
        for group in reports["bm25"]["slices"]["category"].values():
            for metric in group["metrics"].values():
                metric["mean_scored"] = .9
        gate = default_mode_gate(reports)
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["recommended_default"], "dense")

    def test_numpy_safety_baseline_is_retained(self):
        self.assertFalse(default_mode_gate(summaries(numpy=.95))["passed"])


class DefaultPublicationTests(unittest.TestCase):
    def setUp(self):
        self.managed = Mock()
        self.managed.corpus.index_lock.return_value = nullcontext()
        self.manifest = {"default_mode": "hybrid"}
        self.identity = {"kb_id": 1, "generation": "a" * 32, "selection_sha256": "selection",
                         "index_manifest": copy.deepcopy(self.manifest)}
        self.current = {"generation": "a" * 32, "selection_sha256": "selection", "sync_lag": False,
                        "index": "physical-index", "manifest": self.manifest}
        self.managed.status.return_value = self.current
        self.report = {"identity": self.identity, "reports": summaries(), "gate": default_mode_gate(summaries(), "hybrid")}
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "report.json"
        self.path.write_text(json.dumps(self.report), encoding="utf-8")

    def test_publishes_recommended_bm25_and_evaluation_identity(self):
        self.assertTrue(promote_default(self.managed, self.report, self.path))
        metadata = self.managed.store.seal.call_args.args[1]
        self.assertEqual(metadata["default_mode"], "bm25")
        self.assertEqual(metadata["default_mode_selection_policy"], 2)
        self.assertEqual(len(metadata["default_mode_evaluation_sha256"]), 64)

    def test_default_change_in_same_generation_invalidates_stale_decision(self):
        self.manifest["default_mode"] = "dense"
        with self.assertRaises(ManagementError):
            promote_default(self.managed, self.report, self.path)
        self.managed.store.seal.assert_not_called()

    def test_generation_change_and_lag_prevent_publication(self):
        for change in ({"generation": "b" * 32}, {"sync_lag": True}):
            with self.subTest(change=change):
                self.managed.status.return_value = {**self.current, **change}
                with self.assertRaises(ManagementError):
                    promote_default(self.managed, self.report, self.path)
        self.managed.store.seal.assert_not_called()

    def test_forged_recommendation_does_not_override_recomputed_gate(self):
        self.report["gate"]["recommended_default"] = "dense"
        self.path.write_text(json.dumps(self.report), encoding="utf-8")
        with self.assertRaises(ManagementError):
            promote_default(self.managed, self.report, self.path)
        self.managed.store.seal.assert_not_called()

    def test_attached_report_must_match_the_evidence_used_for_selection(self):
        self.path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ManagementError):
            promote_default(self.managed, self.report, self.path)
        self.managed.store.seal.assert_not_called()

    def test_same_generation_with_changed_content_cannot_use_old_evidence(self):
        self.manifest["content_sha256"] = "different"
        with self.assertRaises(ManagementError):
            promote_default(self.managed, self.report, self.path)
        self.managed.store.seal.assert_not_called()
