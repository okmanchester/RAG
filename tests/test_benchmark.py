import asyncio
import copy
import csv
import json
import tempfile
import unittest
import io
import os
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ["RAGAS_DO_NOT_TRACK"] = "true"

from rag_baseline.benchmark.core import (
    Corpus, REVIEW_CHECKS, TYPES, digest, duplicate_clusters, export_review, freeze,
    import_review, load_release, select_release, validate_item, validate_quotas, write_json, write_jsonl,
)
from rag_baseline.benchmark.evaluation import (
    Judgment, evidence_hit, evaluate_release, retrieval_metrics, score_judgment,
    summarize_scores, validate_run,
)
from rag_baseline.benchmark.generation import UsageRecorder, build_llm, generate_batch, generate_one, AnnotatedDraft, Probe


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "corpus"
        docs, chunks = [], []
        for category in ("policy", "technical"):
            for d in range(2):
                doc_id = f"{category}-{d}"
                blocks = [{"block_id": i, "text": f"条款{i}：{digest([doc_id, i])}，应当完成检查并保存记录。",
                           "role": "body", "source": {"page": i+1}} for i in range(600)]
                docs.append({"doc_id": doc_id, "sha256": digest(doc_id), "source_path": doc_id+".pdf", "category": category, "blocks": blocks})
                for block in blocks:
                    chunks.append({"chunk_id": f"{doc_id}-{block['block_id']}", "doc_id": doc_id, "source_path": doc_id+".pdf",
                                   "category": category, "body": block["text"], "text": block["text"], "retrieval_enabled": True,
                                   "heading_path": [], "section_id": block["block_id"], "title": "测试文档",
                                   "sources": [{"block_id": block["block_id"], "block_char_start": 0, "block_char_end": len(block["text"])}]})
        write_jsonl(self.source/"documents.jsonl", docs)
        write_jsonl(self.source/"chunks.jsonl", chunks)
        self.corpus = Corpus(self.source)

    def item(self, index=0, kind="fact", category="policy"):
        doc_id = category+"-0"
        block = self.corpus.blocks[(doc_id, index)]
        evidence = self.corpus.evidence(doc_id, index, block["text"])
        groups = [{"id": "g1", "alternatives": [[evidence]]}]
        if kind in ("within_document", "cross_document"):
            doc2 = doc_id if kind == "within_document" else category+"-1"
            b = self.corpus.blocks[(doc2, index+1)]
            groups.append({"id": "g2", "alternatives": [[self.corpus.evidence(doc2, index+1, b["text"])]]})
        return {"id": f"q-{index:04d}", "user_input": digest([index, "question"]), "reference": digest([index, "answer"]),
                "answer_points": [digest([index, "point"])], "evidence_groups": groups, "category": category,
                "question_type": kind, "difficulty": "boundary" if kind == "boundary" else "easy", "query_style": "natural",
                "expected_behavior": "abstain" if kind == "boundary" else "answer", "boundary_reason": "语料未提供" if kind == "boundary" else "",
                "duplicate_group": "", "review_status": "approved", "reviewer": "human", "review_notes": "",
                "review_checks": {k: True for k in REVIEW_CHECKS}, "pilot": False,
                "corpus_search": [{"chunk_id": f"{doc_id}-{index}", "score": .1}] if kind == "boundary" else []}

    def pool(self):
        rows = []
        for split, fraction in (("dev", .8), ("holdout", .2)):
            ai, bi, ci = 0, 0, 0
            for kind, count in TYPES.items():
                for _ in range(int(count*fraction)):
                    item = self.item(len(rows), kind, "policy" if ci % 2 == 0 else "technical")
                    ci += 1
                    if kind == "boundary":
                        item["expected_behavior"] = ["abstain", "correct_premise", "clarify"][bi % 3]
                        if item["expected_behavior"] == "clarify":
                            item["clarifying_interpretations"] = ["解释一", "解释二"]
                            item["reference"] += "请问您指的是哪一种？"
                        bi += 1
                    else:
                        item["difficulty"] = "easy" if ai < int(90*fraction) else "medium" if ai < int(225*fraction) else "hard"
                        item["query_style"] = "document" if ai % 3 == 0 else "natural"
                        ai += 1
                    item["split"] = split
                    rows.append(item)
        return rows

    def family(self):
        return {"family_by_doc": {k: k for k in self.corpus.documents}, "review_status": "approved", "reviewer": "human"}

    def test_exact_selection_and_freeze_integrity(self):
        rows = self.pool()
        validate_quotas(rows)
        rows[0]["pilot"] = True
        selected = select_release(rows, self.family())
        validate_quotas(selected)
        self.assertEqual(next(r for r in selected if r["id"] == rows[0]["id"])["split"], "dev")
        release = self.root/"v1"
        freeze(rows, self.corpus, self.family(), release, {"corpus": self.corpus.identity()})
        self.assertEqual(len(load_release(release)[0]), 300)
        with self.assertRaises(FileExistsError):
            freeze(rows, self.corpus, self.family(), release, {"corpus": self.corpus.identity()})
        with (release/"gold.jsonl").open("a") as f:
            f.write("\n")
        with self.assertRaisesRegex(ValueError, "modified"):
            load_release(release)

    def test_infeasible_pool_and_unreviewed_freeze(self):
        with self.assertRaisesRegex(ValueError, "approved"):
            select_release([self.item()], self.family())
        family = self.family()
        family["review_status"] = "pending"
        with self.assertRaisesRegex(ValueError, "human"):
            freeze([], self.corpus, family, self.root/"v1", {"corpus": self.corpus.identity()})

    def test_review_round_trip_stale_and_explicit_checks(self):
        original = self.item()
        original["review_status"] = "pending"
        folder = self.root/"review"
        export_review([original], folder, self.corpus)
        with self.assertRaises(FileExistsError):
            export_review([original], folder, self.corpus)
        path = folder/"review.csv"
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        rows[0]["review_status"] = "approved"
        def save():
            with path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, rows[0].keys()); writer.writeheader(); writer.writerows(rows)
        save()
        with self.assertRaisesRegex(ValueError, "checks"):
            import_review([original], path, self.corpus)
        rows[0]["review_checks"] = json.dumps({k: True for k in REVIEW_CHECKS})
        rows[0]["reference"] = "人工修改后的参考答案"
        save()
        imported = import_review([original], path, self.corpus)
        self.assertEqual(imported[0]["reference"], rows[0]["reference"])
        changed = dict(original, reference="another")
        with self.assertRaisesRegex(ValueError, "Stale"):
            import_review([changed], path, self.corpus)

    def test_rechunking_union_and_alternative_evidence(self):
        item = self.item()
        e = item["evidence_groups"][0]["alternatives"][0][0]
        middle = len(e["text"])//2
        contexts = [{"id": f"new-{a}", "doc_id": e["doc_id"], "document_sha256": e["document_sha256"],
                     "text": e["text"][a:b], "sources": [{"block_id": e["block_id"], "block_char_start": a, "block_char_end": b}]}
                    for a,b in ((0,middle),(middle,len(e["text"])))]
        self.assertFalse(evidence_hit(e, contexts[:1]))
        self.assertTrue(evidence_hit(e, contexts))
        self.assertEqual(retrieval_metrics(item, contexts)["evidence_recall@5"], 1.)
        contexts[1]["document_sha256"] = "wrong"
        self.assertFalse(evidence_hit(e, contexts))
        alternate = self.corpus.evidence("policy-0", 1, self.corpus.blocks[("policy-0",1)]["text"])
        item["evidence_groups"][0]["alternatives"].append([alternate])
        c = {"id": "alt", "doc_id": alternate["doc_id"], "document_sha256": alternate["document_sha256"], "text": alternate["text"]}
        self.assertEqual(retrieval_metrics(item, [c])["all_evidence@5"], 1.)

    def test_wrong_sources_and_boundary_metrics(self):
        item = self.item()
        e = item["evidence_groups"][0]["alternatives"][0][0]
        row = {"id": item["id"], "response": "answer", "run_config": {"version": "test"}, "error": None, "citations": [],
               "retrieved_contexts": [{"id": "c", "doc_id": e["doc_id"], "document_sha256": e["document_sha256"], "text": "unrelated",
                                       "sources": [{"block_id": 0, "block_char_start": 0, "block_char_end": 10}]}]}
        with self.assertRaisesRegex(ValueError, "does not occur"):
            validate_run(row, self.corpus)
        self.assertEqual(retrieval_metrics(self.item(kind="boundary"), []), {})
        e["text"] = "fabricated quote"
        with self.assertRaisesRegex(ValueError, "mismatch"):
            validate_item(item, self.corpus)

    def test_duplicates_and_missing_scores_visible(self):
        a = self.item()
        b = self.item(1)
        b["user_input"] = a["user_input"]
        groups, links = duplicate_clusters([a,b])
        self.assertEqual(len(groups),1)
        self.assertEqual(len(links),1)
        scores = [{"id": a["id"], "status": "ok", "metrics": {"evidence_recall@5": 1}},
                  {"id": b["id"], "status": "missing_run", "metrics": {}}]
        report = summarize_scores([a,b], scores, True)
        metric = report["overall"]["metrics"]["evidence_recall@5"]
        self.assertEqual(metric["completion_rate"], .5)
        self.assertEqual(metric["eligible_count"], 2)

    def test_judgment_cardinality_and_citation_validation(self):
        item = self.item()
        judgment = Judgment(point_verdicts=["partial"], citation_verdicts=[True],
                            claims=[{"claim": "answer", "citation_supported": True}], boundary_correct=None, reason="test")
        row = {"response": "answer", "retrieved_contexts": [], "citations": [{"claim": "answer", "context_ids": ["missing"]}]}
        scores = score_judgment(item, row, judgment)
        self.assertEqual(scores["answer_point_completeness"], .5)
        self.assertEqual(scores["citation_support"], 0)
        self.assertEqual(scores["citation_coverage"], 0)
        judgment.point_verdicts = []
        with self.assertRaisesRegex(ValueError, "cardinality"):
            score_judgment(item, row, judgment)

    def test_evaluation_resume_and_input_identity(self):
        rows = self.pool()
        release = self.root/"v1"
        freeze(rows, self.corpus, self.family(), release, {"corpus": self.corpus.identity()})
        runs = self.root/"runs.jsonl"
        write_jsonl(runs, [])
        out = self.root/"eval"
        with redirect_stdout(io.StringIO()):
            first = asyncio.run(evaluate_release(release, runs, out, retrieval_only=True))
            second = asyncio.run(evaluate_release(release, runs, out, retrieval_only=True))
        self.assertEqual(first["overall"], second["overall"])
        selected = self.root/"selected_ids.jsonl"
        dev_ids = [i["id"] for i in load_release(release)[0] if i["split"] == "dev"][:2]
        write_jsonl(selected, [{"id": identifier} for identifier in dev_ids])
        with redirect_stdout(io.StringIO()):
            subset = asyncio.run(evaluate_release(release, runs, self.root/"subset", retrieval_only=True, ids_path=selected))
        self.assertEqual(subset["overall"]["count"], 2)
        self.assertEqual(subset["overall"]["statuses"], {"missing_run": 2})
        item = next(i for i in load_release(release)[0] if i["split"] == "dev")
        write_jsonl(runs, [{"id": item["id"], "response": "", "run_config": {"test": 1}, "error": "timeout", "retrieved_contexts": [], "citations": []}])
        with self.assertRaisesRegex(ValueError, "changed"):
            asyncio.run(evaluate_release(release, runs, out, retrieval_only=True))

    def test_missing_key_no_default_provider(self):
        with patch.dict("os.environ", {"DASHSCOPE_API_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "DASHSCOPE_API_KEY"):
                build_llm("judge", self.root/"usage.jsonl")

    def test_mock_judge_full_evaluation_and_partial_failure(self):
        from unittest.mock import Mock
        from ragas.metrics.collections import Faithfulness
        from ragas.llms.base import InstructorBaseRagasLLM
        a,b = self.item(), self.item(1)
        a["split"]=b["split"]="dev"
        rows=[]
        for item in (a,b):
            e=item["evidence_groups"][0]["alternatives"][0][0]
            rows.append({"id":item["id"],"response":"完成检查。","error":None,"run_config":{"version":"mock"},
                         "retrieved_contexts":[{"id":"c1","doc_id":e["doc_id"],"document_sha256":e["document_sha256"],"text":e["text"]}],
                         "citations":[{"claim":"完成检查","context_ids":["c1"]}]})
        run_path=self.root/"run.jsonl";write_jsonl(run_path,rows)
        judgment=Judgment(point_verdicts=["correct"],citation_verdicts=[True],claims=[{"claim":"完成检查","citation_supported":True}],boundary_correct=None,reason="test")
        llm=Mock(spec=InstructorBaseRagasLLM);llm.agenerate=AsyncMock(side_effect=[Probe(ok=True),judgment,RuntimeError("simulated judge failure")])
        client=Mock();client.close=AsyncMock()
        with patch("rag_baseline.benchmark.evaluation.load_release",return_value=([a,b],{"gold_sha256":"test"})), \
             patch("rag_baseline.benchmark.evaluation.build_llm",return_value=(llm,client)), \
             patch.object(Faithfulness,"ascore",new=AsyncMock(return_value=Mock(value=1.0))), \
             redirect_stdout(io.StringIO()):
            report=asyncio.run(evaluate_release(self.source,run_path,self.root/"scores", metric_version=1))
        self.assertEqual(report["overall"]["statuses"],{"ok":1,"judge_error":1})
        self.assertEqual(report["overall"]["metrics"]["answer_point_accuracy"]["completion_rate"],.5)
        self.assertEqual(report["overall"]["metrics"]["faithfulness"]["completion_rate"],1.)
        client.close.assert_awaited_once()

    def test_concurrent_evaluation_preserves_order_and_limits_inflight(self):
        from unittest.mock import Mock
        from ragas.metrics.collections import Faithfulness
        from ragas.llms.base import InstructorBaseRagasLLM
        items = [self.item(n) for n in range(3)]
        for item in items:
            item["split"] = "dev"
        rows = [{"id": item["id"], "response": "完成检查。", "error": None,
                 "run_config": {"version": "mock"}, "retrieved_contexts": [], "citations": []} for item in items]
        run_path = self.root / "parallel-runs.jsonl"
        write_jsonl(run_path, rows)
        active, peak = 0, 0
        async def judge(prompt, model):
            nonlocal active, peak
            if model is Probe:
                return Probe(ok=True)
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.02)
            active -= 1
            return Judgment(point_verdicts=["correct"], citation_verdicts=[], claims=[],
                            boundary_correct=None, reason="test")
        llm = Mock(spec=InstructorBaseRagasLLM)
        llm.agenerate = AsyncMock(side_effect=judge)
        client = Mock()
        client.close = AsyncMock()
        output = self.root / "parallel-scores"
        with patch("rag_baseline.benchmark.evaluation.load_release", return_value=(items, {"gold_sha256": "test"})), \
             patch("rag_baseline.benchmark.evaluation.build_llm", return_value=(llm, client)), \
             patch.object(Faithfulness, "ascore", new=AsyncMock(return_value=Mock(value=1.))), \
             redirect_stdout(io.StringIO()):
            report = asyncio.run(evaluate_release(self.source, run_path, output, concurrency=2, metric_version=1))
        self.assertEqual(peak, 2)
        self.assertEqual(report["overall"]["statuses"], {"ok": 3})
        self.assertEqual([json.loads(line)["id"] for line in (output / "scores.jsonl").read_text(encoding="utf-8").splitlines()],
                         [item["id"] for item in items])
        client.close.assert_awaited_once()

    def test_usage_logging_omits_prompt_and_key(self):
        import httpx
        response = httpx.Response(200, json={"model": "test", "usage": {"total_tokens": 5}, "id": "r", "choices": [{"message": "private"}]})
        recorder = UsageRecorder(self.root/"usage.jsonl")
        asyncio.run(recorder.response(response))
        text = (self.root/"usage.jsonl").read_text()
        self.assertNotIn("private", text)
        self.assertIn("total_tokens", text)

    def test_ragas_single_hop_uses_compatible_scenario(self):
        from ragas.testset.graph import Node, NodeType
        from ragas.testset.synthesizers.single_hop.prompts import QueryAnswerGenerationPrompt, GeneratedQueryAnswer
        node = Node(type=NodeType.CHUNK, properties={"page_content": "材料", "themes": ["合规"]})
        task = {"id": "mock-1", "category": "policy", "question_type": "fact", "difficulty": "easy",
                "query_style": "natural", "expected_behavior": "answer", "node_ids": [str(node.id)],
                "chunk_ids": ["policy-0-0"], "synthesizer": "single_hop", "pilot": True}
        block = self.corpus.blocks[("policy-0", 0)]
        annotation = AnnotatedDraft(user_input="企业需要完成什么检查？", reference="应当完成检查并保存记录。",
                                    answer_points=["完成检查", "保存记录"], evidence_groups=[{"quotes": [{"doc_id": "policy-0", "block_id": 0, "text": block["text"]}]}])
        from unittest.mock import Mock
        llm = Mock()
        llm.agenerate = AsyncMock(return_value=annotation)
        with patch.object(QueryAnswerGenerationPrompt, "generate", new=AsyncMock(return_value=GeneratedQueryAnswer(query="测试问题", answer="测试答案"))):
            item = asyncio.run(generate_one(task, self.corpus, {str(node.id): node}, llm, self.root/"gen", None))
        self.assertEqual(item["review_status"], "pending")
        self.assertEqual(item["evidence_groups"][0]["alternatives"][0][0]["block_id"], 0)

    def test_generation_failure_resume_without_repeating_success(self):
        from ragas.testset.graph import KnowledgeGraph, Node, NodeType
        from unittest.mock import Mock
        kg = KnowledgeGraph(nodes=[Node(type=NodeType.CHUNK, properties={"page_content": c["text"], "document_metadata": {"chunk_id": cid}})
                                   for cid,c in self.corpus.chunks.items()])
        path = self.root/"kg.json"
        kg.save(path)
        tasks = [{"id": "q-0000", "question_type": "fact"}, {"id": "q-0001", "question_type": "fact"}]
        client = Mock(); client.close = AsyncMock()
        llm = Mock()
        gen = AsyncMock(side_effect=[self.item(0), RuntimeError("simulated")])
        out = self.root/"batch"
        patches = [patch("rag_baseline.benchmark.generation.build_tasks", return_value=tasks),
                   patch("rag_baseline.benchmark.generation.build_llm", return_value=(llm,client)),
                   patch("rag_baseline.benchmark.generation.CorpusSearch"),
                   patch("rag_baseline.benchmark.generation.generate_one", gen)]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches: stack.enter_context(p)
            stack.enter_context(redirect_stdout(io.StringIO()))
            first = asyncio.run(generate_batch(self.source, path, out, size=2))
            self.assertEqual(first["completed"],1)
            gen.reset_mock();gen.side_effect=None;gen.return_value=self.item(1)
            second = asyncio.run(generate_batch(self.source, path, out, size=2,retry_failed=True))
            self.assertEqual(second["completed"],2)
            self.assertEqual(gen.await_count,1)
            self.assertTrue((out/"failure_history.jsonl").exists())

    def test_mock_http_retry_is_metered(self):
        import httpx
        from openai import AsyncOpenAI
        from ragas.llms import llm_factory
        attempts = []
        async def handler(request):
            attempts.append(json.loads(request.content))
            if len(attempts) == 1:
                return httpx.Response(429, json={"error": {"message": "rate limited", "type": "rate_limit"}}, headers={"retry-after-ms": "1"})
            return httpx.Response(200, json={"id": "mock", "object": "chat.completion", "created": 1,
                "model": "test", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": '{"ok":true}',
                "tool_calls": [{"id": "call1", "type": "function", "function": {"name": "Probe", "arguments": '{"ok":true}'}}]}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})
        async def run():
            recorder = UsageRecorder(self.root/"retry_usage.jsonl")
            client = AsyncOpenAI(api_key="test-only", max_retries=1, http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler), event_hooks={"response": [recorder.response]}))
            try:
                llm = llm_factory("test", client=client, max_retries=1)
                return await llm.agenerate("Return ok=true", Probe)
            finally:
                await client.close()
        self.assertTrue(asyncio.run(run()).ok)
        self.assertEqual(len(attempts),2)
        entries = [json.loads(x) for x in (self.root/"retry_usage.jsonl").read_text().splitlines()]
        self.assertEqual([r["http_status"] for r in entries], [429,200])


if __name__ == "__main__":
    unittest.main()
