import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path

from rag_baseline.benchmark.core import (
    Corpus, REVIEW_CHECKS, freeze, freeze_trial, import_review, load_release, read_jsonl,
)
from rag_baseline.benchmark.quality import CONTENT_CHECKS
from rag_baseline.benchmark.evaluation import evaluate_release, ragas_export


@unittest.skipUnless(Path('data/benchmark/trial_v3/candidates.jsonl').exists(), 'Local trial fixture is not distributed')
class FinalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.corpus = Corpus('data/processed/baseline_v1')
        self.rows = read_jsonl('data/benchmark/trial_v3/candidates.jsonl')
        for row in self.rows:
            row.update(review_status='approved', reviewer='test fixture only',
                       review_checks={k:True for k in (*REVIEW_CHECKS,*CONTENT_CHECKS)})
        self.families = json.loads(Path('data/benchmark/trial_v3/families.json').read_text(encoding='utf-8'))
        self.families.update(review_status='approved', reviewer='test fixture only')
        self.manifest = {'corpus':self.corpus.identity()}

    def test_trial_freeze_and_artifact_integrity(self):
        out=self.root/'release'
        freeze_trial(self.rows,self.corpus,self.families,out,self.manifest)
        gold,m=load_release(out)
        self.assertEqual(len(gold),30)
        self.assertEqual({i['split'] for i in gold},{'dev'})
        self.assertEqual(m['dataset_kind'],'reviewed_trial')
        self.assertFalse(m['full_benchmark_quotas_applied'])
        self.assertTrue(all(set(q)=={'id','user_input'} for q in read_jsonl(out/'queries.jsonl')))
        self.assertTrue(all(r['error']=='not_run' and r['response']=='' for r in read_jsonl(out/'run_template.jsonl')))
        imported=import_review(gold,out/'review/review.csv',Corpus(out))
        self.assertTrue(all(i['review_status']=='approved' for i in imported))
        with self.assertRaises(FileExistsError):freeze_trial(self.rows,self.corpus,self.families,out,self.manifest)
        (out/'queries.jsonl').write_text('{}\n',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'artifact'):load_release(out)

    def test_trial_does_not_relax_full_release_or_review_guards(self):
        with self.assertRaisesRegex(ValueError,'need at least 300'):
            freeze(self.rows,self.corpus,self.families,self.root/'full',self.manifest)
        with self.assertRaisesRegex(ValueError,'exactly 30'):
            freeze_trial(self.rows[:-1],self.corpus,self.families,self.root/'small',self.manifest)
        bad=copy.deepcopy(self.rows);bad[0]['review_checks']['source_quality']=False
        with self.assertRaisesRegex(ValueError,'semantic human'):
            freeze_trial(bad,self.corpus,self.families,self.root/'unchecked',self.manifest)
        bad=copy.deepcopy(self.rows);bad[0]['split']='holdout'
        with self.assertRaisesRegex(ValueError,'holdout'):
            freeze_trial(bad,self.corpus,self.families,self.root/'holdout',self.manifest)
        bad=copy.deepcopy(self.rows);bad[0]['review_status']='pending'
        with self.assertRaisesRegex(ValueError,'approval'):
            freeze_trial(bad,self.corpus,self.families,self.root/'pending',self.manifest)

    def test_trial_has_no_evaluable_holdout(self):
        out=self.root/'release'
        freeze_trial(self.rows,self.corpus,self.families,out,self.manifest)
        with self.assertRaisesRegex(ValueError,'no questions'):
            asyncio.run(evaluate_release(out,out/'run_template.jsonl',self.root/'eval','holdout',True))
        with self.assertRaisesRegex(ValueError,'no questions'):
            ragas_export(out,out/'run_template.jsonl',self.root/'ragas','holdout')


if __name__=='__main__':
    unittest.main()
