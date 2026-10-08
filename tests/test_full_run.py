import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag.benchmark.core import write_json, write_jsonl
from rag.benchmark.full_run import summarize_full_run


class FullRunTests(unittest.TestCase):
    def test_full_report_weights_scored_questions_and_requires_full_coverage(self):
        items = [{"id": f"q{n:03d}", "schema_version": 2, "split": "dev" if n < 6 else "holdout",
                  "category": "policy", "answer_form": ["fact"], "evidence_span": "single_unit",
                  "difficulty": "easy", "query_style": "natural",
                  "expected_behavior": "answer" if n < 7 else "abstain"} for n in range(8)]
        with tempfile.TemporaryDirectory() as root:
            output = Path(root)
            for split in ("dev", "holdout"):
                selected = [item for item in items if item["split"] == split]
                folder = output / split
                scores = [{"id": item["id"], "status": "ok", "metrics":
                           {"faithfulness": float(split == "dev")} if item["expected_behavior"] == "answer"
                           else {"boundary_behavior_accuracy": 1.}} for item in selected]
                write_jsonl(folder / "evaluation" / "scores.jsonl", scores)
                write_jsonl(folder / "runs.jsonl", [{"id": item["id"]} for item in selected])
                write_json(folder / "trace_map.json", {item["id"]: f"trace-{item['id']}" for item in selected})
                write_json(folder / "evaluation" / "report.json", {"overall": {"count": len(selected)}, "identity": {"split": split}})
                write_json(folder / "remote_verification.json", {"status": "complete"})
            with patch("rag.benchmark.full_run.load_release", return_value=(items, {"gold_sha256": "gold"})):
                report = summarize_full_run(output / "release", output)
                self.assertEqual(report["overall"]["count"], 8)
                self.assertEqual(report["overall"]["statuses"], {"ok": 8})
                metric = report["overall"]["metrics"]["faithfulness"]
                self.assertEqual(metric["scored_count"], 7)
                self.assertAlmostEqual(metric["mean_scored"], 6 / 7)
                write_jsonl(output / "holdout" / "runs.jsonl", [])
                with self.assertRaisesRegex(ValueError, "per frozen question"):
                    summarize_full_run(output / "release", output)


if __name__ == "__main__":
    unittest.main()
