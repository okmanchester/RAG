import json
import tempfile
import unittest
from pathlib import Path

from rag_baseline.retrieval import annotate_chunk, iter_retrieval_chunks, mark_existing


def chunk(headings, block=1):
    return {"chunk_id": str(block), "doc_id": "doc", "heading_path": headings,
            "sources": [{"block_id": block}], "body": "保留原文", "text": "标题\n保留原文",
            "source_path": "test.docx", "token_count": 10}


class RetrievalTests(unittest.TestCase):
    def test_toc_and_overlap_are_excluded_but_business_catalogs_remain(self):
        for title in ["目录", "目 次", "图目录", "表目录", "Table of Contents"]:
            value = chunk([title])
            annotate_chunk(value)
            self.assertFalse(value["retrieval_enabled"])
        value = chunk(["普通章节"])
        value["sources"][0]["is_overlap"] = True
        annotate_chunk(value, {1})
        self.assertFalse(value["retrieval_enabled"])
        for title in ["联网工业企业所属行业网络安全分类指导目录", "6.2 设计产品目录", "正文"]:
            value = chunk([title])
            annotate_chunk(value)
            self.assertTrue(value["retrieval_enabled"])

    def test_annotation_is_idempotent_preserves_records_and_gates_ingestion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            originals = [chunk(["目录"], 1), chunk(["普通章节"], 2)]
            (root / "chunks.jsonl").write_text("\n".join(map(json.dumps, originals)), encoding="utf-8")
            (root / "documents.jsonl").write_text(json.dumps({"doc_id": "doc", "blocks": [
                {"block_id": 1, "role": "toc"}, {"block_id": 2, "role": "body"}]}), encoding="utf-8")
            (root / "report.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "retrieval_enabled"):
                list(iter_retrieval_chunks(root / "chunks.jsonl"))
            mark_existing(root)
            first = (root / "chunks.jsonl").read_bytes()
            mark_existing(root)
            self.assertEqual(first, (root / "chunks.jsonl").read_bytes())
            saved = list(map(json.loads, first.decode().splitlines()))
            self.assertEqual(len(saved), len(originals))
            for old, new in zip(originals, saved):
                self.assertTrue(all(new[k] == v for k, v in old.items()))
            self.assertEqual([c["chunk_id"] for c in iter_retrieval_chunks(root / "chunks.jsonl")], ["2"])
