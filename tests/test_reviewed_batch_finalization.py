import copy
import json
import tempfile
import unittest
from pathlib import Path

from rag_baseline.benchmark.core import (
    Corpus, digest, file_hash, freeze, freeze_reviewed, freeze_trial,
    import_review, load_release, read_jsonl,
)


RELEASE = Path('data/benchmark/trial30_final_v1')


class ReviewedBatchFinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        gold, self.manifest = load_release(RELEASE)
        self.corpus = Corpus(RELEASE)
        self.rows = copy.deepcopy(gold[:6])
        for row in self.rows:
            row['pilot'] = False  # Original generation may have been a full batch.
        self.families = json.loads((RELEASE/'families.json').read_text(encoding='utf-8'))

    def freeze(self, rows=None, **options):
        out = self.root/'release'
        freeze_reviewed(self.rows if rows is None else rows, self.corpus, self.families,
                        out, self.manifest, '用户授权的测试夹具，不是新增题审签', **options)
        return out

    def test_public_batch_round_trip_and_immutable_artifacts(self):
        before = digest(self.rows)
        source_hash = file_hash(RELEASE/'manifest.json')
        audit = self.root/'audit.json'
        audit.write_text('{}', encoding='utf-8')
        out = self.freeze(review_artifacts=[audit])
        gold, manifest = load_release(out)
        self.assertEqual(len(gold), 6)
        self.assertEqual(manifest['item_count'], 6)
        self.assertEqual(manifest['dataset_kind'], 'reviewed_dev_batch')
        self.assertFalse(manifest['full_benchmark_quotas_applied'])
        self.assertFalse(manifest['no_freeze_performed'])
        self.assertEqual({i['split'] for i in gold}, {'dev'})
        self.assertTrue(all(i['pilot'] for i in gold))
        self.assertEqual(digest(self.rows), before)
        self.assertEqual(file_hash(RELEASE/'manifest.json'), source_hash)
        self.assertEqual(read_jsonl(out/'review_provenance/source_approved.jsonl'), self.rows)
        self.assertTrue(all(set(q) == {'id','user_input'} for q in read_jsonl(out/'queries.jsonl')))
        self.assertTrue(all(i['error']=='not_run' for i in read_jsonl(out/'run_template.jsonl')))
        self.assertEqual(len(import_review(gold, out/'review/review.csv', Corpus(out))), 6)
        with self.assertRaises(FileExistsError):
            self.freeze()
        (out/'review_provenance/audit.json').write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'artifact'):
            load_release(out)

    def test_no_unreviewed_rows_holdout_or_implicit_authorization(self):
        bad = copy.deepcopy(self.rows)
        bad[0]['review_status'] = 'pending'
        with self.assertRaisesRegex(ValueError, 'approval'):
            self.freeze(bad)
        bad = copy.deepcopy(self.rows)
        bad[0]['review_checks']['source_quality'] = False
        with self.assertRaisesRegex(ValueError, 'semantic human'):
            self.freeze(bad)
        bad = copy.deepcopy(self.rows)
        bad[0]['split'] = 'holdout'
        with self.assertRaisesRegex(ValueError, 'holdout'):
            self.freeze(bad)
        with self.assertRaisesRegex(ValueError, 'authorization'):
            freeze_reviewed(self.rows, self.corpus, self.families, self.root/'release', self.manifest, '')
        self.assertFalse((self.root/'release').exists())

    def test_reference_release_rejects_renamed_duplicate_and_tampering(self):
        bad = copy.deepcopy(self.rows)
        for i in bad:
            i['id'] = 'new-' + i['id']
        with self.assertRaisesRegex(ValueError, 'Duplicate reviewed questions'):
            self.freeze(bad, reference_releases=[RELEASE])
        self.assertFalse((self.root/'release').exists())
        corrupt = self.root/'corrupt'
        self.freeze()
        (self.root/'release').rename(corrupt)
        (corrupt/'queries.jsonl').write_text('{}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'artifact'):
            self.freeze(bad, reference_releases=[corrupt])

    def test_formal_and_thirty_item_contracts_are_unchanged(self):
        with self.assertRaisesRegex(ValueError, 'need at least 300'):
            freeze(self.rows, self.corpus, self.families, self.root/'full', self.manifest)
        with self.assertRaisesRegex(ValueError, 'exactly 30'):
            freeze_trial(self.rows, self.corpus, self.families, self.root/'trial', self.manifest)


if __name__ == '__main__':
    unittest.main()
