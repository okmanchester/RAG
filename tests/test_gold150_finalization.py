"""Regression checks for the frozen, auditable 150-question release."""
import asyncio
import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from rag_baseline.benchmark.core import (
    Corpus, GOLD150_CORE_FILES, digest, file_hash, import_review, load_release, read_jsonl, write_jsonl,
)
from rag_baseline.benchmark.gold150 import (
    adopt_reviews, freeze_gold150, select_gold150, shared_evidence_groups,
)
from rag_baseline.benchmark.quality import content_fingerprint


RELEASE = Path(os.environ.get('GOLD150_TEST_RELEASE', 'data/benchmark/gold150_final_v1'))


class Gold150FinalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gold, cls.manifest = load_release(RELEASE)
        cls.corpus = Corpus(RELEASE)
        cls.history_available = all((RELEASE/name).is_file() for name in (
            'review_provenance/source_pool150.jsonl', 'review_provenance/source_candidates.jsonl'))
        cls.pool = read_jsonl(RELEASE/'review_provenance/source_pool150.jsonl') if cls.history_available else copy.deepcopy(cls.gold)
        cls.candidates = read_jsonl(RELEASE/'review_provenance/source_candidates.jsonl') if cls.history_available else []
        cls.families = json.loads((RELEASE/'families.json').read_text(encoding='utf-8'))
        cls.approved, cls.adoptions = adopt_reviews(cls.pool, cls.candidates, cls.corpus, '测试冻结授权')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def freeze(self, **options):
        args = dict(source_candidates=self.candidates)
        args.update(options)
        out = self.root/'release'
        test_manifest = {k:self.manifest[k] for k in ('corpus','judge','generator','evidence_policy')
                         if k in self.manifest}
        freeze_gold150(self.pool, self.corpus, self.families, out, test_manifest,
                       '测试冻结授权', **args)
        return out

    def test_approvals_are_bound_and_content_is_preserved(self):
        if not self.history_available:
            self.skipTest('Original approval history is local-only')
        self.assertEqual(len(self.adoptions), 12)
        self.assertEqual(sum(i['review_status'] == 'pending' for i in self.pool), 12)
        original = {r['id']:r for r in self.pool}
        for row in self.approved:
            self.assertEqual(row['review_status'], 'approved')
            self.assertEqual(content_fingerprint(row), content_fingerprint(original[row['id']]))
            self.assertEqual(row['category'], original[row['id']]['category'])
        self.assertTrue(all(not r['approval_provenance']['human_per_item_review_claimed']
                            for r in self.adoptions))
        bad = copy.deepcopy(self.pool)
        pending = next(r for r in bad if r['review_status'] == 'pending')
        pending['agent_review']['source_candidate_hash'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            adopt_reviews(bad, self.candidates, self.corpus, '测试授权')
        pending['agent_review']['source_candidate_hash'] = next(
            r for r in self.pool if r['id'] == pending['id'])['agent_review']['source_candidate_hash']
        pending['reference'] += '未审核的改写'
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            adopt_reviews(bad, self.candidates, self.corpus, '测试授权')
        bad = copy.deepcopy(self.pool)
        next(r for r in bad if r['review_status'] == 'pending')['agent_review']['agent_review_checks']['answer'] = False
        with self.assertRaisesRegex(ValueError, 'checks'):
            adopt_reviews(bad, self.candidates, self.corpus, '测试授权')
        with self.assertRaisesRegex(ValueError, 'authorization'):
            adopt_reviews(self.pool, self.candidates, self.corpus, '')

    def test_partition_is_optimal_order_independent_and_grouped(self):
        before = digest(self.approved)
        forward, selection = select_gold150(self.approved)
        reverse, reversed_selection = select_gold150(list(reversed(self.approved)))
        self.assertEqual(forward, reverse)
        self.assertEqual(selection, reversed_selection)
        self.assertEqual(digest(self.approved), before)
        self.assertEqual(selection['assignment'], self.manifest['selection']['assignment'])
        self.assertEqual(selection['optimal_scaled_deviation'], 26)
        self.assertEqual([len([r for r in forward if r['split']==s]) for s in ('dev','holdout')], [120,30])
        for group in shared_evidence_groups(forward):
            self.assertEqual(len({forward[i]['split'] for i in group}), 1)
        self.assertTrue(all(r['split']=='dev' for r in forward
                            if r.get('difficulty')=='hard' or r['expected_behavior']=='clarify'))
        bad = copy.deepcopy(self.approved)
        for row in bad:
            row['evidence_groups'] = bad[0]['evidence_groups']
        with self.assertRaisesRegex(ValueError, 'Cannot satisfy'):
            select_gold150(bad)
        with self.assertRaisesRegex(ValueError, 'exactly 150'):
            select_gold150(self.approved[:-1])

    def test_freeze_round_trip_immutable_and_tamper_detection(self):
        before = digest(self.pool)
        out = self.freeze()
        gold, manifest = load_release(out)
        self.assertEqual(digest(self.pool), before)
        self.assertEqual(len(gold), 150)
        self.assertEqual(manifest['approval_adoption_count'], len(self.adoptions))
        self.assertEqual(len(import_review(gold, out/'review/review.csv', Corpus(out))), 150)
        for split, count in (('dev',120),('holdout',30)):
            part = read_jsonl(out/split/'gold.jsonl')
            self.assertEqual(len(part), count)
            self.assertEqual(part, [r for r in gold if r['split']==split])
            self.assertTrue(all(set(q)=={'id','user_input'} for q in read_jsonl(out/split/'queries.jsonl')))
            self.assertTrue(all(r['error']=='not_run' for r in read_jsonl(out/split/'run_template.jsonl')))
        with self.assertRaises(FileExistsError):
            self.freeze()
        (out/'holdout/queries.jsonl').write_text('{}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'artifact'):
            load_release(out)

    def test_missing_approval_and_bad_family_fail_before_publishing(self):
        if self.history_available:
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                self.freeze(source_candidates=[])
        self.assertFalse((self.root/'release').exists())
        bad = copy.deepcopy(self.pool)
        next(r for r in bad if r['review_status']=='approved')['reviewer'] = ''
        with self.assertRaisesRegex(ValueError, 'approval'):
            freeze_gold150(bad, self.corpus, self.families, self.root/'release', self.manifest,
                           '测试授权', source_candidates=self.candidates)
        self.assertFalse((self.root/'release').exists())
        families = copy.deepcopy(self.families)
        families['review_status'] = 'pending'
        with self.assertRaisesRegex(ValueError, 'family'):
            freeze_gold150(self.pool, self.corpus, families, self.root/'release', self.manifest,
                           '测试授权', source_candidates=self.candidates)

    def test_history_public_exposure_and_archived_provenance(self):
        if not self.history_available:
            self.skipTest('Complete source history is local-only')
        load_release(RELEASE, verify_history=True)
        original = {r['id']:r for r in self.pool}
        by_id = {r['id']:r for r in self.gold}
        for row in self.gold:
            prior = original[row['id']]
            self.assertEqual(row.get('pilot'), prior.get('pilot'))
            self.assertEqual(row['freeze_provenance']['original_split'], prior.get('split'))
            for field, key in (('approval_provenance','audit_file'), ('review_provenance','audit_file'),
                               ('corpus_search_provenance','current_search_file')):
                value = row.get(field, {}).get(key)
                if value:
                    self.assertTrue((RELEASE/value).is_file(), (row['id'], value))
        public = set()
        for reference in self.manifest['previous_frozen_releases']:
            archive = RELEASE/'review_provenance/sources'/reference['release']
            old = read_jsonl(archive/'gold.jsonl')
            public.update(r['id'] for r in old)
            self.assertEqual(file_hash(archive/'gold.jsonl'), reference['gold_sha256'])
            self.assertEqual(file_hash(archive/'manifest.json'), reference['manifest_sha256'])
            for row in old:
                self.assertEqual(content_fingerprint(row), content_fingerprint(by_id[row['id']]))
        exposed = {r['id'] for r in self.gold if r['split']=='holdout' and r['id'] in public}
        self.assertEqual(exposed, set(self.manifest['holdout_previously_public_ids']))
        self.assertFalse(self.manifest['holdout_is_claimed_unseen'])
        self.assertEqual(read_jsonl(RELEASE/'review_provenance/source_pool150.jsonl'), self.pool)
        index = json.loads((RELEASE/'review_provenance/artifact_index.json').read_text(encoding='utf-8'))
        self.assertTrue(index)
        for artifact in index:
            self.assertEqual(file_hash(RELEASE/artifact['artifact_path']), artifact['sha256'])

    def core_copy(self):
        out = self.root/'core_release'
        for name in GOLD150_CORE_FILES:
            target = out/name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(RELEASE/name, target)
        return out

    def test_core_checkout_verifies_and_evaluates_without_history(self):
        from rag_baseline.benchmark.evaluation import evaluate_release
        out = self.core_copy()
        rows, manifest = load_release(out)
        self.assertEqual(rows, self.gold)
        self.assertEqual(manifest, self.manifest)
        self.assertEqual(sum(p.is_file() for p in out.rglob('*')), 11)
        with self.assertRaisesRegex(ValueError, 'Missing frozen artifact'):
            load_release(out, verify_history=True)
        runs = self.root/'runs.jsonl'
        write_jsonl(runs, [{'id':r['id'], 'response':'core fixture', 'retrieved_contexts':[],
                           'citations':[], 'run_config':{'mode':'core_checkout_fixture'}, 'error':None} for r in rows])
        with patch('rag_baseline.benchmark.evaluation.build_llm', side_effect=AssertionError('No model calls')):
            with redirect_stdout(io.StringIO()):
                for split, count in (('dev',120),('holdout',30)):
                    report = asyncio.run(evaluate_release(out, runs, self.root/split, split, True))
                    self.assertEqual(report['overall']['count'], count)
                    self.assertEqual(report['overall']['statuses'], {'ok':count})
        (out/'holdout/queries.jsonl').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing frozen artifact'):
            load_release(out)

    def test_core_is_strict_and_local_history_is_explicit(self):
        out = self.core_copy()
        (out/'review_provenance').mkdir()
        (out/'review_provenance/source_pool150.jsonl').write_text('changed local history', encoding='utf-8')
        self.assertEqual(len(load_release(out)[0]), 150)
        # Full verification checks every original manifest artifact, including history.
        with self.assertRaisesRegex(ValueError, 'Missing frozen artifact|Frozen artifact was modified'):
            load_release(out, verify_history=True)
        (out/'dev/queries.jsonl').write_text('{}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'artifact'):
            load_release(out)

    def test_approval_adoption_without_historical_files(self):
        row = copy.deepcopy(next(r for r in self.gold if r['expected_behavior']=='answer'))
        checks = copy.deepcopy(row['review_checks'])
        row.pop('agent_review', None)
        row.update(review_status='pending', reviewer='', review_checks={})
        reviewed = copy.deepcopy(row)
        reviewed['agent_review'] = {'id':row['id'], 'source_candidate_hash':digest(row),
                                   'agent_review_status':'approved', 'failed_checks':[],
                                   'reviewer':'synthetic approval fixture', 'reviewed_at':'2026-09-29T00:00:00Z',
                                   'agent_review_checks':checks}
        approved, ledger = adopt_reviews([reviewed], [row], self.corpus, '测试夹具接纳授权')
        self.assertEqual(len(ledger), 1)
        self.assertEqual(content_fingerprint(approved[0]), content_fingerprint(row))
        reviewed['agent_review']['source_candidate_hash'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'fingerprint'):
            adopt_reviews([reviewed], [row], self.corpus, '测试授权')

    def test_offline_both_splits_do_not_call_models(self):
        from rag_baseline.benchmark.evaluation import evaluate_release
        runs = self.root/'runs.jsonl'
        # Controlled smoke input, not a real RAG run or a quality measurement.
        write_jsonl(runs, [{'id':r['id'], 'response':'offline smoke', 'retrieved_contexts':[],
                           'citations':[], 'run_config':{'mode':'offline_fixture'}, 'error':None} for r in self.gold])
        with patch('rag_baseline.benchmark.evaluation.build_llm', side_effect=AssertionError('No model calls')):
            with redirect_stdout(io.StringIO()):
                for split, count in (('dev',120),('holdout',30)):
                    report = asyncio.run(evaluate_release(RELEASE, runs, self.root/split, split, True))
                    self.assertEqual(report['overall']['count'], count)
                    self.assertEqual(report['overall']['statuses'], {'ok':count})


if __name__ == '__main__':
    unittest.main()
