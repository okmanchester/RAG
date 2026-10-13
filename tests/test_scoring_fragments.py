import asyncio
import copy
import unittest
from unittest.mock import AsyncMock, Mock

from rag.benchmark.scoring_v2 import (JudgmentV2, extract_citations_v2, grounding_issues,
                                     score_judgment_v2, score_judgment_v2_parts)
from rag.benchmark.scoring_fragments import (SelectedJudgment, build_fragment_catalog,
    resolve_selected_judgment, judge_fragments)


class FragmentScoringTests(unittest.TestCase):
    def setUp(self):
        self.item = {'answer_points': ['审计不可关闭'], 'expected_behavior': 'answer',
                     'user_input': '审计如何保护？', 'reference': '不可关闭', 'boundary_reason': ''}
        self.row = {'response': '审计不可关闭。[1] 日志应备份[2]。',
                    'retrieved_contexts': [{'id': 'a', 'text': '**规定**\n审计不可关 闭。'},
                                           {'id': 'b', 'text': '日志应备份。'}]}
        self.selected = {'points': [{'verdict': 'correct', 'response_fragment_id': 'R0'}],
            'claims': [{'claim': '审计不可关闭', 'response_fragment_id': 'R0', 'citation_supported': True,
                        'support_fragment_ids': ['C1.ALL']},
                       {'claim': '日志应备份', 'response_fragment_id': 'R1', 'citation_supported': True,
                        'support_fragment_ids': ['C2.ALL']}],
            'boundary_correct': None, 'boundary_response_fragment_ids': [], 'reason': '核对原文'}

    def resolve(self, selected=None):
        return resolve_selected_judgment(self.item, self.row,
            SelectedJudgment(**(selected or self.selected)), build_fragment_catalog(self.row))

    def test_post_punctuation_and_quoted_sentence_references(self):
        for response in ['审计不可关闭。[1]', '审计不可关闭。”[1]', '审计不可关闭[1]。']:
            with self.subTest(response=response):
                citations = extract_citations_v2(response, self.row['retrieved_contexts'])
                self.assertIn('审计不可关闭', citations[0]['claim'])
        citations = extract_citations_v2('甲。[1] 乙。[2]', self.row['retrieved_contexts'])
        self.assertNotIn('甲', citations[1]['claim'])
        self.assertIn('乙', citations[1]['claim'])
        self.assertEqual(extract_citations_v2('甲。\n\n[1]', self.row['retrieved_contexts'])[0]['claim'], '')
        self.assertEqual(extract_citations_v2('First. Second.[1]', self.row['retrieved_contexts'])[0]['claim'], 'Second.')
        self.assertIn('9.5%', extract_citations_v2('指标为9.5%[1]', self.row['retrieved_contexts'])[0]['claim'])

    def test_fragments_copy_layout_and_preserve_source_offsets(self):
        catalog = build_fragment_catalog(self.row)
        for fragment in catalog['fragments'].values():
            source = catalog['sources'][fragment['source']]['canonical_text']
            self.assertEqual(source[fragment['start']:fragment['end']], fragment['text'])
        judgment, issues, binding = self.resolve()
        self.assertFalse(issues)
        self.assertEqual(judgment.claims[0].supports[0].support_quote, '**规定**\n审计不可关 闭。')
        self.assertEqual(score_judgment_v2(self.item, self.row, judgment)['citation_support'], 1)
        self.assertTrue(binding['catalog_sha256'])

    def test_context_cannot_be_used_as_response_evidence(self):
        selected = copy.deepcopy(self.selected)
        selected['points'][0]['response_fragment_id'] = 'C1.ALL'
        judgment, extra, _ = self.resolve(selected)
        metrics, errors, _ = score_judgment_v2_parts(self.item, self.row, judgment, extra)
        self.assertNotIn('answer_point_completeness', metrics)
        self.assertIn('answer_point_completeness', errors)
        self.assertEqual(metrics['citation_support'], 1)

    def test_unreferenced_material_does_not_support_claim(self):
        selected = copy.deepcopy(self.selected)
        selected['claims'][0]['support_fragment_ids'] = ['C2.ALL']
        judgment, extra, _ = self.resolve(selected)
        metrics, errors, _ = score_judgment_v2_parts(self.item, self.row, judgment, extra)
        self.assertEqual(metrics['answer_point_completeness'], 1)
        self.assertNotIn('citation_support', metrics)
        self.assertIn('citation_support', errors)
        self.assertIn('actual citation numbers=[1]', errors['citation_support'])
        self.assertIn('C1.ALL', errors['citation_support'])

    def test_repair_candidates_can_find_same_quote_in_actually_cited_material(self):
        self.row['retrieved_contexts'][1]['text']=self.row['retrieved_contexts'][0]['text']
        selected=copy.deepcopy(self.selected)
        selected['claims'][0]['support_fragment_ids']=['C2.ALL']
        judgment,extra,_=self.resolve(selected)
        self.assertTrue(any('allowed candidates' in i['error'] and 'C1.ALL' in i['error'] for i in extra))
        self.assertTrue(score_judgment_v2_parts(self.item,self.row,judgment,extra)[1])

    def test_distinct_atomic_facts_may_share_response_quote(self):
        self.row['response'] = '审计不可关闭且日志应备份。[1]'
        self.row['retrieved_contexts'][0]['text'] = '审计不可关闭且日志应备份。'
        selected = copy.deepcopy(self.selected)
        selected['claims'][1].update(response_fragment_id='R0', support_fragment_ids=['C1.ALL'])
        judgment, _, _ = self.resolve(selected)
        self.assertFalse(grounding_issues(self.item, self.row, judgment))
        judgment.claims.append(judgment.claims[0].model_copy(deep=True))
        self.assertTrue(any('duplicate atomic' in i['error'] for i in grounding_issues(self.item, self.row, judgment)))

    def test_noncontiguous_evidence_is_two_real_fragments(self):
        self.row['response'] = '不可超范围，不可转委托。[1]'
        self.row['retrieved_contexts'][0]['text'] = '不得超出目的；合同终止须删除。不得转委托。'
        catalog = build_fragment_catalog(self.row)
        ids = [f['id'] for f in catalog['fragments'].values()
               if f['id'].startswith('C1.') and f['text'] in ['不得超出目的；', '不得转委托。']]
        self.assertEqual(len(ids), 2)
        selected = copy.deepcopy(self.selected); selected['claims'] = [selected['claims'][0]]
        selected['claims'][0].update(claim='不得超范围及转委托', support_fragment_ids=ids)
        judgment, extra, _ = self.resolve(selected)
        self.assertFalse(extra)
        self.assertEqual(len(judgment.claims[0].supports), 2)
        self.assertEqual(score_judgment_v2(self.item, self.row, judgment)['citation_support'], 1)

    def test_unknown_fragment_only_invalidates_its_group(self):
        selected = copy.deepcopy(self.selected)
        selected['claims'][0]['support_fragment_ids'] = ['C99.fake']
        judgment, extra, _ = self.resolve(selected)
        metrics, errors, _ = score_judgment_v2_parts(self.item, self.row, judgment, extra)
        self.assertEqual(metrics['answer_point_accuracy'], 1)
        self.assertIn('citation_coverage', errors)

    def test_missing_requires_no_fragment_and_bad_reference_is_retained(self):
        selected = copy.deepcopy(self.selected)
        selected['points'][0]['verdict'] = 'missing'
        judgment, extra, _ = self.resolve(selected)
        metrics, errors, _ = score_judgment_v2_parts(self.item, self.row, judgment, extra)
        self.assertIn('answer_point_accuracy', errors)
        selected['points'][0]['response_fragment_id'] = None
        judgment, extra, _ = self.resolve(selected)
        self.assertEqual(score_judgment_v2_parts(self.item, self.row, judgment, extra)[0]['answer_point_missing_rate'], 1)
        self.row['response'] = '审计不可关闭。[999]'
        self.assertEqual(build_fragment_catalog(self.row)['citations'][0]['invalid_numbers'], [999])

    def test_boundary_has_independent_anchored_score(self):
        self.item['expected_behavior'] = 'clarify'
        self.row['response'] = '请明确是否属于委托处理。'
        selected = copy.deepcopy(self.selected)
        selected.update(boundary_correct=True, boundary_response_fragment_ids=['R.ALL'])
        judgment, extra, _ = self.resolve(selected)
        metrics, errors, issues = score_judgment_v2_parts(self.item, self.row, judgment, extra)
        self.assertEqual(metrics, {'boundary_behavior_accuracy': 1})
        self.assertFalse(errors)
        self.assertTrue(issues)  # Irrelevant malformed citation selections remain auditable.
        selected['boundary_response_fragment_ids'] = ['C1.ALL']
        judgment, extra, _ = self.resolve(selected)
        self.assertIn('boundary_behavior_accuracy', score_judgment_v2_parts(self.item, self.row, judgment, extra)[1])

    def test_point_cardinality_does_not_discard_citations(self):
        selected = copy.deepcopy(self.selected); selected['points'] = []
        judgment, extra, _ = self.resolve(selected)
        metrics, errors, _ = score_judgment_v2_parts(self.item, self.row, judgment, extra)
        self.assertIn('answer_point_accuracy', errors)
        self.assertEqual(metrics['citation_support'], 1)

    def test_missing_citation_assessment_is_not_a_baseline_error(self):
        selected=copy.deepcopy(self.selected);selected['claims']=selected['claims'][:1]
        judgment,extra,_=self.resolve(selected)
        metrics,errors,_=score_judgment_v2_parts(self.item,self.row,judgment,extra)
        self.assertEqual(metrics['answer_point_completeness'],1)
        self.assertNotIn('citation_support',metrics)
        self.assertIn('has not been assessed',errors['citation_support'])

    def test_repeated_fact_at_different_references_requires_both_assessments(self):
        self.row['response']='审计不可关闭[1]。审计不可关闭[1]。'
        selected=copy.deepcopy(self.selected);selected['claims']=selected['claims'][:1]
        judgment,extra,_=self.resolve(selected)
        self.assertTrue(any('citations[1]'==i['path'] for i in extra))
        repeated=copy.deepcopy(selected['claims'][0]);repeated['response_fragment_id']='R1'
        selected['claims'].append(repeated)
        judgment,extra,_=self.resolve(selected)
        self.assertFalse(extra)
        self.assertEqual(score_judgment_v2(self.item,self.row,judgment)['citation_support'],1)


class FragmentJudgeAsyncTests(unittest.TestCase):
    setUp = FragmentScoringTests.setUp
    def test_repair_preserves_valid_points_and_updates_only_bad_citations(self):
        bad = copy.deepcopy(self.selected)
        bad['claims'][0]['support_fragment_ids'] = ['C2.ALL']
        fixed = copy.deepcopy(self.selected)
        fixed['points'][0] = {'verdict': 'missing', 'response_fragment_id': None}
        llm = Mock(); llm.agenerate = AsyncMock(side_effect=[SelectedJudgment(**bad), SelectedJudgment(**fixed)])
        result = asyncio.run(judge_fragments(llm, self.item, self.row))
        self.assertEqual(result['metrics']['answer_point_completeness'], 1)
        self.assertEqual(result['metrics']['citation_support'], 1)
        self.assertIn('points', result['judge_attempts'][1]['preserved_groups'])

    def test_api_failure_does_not_discard_first_attempt_valid_metrics(self):
        bad = copy.deepcopy(self.selected)
        bad['claims'][0]['support_fragment_ids'] = ['C2.ALL']
        llm = Mock(); llm.agenerate = AsyncMock(side_effect=[SelectedJudgment(**bad), RuntimeError('timeout')])
        result = asyncio.run(judge_fragments(llm, self.item, self.row))
        self.assertEqual(result['metrics']['answer_point_completeness'], 1)
        self.assertIn('citation_support', result['metric_errors'])

    def test_resume_reuses_valid_judgment_without_calling_model(self):
        llm = Mock(); llm.agenerate = AsyncMock(return_value=SelectedJudgment(**self.selected))
        saved = asyncio.run(judge_fragments(llm, self.item, self.row))
        llm.agenerate.reset_mock()
        resumed = asyncio.run(judge_fragments(llm, self.item, self.row, saved))
        llm.agenerate.assert_not_awaited()
        self.assertTrue(resumed['judgment_reused'])
        self.assertEqual(resumed['metrics'], saved['metrics'])


class FragmentEvaluationTests(unittest.TestCase):
    from tests.test_scoring_v2 import EvaluationV2Tests as _Fixture
    setUp = _Fixture.setUp
    evaluate = _Fixture.evaluate

    def selection(self):
        return SelectedJudgment(points=[{'verdict': 'correct', 'response_fragment_id': 'R.ALL'}],
            claims=[], boundary_correct=None, boundary_response_fragment_ids=[], reason='实际回答完整')

    def test_default_fragment_protocol_records_provenance_and_guards_cache(self):
        import json
        report, llm, _ = self.evaluate([self.selection()], judge_protocol='fragments')
        self.assertEqual(report['overall']['statuses'], {'ok': 1})
        self.assertEqual(report['identity']['judge_protocol_revision'], 'source_fragments_v1')
        record = json.loads((self.root/'evaluation/items'/f"{self.items[0]['id']}.json").read_text(encoding='utf-8'))
        self.assertTrue(record['fragment_bindings']['catalog_sha256'])
        self.assertTrue((self.root/'evaluation/fragments'/f"{self.items[0]['id']}.json").exists())
        _, llm, _ = self.evaluate([], judge_protocol='fragments')
        self.assertEqual(llm.agenerate.await_count, 1)
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.evaluate([], judge_protocol='verbatim')

    def test_failed_quote_group_retains_independent_point_score(self):
        data=self.selection().model_dump()
        data['claims']=[{'claim':'虚构片段', 'response_fragment_id':'R.unknown',
                        'citation_supported':False, 'support_fragment_ids':[]}]
        bad=SelectedJudgment.model_validate(data)
        report, _, _ = self.evaluate([bad,bad], judge_protocol='fragments')
        metrics=report['overall']['metrics']
        self.assertEqual(metrics['answer_point_completeness']['completion_rate'], 1)
        self.assertEqual(metrics['citation_support']['completion_rate'], 0)
        self.assertEqual(report['overall']['statuses'], {'judge_error':1})

    def test_explicit_model_selection_reuse_recomputes_metrics(self):
        from rag.benchmark.core import read_jsonl,write_jsonl
        self.evaluate([self.selection()],judge_protocol='fragments')
        source=self.root/'evaluation'
        rows=read_jsonl(source/'scores.jsonl')
        rows[0]['metrics']['answer_point_completeness']=.123
        write_jsonl(source/'scores.jsonl',rows)
        self.root=self.root/'fresh';self.root.mkdir()
        report,llm,_=self.evaluate([],judge_protocol='fragments',reuse_judgments_from=source)
        self.assertEqual(report['overall']['metrics']['answer_point_completeness']['mean_scored'],1)
        self.assertEqual(llm.agenerate.await_count,1)
        self.assertEqual(report['identity']['model_judgment_reuse']['valid_replayed_judgments'],1)

    def test_model_selection_reuse_rejects_mismatched_provenance(self):
        from rag.benchmark.core import read_jsonl,write_jsonl
        self.evaluate([self.selection()],judge_protocol='fragments')
        source=self.root/'evaluation'
        rows=read_jsonl(source/'scores.jsonl');rows[0]['fragment_bindings']['catalog_sha256']='tampered'
        write_jsonl(source/'scores.jsonl',rows)
        self.root=self.root/'fresh';self.root.mkdir()
        with self.assertRaisesRegex(ValueError,'provenance mismatch'):
            self.evaluate([],judge_protocol='fragments',reuse_judgments_from=source)

    def test_complete_replay_needs_no_model_client_or_credentials(self):
        self.evaluate([self.selection()],judge_protocol='fragments')
        source=self.root/'evaluation';self.root=self.root/'fresh';self.root.mkdir()
        report,llm,faith=self.evaluate([],judge_protocol='fragments',reuse_judgments_from=source,
                                       reuse_faithfulness_from=source)
        llm.agenerate.assert_not_awaited()
        faith.assert_not_awaited()
        self.assertEqual(report['overall']['statuses'],{'ok':1})
