import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel
from ragas.llms import llm_factory
from ragas.testset.graph import KnowledgeGraph, Node, NodeType, Relationship
from ragas.testset.transforms.base import Extractor

from scripts import build_benchmark_kg as builder


@dataclass
class FakeExtractor(Extractor):
    property_name: str = "summary"
    calls: list = field(default_factory=list)
    fail: bool = True

    async def extract(self, node):
        name = node.get_property("page_content")
        self.calls.append(name)
        if self.fail and name == "bad":
            raise ValueError("simulated extraction failure")
        return self.property_name, "summary of " + name


class KGTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkpoint = builder.Checkpoint(self.root, {"pipeline_version": 2})

    def test_help_and_missing_key(self):
        env = dict(os.environ)
        env.pop("DEEPSEEK_API_KEY", None)
        result = subprocess.run(
            [sys.executable, str(Path(builder.__file__).resolve()), "--help"],
            env=env, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "DEEPSEEK_API_KEY"):
                builder.build_llm()

    def test_partial_resume_without_cache_and_missing_report(self):
        kg = KnowledgeGraph(nodes=[
            Node(type=NodeType.CHUNK, properties={"page_content": name})
            for name in ("good", "bad")
        ])
        extractor = FakeExtractor(filter_nodes=builder.missing_property("summary"))
        stage = builder.Stage("summary", extractor, property_name="summary")
        with self.assertRaisesRegex(ValueError, "simulated extraction failure"):
            builder.run_stage(kg, stage, 0, self.checkpoint)
        report = self.checkpoint.report_dir / "missing_summary.jsonl"
        self.assertEqual(len(report.read_text(encoding="utf-8").splitlines()), 1)
        restored, index = self.checkpoint.load()
        self.assertEqual(index, 0)
        self.assertEqual(restored.nodes[0].get_property("summary"), "summary of good")
        extractor.fail = False
        extractor.calls.clear()
        builder.run_stage(restored, stage, index, self.checkpoint)
        self.assertEqual(extractor.calls, ["bad"])
        self.assertFalse(report.exists())
        self.assertEqual(self.checkpoint.load()[1], 1)

    def test_relationship_rebuild_preserves_other_types(self):
        nodes = [Node(type=NodeType.CHUNK, properties={
            "summary_embedding": vector, "entities": entities,
        }) for vector, entities in [
            ([1., 0.], ["noise", "shared", "first"]),
            ([1., 0.], ["noise", "shared", "second"]),
            ([0., 1.], ["noise", "third"]),
        ]]
        unrelated = Relationship(source=nodes[0], target=nodes[1], type="keep")
        kg = KnowledgeGraph(nodes=nodes, relationships=[unrelated])
        stages = builder.build_stages(Mock(), Mock())
        for index in (4, 5):
            stage = stages[index]
            builder.run_stage(kg, stage, index, self.checkpoint)
            count = sum(rel.type == stage.relationship_type for rel in kg.relationships)
            self.assertGreater(count, 0)
            # 模拟 KG 已落盘、state 尚未推进的恢复窗口。
            self.checkpoint.save(kg, index)
            kg, next_index = self.checkpoint.load()
            before = [(r.source.id, r.target.id, r.type) for r in kg.relationships]
            builder.run_stage(kg, stage, next_index, self.checkpoint)
            self.assertCountEqual(before, [(r.source.id, r.target.id, r.type) for r in kg.relationships])
        self.assertTrue(any(rel.id == unrelated.id for rel in kg.relationships))

    def test_checkpoint_identity_and_index_validation(self):
        self.checkpoint.save(KnowledgeGraph(), 0)
        with self.assertRaisesRegex(RuntimeError, "不兼容"):
            builder.Checkpoint(self.root, {"pipeline_version": 1}).load()
        self.checkpoint.save(KnowledgeGraph(), 7)
        with self.assertRaisesRegex(RuntimeError, "索引"):
            self.checkpoint.load()
        self.checkpoint.state_path.unlink()
        with self.assertRaisesRegex(RuntimeError, "不完整"):
            self.checkpoint.load()

    def test_stages_and_existing_empty_properties(self):
        stages = builder.build_stages(Mock(), Mock())
        self.assertEqual(tuple(stage.name for stage in stages), builder.STAGE_NAMES)
        for stage in stages[:4]:
            node = Node(type=NodeType.CHUNK, properties={stage.property_name: []})
            self.assertFalse(stage.transform.filter_nodes(node))
            self.assertTrue(stage.transform.filter_nodes(Node(type=NodeType.CHUNK)))
            self.assertFalse(stage.transform.filter_nodes(Node(type=NodeType.DOCUMENT)))

    def test_completed_checkpoint_exports_without_models(self):
        identity = {"pipeline_version": 2}
        checkpoint = builder.Checkpoint(self.root / "kg_build" / "smoke", identity)
        checkpoint.save(KnowledgeGraph(), len(builder.STAGE_NAMES))
        with patch.object(builder, "BENCHMARK_DIR", self.root), \
             patch.object(builder, "parse_args", return_value=Mock(mode="smoke", reset=False, corpus=self.root)), \
             patch.object(builder, "load_chunks", return_value=[]), \
             patch.object(builder, "build_identity", return_value=identity), \
             patch.object(builder, "build_llm") as llm, \
             patch.object(builder, "build_embeddings") as embeddings:
            builder.main()
        llm.assert_not_called()
        embeddings.assert_not_called()
        self.assertTrue((self.root / "knowledge_graph.smoke.json").exists())

    def test_loader_and_sampling(self):
        rows = [{
            "chunk_id": str(i), "chunk_index": i, "doc_id": "doc", "source_path": "doc.md",
            "text": "内容" * (i + 1), "retrieval_enabled": i != 0,
            "sources": [{"page": 2}, {"page": 1}, {"page": 2}],
        } for i in range(26)]
        path = self.root / "chunks.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        docs = builder.load_chunks(path)
        self.assertEqual(len(docs), 25)
        self.assertEqual(docs[0].metadata["pages"], [1, 2])
        sample = builder.select_smoke_chunks(docs, 20)
        self.assertEqual(len({doc.metadata["chunk_id"] for doc in sample}), 20)
        self.assertEqual(sample, builder.select_smoke_chunks(docs, 20))
        self.assertEqual(sample[0].metadata["chunk_id"], "25")

    def test_instructor_attempts_consumed_before_http(self):
        class Answer(BaseModel):
            value: int

        bodies = []

        def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 0,
                "model": builder.LLM_MODEL,
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": '{"value":"invalid"}',
                }, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })

        async def run():
            async with AsyncOpenAI(
                api_key="offline-test", max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ) as client:
                llm = llm_factory(builder.LLM_MODEL, client=client,
                                  max_retries=builder.STRUCTURED_OUTPUT_MAX_ATTEMPTS)
                from instructor.retry import InstructorRetryException
                with self.assertRaises(InstructorRetryException):
                    await llm.agenerate("Return value", Answer)

        asyncio.run(run())
        self.assertEqual(len(bodies), 2)
        self.assertTrue(all("max_retries" not in body for body in bodies))


if __name__ == "__main__":
    unittest.main()
