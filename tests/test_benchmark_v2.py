"""Regression tests for independent labels, source scope and review boundaries."""
import asyncio
import copy
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

os.environ["RAGAS_DO_NOT_TRACK"] = "true"

from rag_baseline.benchmark.core import (Corpus, REVIEW_CHECKS, export_review, import_review,
    read_jsonl, select_release, validate_item, validate_quotas, write_jsonl, digest, schema_version)
from rag_baseline.benchmark.quality import (CONTENT_CHECKS, block_flags, expanded_blocks,
    content_problems, named_document, evidence_audit, content_fingerprint)
from rag_baseline.benchmark.generation import AnnotatedV2, SemanticCheck, generate_one
from rag_baseline.benchmark.evaluation import summarize_scores


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        texts = ["第六十三条 部门可以采取下列措施：", "（一）询问当事人。", "（二）查阅记录。",
                 "（三）实施现场检查。", "（四）经负责人批准可以查封设备。", "第六十四条 其他规定。",
                 "前 言", "园区网络是企业内网和外网之间的纽带。", "参 考 文 献", "[1] GB/T 123 某评价方法",
                 "正文", "A=\U00100c10 a/b/c", "|项目|VLAN|VxLAN|", "|位宽|12|24|",
                 "应用场景9：智能路灯照明", "可远程控制路灯。"]
        blocks = [{"block_id": i, "text": t, "role": "heading" if i in (6,8,10,14) else "body", "source": {"page":i+1}}
                  for i,t in enumerate(texts)]
        docs = [{"doc_id":"d", "source_path":"technical/工业互联网体系架构.pdf", "sha256":"raw-hash",
                 "category":"technical", "blocks":blocks}]
        chunks = [{"chunk_id":str(b["block_id"]),"doc_id":"d", "source_path":docs[0]["source_path"],
                   "title":"工业互联网体系架构", "text":b["text"], "body":b["text"],
                   "retrieval_enabled":True, "sources":[{"block_id":b["block_id"]}],
                   "heading_path":["错误父标题", "参 考 文 献"],"category":"technical"} for b in blocks]
        write_jsonl(self.root/"documents.jsonl",docs)
        write_jsonl(self.root/"chunks.jsonl",chunks)
        self.corpus = Corpus(self.root)

    def item(self):
        e = self.corpus.evidence("d",7,self.corpus.blocks["d",7]["text"])
        return {"schema_version":2,"id":"v2-test","category":"technical", "user_input":"园区网络连接什么网络？",
                "reference":"连接企业内网和外网。","answer_points":["连接企业内网和外网。"],
                "answer_form":["fact"],"evidence_span":"single_unit","expected_behavior":"answer",
                "difficulty":"easy","difficulty_reason":"直接提取定义。","scope_note":"园区网络的连接作用。",
                "multi_source_reason":"","query_style":"natural","point_evidence":[["g1"]],
                "evidence_groups":[{"id":"g1","alternatives":[[e]]}],"boundary_reason":"","clarifying_interpretations":[],
                "review_status":"pending","reviewer":"","review_notes":"","review_checks":{},"duplicate_group":"","pilot":True}

    def test_preface_definition_and_spaced_references(self):
        self.assertEqual(block_flags(self.corpus,"d",7),[])
        validate_item(self.item(),self.corpus)
        row=self.item()
        row['evidence_groups'][0]['alternatives']=[[self.corpus.evidence('d',9,self.corpus.blocks['d',9]['text'])]]
        with self.assertRaisesRegex(ValueError,'参考文献'):
            validate_item(row,self.corpus)

    def test_formula_and_title_only_not_function_evidence(self):
        row=self.item()
        for bid, pattern in ((11,'损坏公式'),(14,'标题证据')):
            row['evidence_groups'][0]['alternatives']=[[self.corpus.evidence('d',bid,self.corpus.blocks['d',bid]['text'])]]
            with self.assertRaisesRegex(ValueError,pattern):
                validate_item(row,self.corpus)
        row['user_input']='有哪些具体应用场景？'
        row['reference']=row['answer_points'][0]='智能路灯照明。'
        validate_item(row,self.corpus)

    def test_complete_article_and_cross_page_table_expansion(self):
        got=expanded_blocks({'chunk_ids':['1']},self.corpus)
        self.assertTrue(set(range(5)) <= {b['block_id'] for b in got})
        self.assertNotIn(7,{b['block_id'] for b in got})
        table=expanded_blocks({'chunk_ids':['13']},self.corpus)
        self.assertIn(12,{b['block_id'] for b in table})
        self.assertIn('table_layout_check',block_flags(self.corpus,'d',13))
        row=self.item(); row['user_input']='执法检查时可以采取哪些措施？'
        row['evidence_groups'][0]['alternatives']=[[self.corpus.evidence('d',bid,self.corpus.blocks['d',bid]['text']) for bid in range(4)]]
        with self.assertRaisesRegex(ValueError,'枚举措施证据不完整'):
            validate_item(row,self.corpus)

    def test_document_hints_not_tied_to_intro_word(self):
        for q in ('在工业互联网体系架构中，平台提供什么？','工业互联网体系架构中有哪些层？','根据《某指南》有什么要求？'):
            self.assertTrue(named_document(q,self.corpus))
        self.assertFalse(named_document('根据相关法律，企业有什么义务？',self.corpus))
        row=self.item();row['query_style']='document'
        with self.assertRaisesRegex(ValueError,'query_style'):
            validate_item(row,self.corpus)

    def test_csv_rechecks_content_and_semantic_confirmations(self):
        row=self.item(); row['semantic_precheck']={'content_sha256':content_fingerprint(row),'supported':True}
        export_review([row],self.root/'review',self.corpus)
        path=self.root/'review/review.csv'
        with path.open(encoding='utf-8-sig',newline='') as f: rows=list(csv.DictReader(f))
        self.assertEqual(rows[0]['machine_notes'],'')
        def save():
            with path.open('w',encoding='utf-8-sig',newline='') as f:
                writer=csv.DictWriter(f,rows[0].keys());writer.writeheader();writer.writerows(rows)
        rows[0]['user_input']='在工业互联网体系架构中，园区网络连接什么？';save()
        with self.assertRaisesRegex(ValueError,'query_style'):import_review([row],path,self.corpus)
        rows[0]['user_input']='园区网络起到什么连接作用？';save()
        changed=import_review([row],path,self.corpus)[0]
        self.assertNotIn('semantic_precheck',changed)
        rows[0]['review_status']='approved';rows[0]['reviewer']='human'
        rows[0]['review_checks']=json.dumps({k:True for k in REVIEW_CHECKS});save()
        with self.assertRaisesRegex(ValueError,'semantic human'):import_review([row],path,self.corpus)
        rows[0]['review_checks']=json.dumps({k:True for k in (*REVIEW_CHECKS,*CONTENT_CHECKS)});save()
        self.assertEqual(import_review([row],path,self.corpus)[0]['review_status'],'approved')

    def test_label_independence_and_evaluation_slices(self):
        row=self.item(); row['answer_form']=['fact','condition']
        report=summarize_scores([row],[{'id':row['id'],'status':'ok','metrics':{}}],True)
        self.assertEqual(set(report['slices']['answer_form']),{'fact','condition'})
        self.assertNotIn('question_type',report['slices'])
        row['expected_behavior']='abstain';row['difficulty']=None;row['boundary_reason']='未提供'
        row['evidence_span']='none';row['evidence_groups']=[];row['point_evidence']=[[]]
        validate_item(row,self.corpus)
        row['difficulty']='boundary'
        with self.assertRaisesRegex(ValueError,'difficulty'):validate_item(row,self.corpus)
        with self.assertRaisesRegex(ValueError,'Mixed'):schema_version([self.item(),{'schema_version':1}])

    def test_semantic_gate_rejects_unsupported_or_incoherent_annotation(self):
        task={'schema_version':2,'id':'v2-gen','category':'technical','question_type':'fact','difficulty':'hard',
              'expected_behavior':'answer','query_style':'natural','chunk_ids':['7'],'node_ids':[],
              'synthesizer':'single_hop','pilot':True,'generation_targets':{'difficulty':'hard'}}
        annotation=AnnotatedV2(user_input='园区网络连接什么网络？',reference='企业内网和外网。',answer_points=['企业内网和外网。'],
            evidence_groups=[{'quotes':[{'doc_id':'d','block_id':7,'text':self.corpus.blocks['d',7]['text']}]}],
            answer_form=['fact'],evidence_span='single_unit',difficulty='easy',difficulty_reason='直接定义',
            scope_note='连接作用',point_evidence=[['g1']])
        for field in ('supported','complete_for_question','scope_preserved','coherent_task'):
            folder=self.root/field
            (folder/'drafts').mkdir(parents=True)
            (folder/'drafts/v2-gen.json').write_text('{}',encoding='utf-8')
            result={k:True for k in ('supported','complete_for_question','scope_preserved','coherent_task')}
            result[field]=False
            llm=Mock();llm.agenerate=AsyncMock(side_effect=[annotation,SemanticCheck(**result,reason='范围、漏项或无关拼接问题')])
            with self.assertRaisesRegex(ValueError,'Semantic precheck'):
                asyncio.run(generate_one(task,self.corpus,{},llm,folder,None))
            self.assertTrue((folder/'semantic_checks/v2-gen.json').exists())
        folder=self.root/'accepted_candidate'
        (folder/'drafts').mkdir(parents=True)
        (folder/'drafts/v2-gen.json').write_text('{}',encoding='utf-8')
        llm=Mock();llm.agenerate=AsyncMock(side_effect=[annotation,SemanticCheck(
            supported=True,complete_for_question=True,scope_preserved=True,coherent_task=True,reason='有完整支持')])
        item=asyncio.run(generate_one(task,self.corpus,{},llm,folder,None))
        self.assertEqual(item['generation_targets']['difficulty'],'hard')
        self.assertEqual(item['difficulty'],'easy')
        self.assertNotIn('question_type',item)
        self.assertEqual(item['review_status'],'pending')
        self.assertFalse(item['review_checks'])

    def test_v2_selection_has_caps_not_legacy_difficulty_quotas(self):
        rows=[]
        for n in range(300):
            row=self.item();row.update(id=f'q-{n}',pilot=n==0,review_status='approved',category='policy' if n%2 else 'technical',
                                       user_input=digest(['q',n]),answer_points=[digest(['a',n])])
            row['evidence_groups'][0]['alternatives'][0][0]['block_id']=100+n
            if n<30:
                row['expected_behavior']=['abstain','correct_premise','clarify'][n%3]
                row['difficulty']=None
            rows.append(row)
        selected=select_release(rows,{'family_by_doc':{'d':'d'}})
        validate_quotas(selected)
        self.assertEqual(next(i for i in selected if i['id']=='q-0')['split'],'dev')
        dev=[i for i in selected if i['split']=='dev']
        for i in dev[:25]:i['query_style']='document'
        with self.assertRaisesRegex(ValueError,'style cap'):validate_quotas(selected)


@unittest.skipUnless(Path('data/benchmark/trial_v1/candidates.jsonl').exists(), 'Local corpus fixture is not distributed')
class TrialRegressionTests(unittest.TestCase):
    def test_all_revisions_and_specific_source_fixes(self):
        from scripts.revise_benchmark_trial import revise
        corpus=Corpus('data/processed/baseline_v1')
        before=corpus.identity()
        rows=revise(corpus,read_jsonl('data/benchmark/trial_v1/candidates.jsonl'))
        self.assertEqual(len(rows),30)
        self.assertEqual(sum(i['query_style']=='document' for i in rows),0)
        self.assertEqual(sum(i['expected_behavior']!='answer' for i in rows),2)
        self.assertEqual(before,corpus.identity())
        def row(suffix):return next(i for i in rows if i['source_id']=='pilot_v2-'+suffix)
        def bids(suffix):return {e['block_id'] for g in row(suffix)['evidence_groups'] for a in g['alternatives'] for e in a}
        self.assertIn(172,bids('0001'))
        self.assertTrue({24,26,28,29,30}<=bids('0009'))
        self.assertTrue({35,36}<=bids('0029'))
        self.assertTrue({625,628,629,630}<=bids('0002'))
        self.assertTrue({165,166}<=bids('0007'))
        self.assertTrue({415,416}<=bids('0026'))
        self.assertTrue({110,111}<=bids('0016'))
        self.assertNotIn(166,bids('0012'))
        self.assertNotIn(240,bids('0030'))
        self.assertNotIn('十四周岁',row('0013')['answer_points'][-1])
        self.assertIn('采信',row('0022')['reference'])
        self.assertNotIn('运行',row('0022')['reference'])
        self.assertIn('具身智能',row('0021')['reference'])
        self.assertNotIn('卫星',row('0021')['reference'])
        self.assertNotIn('什么时候',row('0027')['user_input'])
        for i in rows:
            validate_item(i,corpus)
            self.assertEqual(i['review_status'],'pending')
            self.assertFalse(i['review_checks'])
            self.assertEqual(evidence_audit(i,corpus)['issues'],[])


if __name__=='__main__':
    unittest.main()
