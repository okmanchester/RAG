"""Real historical defects; no local pilot folders, KG, or model calls needed."""
import copy
import json
import unittest
from pathlib import Path

from rag_baseline.benchmark.core import Corpus, digest, validate_item
from rag_baseline.benchmark.quality import block_flags, content_problems, expanded_blocks

ROOT = Path(__file__).resolve().parents[1]


class HistoricalQualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = json.loads((ROOT/'tests/fixtures/benchmark_quality_cases.json').read_text(encoding='utf8'))
        cls.corpus = Corpus(ROOT/'data/benchmark/trial30_final_v1')
        cls.expected_corpus = fixture['corpus']
        cls.cases = fixture['cases']
        cls.unordered_list_case = fixture['unordered_list_case']

    def case(self, suffix):
        return next(c for c in self.cases if c['source_id'] == 'pilot_v2-'+suffix)

    def test_history_integrity_and_corrected_examples(self):
        self.assertEqual(self.corpus.identity(), self.expected_corpus)
        self.assertEqual(len(self.cases), 11)
        for case in self.cases:
            with self.subTest(case=case['source_id']):
                for version in ('before', 'after'):
                    row = case[version]
                    self.assertEqual(digest(row), case[version+'_sha256'])
                    for group in row['evidence_groups']:
                        for alternative in group['alternatives']:
                            for e in alternative:
                                block = self.corpus.blocks[e['doc_id'], e['block_id']]
                                self.assertEqual(block['text'][e['block_char_start']:e['block_char_end']], e['text'])
                self.assertEqual(case['after']['source_id'], case['source_id'])
                validate_item(case['after'], self.corpus)
                self.assertEqual(content_problems(case['after'], self.corpus), [])

    def test_C_E_S_objective_bad_examples(self):
        for case in self.cases:
            if case['mode'] != 'objective':
                continue
            with self.subTest(code=case['code'], case=case['source_id']):
                # Old v1 records keep their original labels; only the content guard
                # is called, so a schema migration error cannot count as detection.
                problems = content_problems(case['before'], self.corpus)
                self.assertTrue(any(case['expected_problem'] in p for p in problems), problems)

    def test_C_full_article_context_includes_missing_measure(self):
        row = self.case('0001')['before']
        context = expanded_blocks(row, self.corpus)
        did = row['evidence_groups'][0]['alternatives'][0][0]['doc_id']
        self.assertTrue({168, 169, 170, 171, 172} <= {b['block_id'] for b in context if b['doc_id'] == did})

    def test_E_table_context_preserves_header(self):
        context = expanded_blocks(self.case('0002')['before'], self.corpus)
        self.assertTrue({625, 628, 629, 630} <= {b['block_id'] for b in context})
        header = next(b for b in context if b['block_id'] == 625)
        self.assertIn('VLAN', header['text'])
        self.assertIn('VxLAN', header['text'])

    def test_C_real_unordered_list_includes_last_item_not_next_section(self):
        case = self.unordered_list_case
        blocks = expanded_blocks(case['task'], self.corpus)
        ids = {b['block_id'] for b in blocks if b['doc_id'] == case['doc_id']}
        self.assertIn(case['expected_block'], ids)
        self.assertNotIn(case['next_heading_block'], ids)
        self.assertIn('设备体积受限', self.corpus.blocks[case['doc_id'], case['expected_block']]['text'])

    def test_E_real_preface_definition_is_eligible(self):
        old = self.cases[-1]['before']
        e = old['evidence_groups'][0]['alternatives'][0][0]
        self.assertEqual(e['block_id'], 8)
        self.assertIn('企业外网和企业内网', e['text'])
        self.assertNotIn('reference_or_navigation', block_flags(self.corpus, e['doc_id'], e['block_id']))
        # Explicitly a derived positive control using the actual historical quote,
        # not a fabricated historical question or a gold replacement.
        row = copy.deepcopy(self.case('0020')['after'])
        row.update(user_input='工业园区网络连接企业内外的哪些网络？',
                   reference='企业外网和企业内网。', answer_points=['企业外网和企业内网。'],
                   answer_form=['fact'], evidence_span='single_unit', point_evidence=[['g1']],
                   evidence_groups=[{'id': 'g1', 'alternatives': [[e]]}],
                   scope_note='只考查前言中的网络连接关系。', review_status='pending', review_checks={})
        validate_item(row, self.corpus)

    def test_L_explicit_subset_is_not_incomplete(self):
        row = self.case('0020')['after']
        self.assertEqual(content_problems(row, self.corpus), [])
        self.assertIn('condition', row['answer_form'])



if __name__ == '__main__':
    unittest.main()
