"""Contracts that protect the isolated FULLTEXT experiment and its conclusions."""
import unittest

from scripts.evaluate_mysql_fulltext import MODES, METRICS, output_path, pairwise, validate_hits


class MySQLFulltextBenchmarkTests(unittest.TestCase):
    def test_output_cannot_enter_tracked_release_or_source(self):
        for path in ("data/benchmark/gold150_hard_v2/new_results", "docs/new_results", "scripts/new_results"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                output_path(path)

    def test_foreign_duplicate_and_mislabeled_candidates_are_rejected(self):
        hit = {"_id": "1", "_score": 1., "_source": {"legacy_chunk_id": "chunk1"}}
        by_id = {"1": {"chunk_id": "chunk1"}}
        validate_hits([hit], by_id)
        bad_cases = ([hit, hit], [{**hit, "_id": "999"}],
                     [{**hit, "_source": {"legacy_chunk_id": "another"}}],
                     [{**hit, "_score": float("nan")}])
        for hits in bad_cases:
            with self.subTest(hits=hits), self.assertRaises(ValueError):
                validate_hits(hits, by_id)

    def test_paired_metrics_exclude_boundary_and_keep_tradeoffs_visible(self):
        items = [{"id": str(n), "user_input": "question", "category": "policy",
                  "expected_behavior": "answer" if n < 2 else "abstain", "evidence_groups": []}
                 for n in range(3)]
        def record(n, values):
            return {"id": str(n), "hits": [], "metrics": dict(zip(METRICS, values)) if n < 2 else {}}
        records = {mode: [record(0, (.5, .5, 0., 0.)), record(1, (1., 1., 1., 1.)), record(2, ())]
                   for mode in MODES}
        records["mysql_default"][0] = record(0, (.25, 1., 0., 1.))
        pair = pairwise(items, records)["mysql_default_vs_es_bm25"]
        self.assertEqual(pair["metrics"]["evidence_recall@10"],
                         {"left_wins": 1, "ties": 1, "right_wins": 0})
        self.assertEqual(pair["metrics"]["evidence_recall@5"],
                         {"left_wins": 0, "ties": 1, "right_wins": 1})
        self.assertEqual(pair["largest_left_advantages"][0]["id"], "0")
        self.assertEqual(pair["largest_left_advantages"][0]["delta_left_minus_right"]["evidence_recall@5"], -.25)


if __name__ == "__main__":
    unittest.main()
