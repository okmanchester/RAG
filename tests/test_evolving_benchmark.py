import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rag.benchmark.core import digest, file_hash, load_release, write_json, write_jsonl
from rag.benchmark.first_run import run_baseline
from rag.benchmark.evaluation import evaluate_release


class EvolvingBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.release = self.root/'dataset'
        self.rows = [{'id':f'q{i}', 'user_input':f'question {i}', 'split':'dev'} for i in range(150)]
        write_jsonl(self.release/'gold.jsonl', self.rows)
        write_jsonl(self.release/'dev/queries.jsonl', [{'id':r['id'],'user_input':r['user_input']} for r in self.rows])
        for filename in ('documents.jsonl','chunks.jsonl','families.json','dev/run_template.jsonl',
                         'changes.jsonl','review/audit.jsonl','distribution.json','pilot_ids.jsonl'):
            write_json(self.release/filename, [])
        self.refresh()

    def refresh(self):
        hashes = {str(p.relative_to(self.release)).replace('\\','/'):file_hash(p)
                  for p in self.release.rglob('*') if p.is_file() and p.name != 'manifest.json'}
        self.manifest = {'dataset_kind':'gold150_evolving','status':'evolving',
            'gold_sha256':hashes['gold.jsonl'], 'families_sha256':hashes['families.json'],
            'corpus':{f:hashes[f] for f in ('documents.jsonl','chunks.jsonl')},'artifacts_sha256':hashes}
        self.manifest['version_sha256'] = digest({k:self.manifest[k] for k in ('gold_sha256','corpus','artifacts_sha256')})
        write_json(self.release/'manifest.json', self.manifest)

    def test_explicit_opt_in_and_no_change_to_frozen_default(self):
        with self.assertRaisesRegex(ValueError,'allow-evolving'):
            load_release(self.release)
        rows, manifest = load_release(self.release, allow_evolving=True)
        self.assertEqual(len(rows),150)
        self.assertEqual(manifest['status'],'evolving')

    def test_each_required_file_is_hash_checked(self):
        for filename in ('gold.jsonl','documents.jsonl','chunks.jsonl','families.json',
                         'dev/queries.jsonl','dev/run_template.jsonl','review/audit.jsonl','pilot_ids.jsonl'):
            with self.subTest(filename=filename):
                path = self.release/filename
                original = path.read_bytes()
                path.write_bytes(original+b' ')
                with self.assertRaises(ValueError):
                    load_release(self.release,allow_evolving=True)
                path.write_bytes(original)

    def test_rehashed_queries_still_must_match_gold(self):
        queries = [{'id':r['id'],'user_input':r['user_input']} for r in self.rows]
        queries[0]['user_input'] = 'different question'
        write_jsonl(self.release/'dev/queries.jsonl',queries)
        self.refresh()
        with self.assertRaisesRegex(ValueError,'queries differ'):
            load_release(self.release,allow_evolving=True)

    def test_missing_hash_and_invalid_version_are_rejected(self):
        self.manifest['version_sha256'] = 'invalid'
        write_json(self.release/'manifest.json',self.manifest)
        with self.assertRaisesRegex(ValueError,'fingerprint'):
            load_release(self.release,allow_evolving=True)
        self.refresh()
        self.manifest['artifacts_sha256'].pop('pilot_ids.jsonl')
        write_json(self.release/'manifest.json',self.manifest)
        with self.assertRaisesRegex(ValueError,'Missing'):
            load_release(self.release,allow_evolving=True)

    def test_local_resume_and_failed_question_retry(self):
        index = Mock(identity={'index':1})
        index.search.return_value = []
        output = self.root/'run'
        with patch('openai.OpenAI'), patch('rag.benchmark.first_run.validate_run'), \
             patch('rag.benchmark.first_run.answer_question', side_effect=[('answer', {'total_tokens':3}),
                    RuntimeError('failure'), ('recovered',{'total_tokens':4})]) as answer:
            run_baseline(self.release,['q0','q1'],output,index,allow_evolving=True)
            run_baseline(self.release,['q0','q1'],output,index,allow_evolving=True)
            run_baseline(self.release,['q0','q1'],output,index,allow_evolving=True)
        self.assertEqual(answer.call_count,3)
        self.assertEqual(json.loads((output/'items/q0.json').read_text())['usage']['total_tokens'],3)
        self.assertIsNone(json.loads((output/'items/q1.json').read_text())['error'])
        self.assertEqual(json.loads((output/'trace_map.json').read_text()),{})

    def test_revision_invalidates_run_cache(self):
        index = Mock(identity={'index':1})
        index.search.return_value = []
        output = self.root/'run'
        with patch('openai.OpenAI'), patch('rag.benchmark.first_run.validate_run'), \
             patch('rag.benchmark.first_run.answer_question',return_value=('answer',{})) as answer:
            run_baseline(self.release,['q0'],output,index,allow_evolving=True)
            self.rows[0]['user_input'] = 'revised'
            write_jsonl(self.release/'gold.jsonl',self.rows)
            write_jsonl(self.release/'dev/queries.jsonl',[{'id':r['id'],'user_input':r['user_input']} for r in self.rows])
            self.refresh()
            with self.assertRaisesRegex(ValueError,'new output directory'):
                run_baseline(self.release,['q0'],output,index,allow_evolving=True)
        self.assertEqual(answer.call_count,1)

    def test_evaluation_binds_full_dataset_version_and_rejects_old_cache(self):
        self.rows[0].update(schema_version=2, category='policy', difficulty='easy',
            query_style='natural', answer_form=['fact'], evidence_span='single_unit',
            expected_behavior='answer', evidence_groups=[{'id':'g1','alternatives':[[{
                'doc_id':'doc','document_sha256':'sha','block_id':1,
                'block_char_start':0,'block_char_end':4,'text':'rule'}]]}])
        write_jsonl(self.release/'gold.jsonl',self.rows)
        self.refresh()
        selected = self.root/'ids.jsonl'
        runs = self.root/'runs.jsonl'
        write_jsonl(selected,[{'id':'q0'}])
        write_jsonl(runs,[{'id':'q0','response':'','run_config':{'test':True},
                          'error':None,'retrieved_contexts':[],'citations':[]}])
        output = self.root/'evaluation'
        with patch('rag.benchmark.evaluation.Corpus'), \
             patch('rag.benchmark.evaluation.dependency_versions',return_value={'test':1}):
            report = asyncio.run(evaluate_release(self.release,runs,output,
                retrieval_only=True,ids_path=selected,allow_evolving=True))
            self.assertEqual(report['identity']['dataset_version_sha256'],self.manifest['version_sha256'])
            write_json(self.release/'distribution.json',{'revised_metadata':True})
            self.refresh()
            with self.assertRaisesRegex(ValueError,'new output directory'):
                asyncio.run(evaluate_release(self.release,runs,output,
                    retrieval_only=True,ids_path=selected,allow_evolving=True))


if __name__ == '__main__':
    unittest.main()
