import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag.chunk import run
from rag.parsers import adapt_middle, configure, parse, pdf_health
from rag.splitter import chunk_document, TokenCounter
from rag.structure import build_sections
from rag.verify import verify
from rag.benchmark.core import Corpus


def node(typ, content, **kwargs):
    return {"type": typ, "content": content, **kwargs}


def middle(*pages, suffix="pdf"):
    return {"schema": "docvortex.middle", "schema_version": "2.0", "is_full_document": True,
            "metadata": {"file_suffix": suffix, "producer": {"name": "mineru", "version": "4.0.10"}},
            "extensions": {"mineru": {"tier": "standard" if suffix == "pdf" else "flash", "parse_mode": "auto"}},
            "pages": [{"page_idx": i, "blocks": [{"index": j, "bbox": [.1, .1, .9, .9], **b}
                        for j, b in enumerate(page)]} for i, page in enumerate(pages)]}


def split(raw, maximum=100, overlap=15):
    blocks, meta = adapt_middle(raw, "." + raw["metadata"]["file_suffix"])
    title, sections, warnings = build_sections(blocks, "样例文档")
    document = {"doc_id": "doc", "source_path": "sample.pdf", "category": "technical",
                "title": title, "blocks": blocks, "artifacts": "parsed/doc", **meta}
    return chunk_document(document, sections, len, maximum, overlap), document


def fake_parse(raw):
    def do_parse(path, *, artifact_dir, **kwargs):
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "middle_json.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        blocks, metadata = adapt_middle(raw, path.suffix.lower())
        return blocks, metadata
    return do_parse


class StructureTests(unittest.TestCase):
    def test_explicit_heading_and_cross_page_sources(self):
        raw = middle([node("doc_title", "测试文档", level=1), node("paragraph_title", "管理要求", level=2),
                      node("text", "前页正文。")], [node("text", "后页正文。"), node("page_footnote", "脚注内容")])
        chunks, doc = split(raw)
        self.assertEqual(chunks[0]["heading_path"], ["管理要求"])
        self.assertEqual({s["page"] for s in chunks[0]["sources"]}, {1, 2})
        self.assertIn("脚注内容", chunks[0]["body"])

    def test_numbered_prose_not_a_heading(self):
        chunks, _ = split(middle([node("paragraph_title", "第一条 企业应当定期检查。", level=2),
                                  node("text", "（一）落实措施。企业应保存记录。")]))
        self.assertEqual(chunks[0]["heading_path"], [])
        self.assertIn("第一条", chunks[0]["body"])

    def test_numeric_heading_fallback_and_plain_title(self):
        chunks, _ = split(middle([node("text", "4 评价原则"), node("text", "4 . 1 目标导向"),
                                  node("text", "正文。")]))
        self.assertEqual(chunks[0]["heading_path"], ["4 评价原则", "4 . 1 目标导向"])

    def test_toc_and_auxiliary_audit(self):
        raw = middle([node("header", "页眉内容"), node("doc_title", "样例文档", level=1),
                      node("index", [node("text", "第一章 总则……1")]),
                      node("paragraph_title", "第一章 总则", level=2), node("text", "实质正文。"),
                      node("footer", "页脚内容"), node("page_number", "3")])
        chunks, doc = split(raw)
        self.assertFalse(chunks[0]["retrieval_enabled"])
        self.assertTrue(chunks[-1]["retrieval_enabled"])
        self.assertEqual(sum(b["role"] == "excluded" for b in doc["blocks"]), 3)
        self.assertNotIn("页眉内容", "".join(c["body"] for c in chunks))

    def test_substantive_catalog_is_eligible(self):
        chunks, _ = split(middle([node("paragraph_title", "行业分类指导目录", level=2), node("text", "正文内容。")]))
        self.assertTrue(all(c["retrieval_enabled"] for c in chunks))

    def test_merged_cells_and_repeated_table_context(self):
        html = '<table><thead><tr><th>名称</th><th>说明</th></tr></thead><tr><td rowspan="2">合并</td><td>' + "内容。" * 80 + '</td></tr><tr><td>后一行</td></tr></table>'
        raw = middle([node("text", "表前正文。"), node("table", [node("table_caption", "表1 示例"),
                      node("table_body", html)]), node("text", "表后正文。")])
        chunks, doc = split(raw)
        tables = [c for c in chunks if c["structure_type"] == "table"]
        self.assertGreater(len(tables), 2)
        self.assertTrue(all(c["overlap_char_count"] == 0 for c in tables))
        self.assertTrue(all("名称 | 说明" in c["text"] for c in tables))
        self.assertTrue(any(c["structure_split"] for c in tables))
        rows = [b for b in doc["blocks"] if b["kind"] == "table_row"]
        self.assertEqual(rows[1]["cells"][0]["rowspan"], 2)
        self.assertEqual(rows[2]["cells"][0]["column"], 2)
        self.assertEqual(sum(c["body"].count("合并") for c in tables), 1)
        self.assertTrue(any(s.get("cell_spans") for c in tables for s in c["sources"]))
        self.assertTrue(all(c["token_count"] <= 100 for c in chunks))

    def test_oversized_table_context_is_audited_without_loss(self):
        html = '<table><tr><th>' + "超长表头" * 50 + '</th></tr><tr><td>内容</td></tr></table>'
        chunks, _ = split(middle([node("table", [node("table_body", html)])]))
        self.assertTrue(all("table_context_omitted_budget" in c["warnings"] for c in chunks))
        self.assertEqual("".join(c["body"] for c in chunks), "超长表头" * 50 + "\n\n内容")

    def test_plain_td_headers_require_explicit_column_labels(self):
        html = '<table><tr><td>名称</td><td>说明</td></tr><tr><td>A</td><td>正文</td></tr></table>'
        _, doc = split(middle([node("table", [node("table_body", html)])]))
        self.assertTrue(doc["blocks"][0]["is_table_header"])
        self.assertEqual(doc["blocks"][0]["header_detection"], "column_labels")
        _, doc = split(middle([node("table", [node("table_body", '<table><tr><td>A</td><td>正文</td></tr></table>')])]))
        self.assertFalse(doc["blocks"][0]["is_table_header"])

    def test_equation_code_and_image_annotations_are_isolated(self):
        raw = middle([node("text", "普通正文。"), node("equation", "E=mc^2" * 30),
                      node("code", [node("code_body", "print(1)\n" * 30)]),
                      node("image", [node("image_body", "", image_path="images/test.png"),
                                     node("image_caption", "图1 流程图"), node("image_footnote", "图注内容")])])
        chunks, doc = split(raw)
        self.assertEqual({c["structure_type"] for c in chunks}, {"body", "equation", "code", "image"})
        self.assertTrue(all(c["overlap_token_count"] == 0 for c in chunks if c["structure_type"] != "body"))
        image = [c for c in chunks if c["structure_type"] == "image"][0]
        self.assertTrue(image["asset_refs"])
        self.assertIn("图注内容", image["body"])
        self.assertTrue(any(b["exclusion_reason"] == "nontext_asset" for b in doc["blocks"]))

    def test_unknown_type_is_visible(self):
        chunks, doc = split(middle([node("future_kind", "可读取未知内容")]))
        self.assertIn("unknown_block_type", doc["parser_warnings"][0])
        self.assertEqual(chunks[0]["body"], "可读取未知内容")

    def test_docx_positions_never_claim_physical_pages(self):
        chunks, doc = split(middle([node("text", "前文"), node("table", [node("table_body", '<table><tr><td>表格</td></tr></table>')]),
                                  node("text", "后文")], suffix="docx"))
        self.assertIsNone(doc["page_count"])
        self.assertTrue(all("page" not in s and "content_index" in s for c in chunks for s in c["sources"]))

    def test_token_overlap_and_replay(self):
        raw = middle([node("text", "数据安全要求。" * 60), node("paragraph_title", "第二章 要求", level=2),
                      node("text", "后续正文。" * 30)])
        a, _ = split(raw)
        b, _ = split(raw)
        self.assertEqual(a, b)
        self.assertTrue(any(c["overlap_char_count"] for c in a))
        self.assertTrue(all(c["token_count"] <= 100 and c["overlap_token_count"] <= 15 for c in a))
        self.assertTrue(all(c["overlap_char_count"] == 0 for c in a if c["section_char_start"] == 0))

    def test_title_budget_exhaustion(self):
        with self.assertRaisesRegex(ValueError, "prefix"):
            split(middle([node("doc_title", "长标题" * 100, level=1), node("text", "正文")]))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input, self.output = self.root / "raw", self.root / "out"
        self.input.mkdir()
        self.path = self.input / "sample.pdf"
        self.path.write_bytes(b"fake input")

    def test_complete_pipeline_replay_and_benchmark_evidence(self):
        raw = middle([node("doc_title", "样例文档", level=1), node("header", "页眉"),
                      node("paragraph_title", "第一章 总则", level=2), node("text", "正文内容。" * 120),
                      node("table", [node("table_body", '<table><tr><th>标题</th></tr><tr><td>表格内容</td></tr></table>')])])
        with patch("rag.chunk.parse", side_effect=fake_parse(raw)):
            self.assertEqual(run(self.input, self.output), 0)
        result = verify(self.output)
        self.assertTrue(result["batch_success"])
        corpus = Corpus(self.output)
        chunk = next(iter(corpus.chunks.values()))
        span = chunk["sources"][0]
        quote = corpus.blocks[(chunk["doc_id"], span["block_id"])]["text"][:4]
        corpus.validate_evidence(corpus.evidence(chunk["doc_id"], span["block_id"], quote, chunk["chunk_id"]))
        self.assertEqual(len(corpus.chunks), result["retrieval_eligible_chunks"])

    def test_mixed_generation_is_detected(self):
        with patch("rag.chunk.parse", side_effect=fake_parse(middle([node("text", "正文内容")]))):
            run(self.input, self.output)
        with (self.output / "chunks.jsonl").open("a", encoding="utf-8") as stream:
            stream.write("{}\n")
        with self.assertRaisesRegex(AssertionError, "modified"):
            verify(self.output)

    def test_corrupted_pdf_oom_empty_and_continue(self):
        for name in ("broken.pdf", "oom.pdf", "empty.pdf"):
            (self.input / name).write_bytes(b"fake")
        good = fake_parse(middle([node("text", "有效正文。")]))
        empty = fake_parse(middle([node("header", "页眉")]))
        def parser(path, **kwargs):
            if path.stem == "broken":
                raise ValueError("damaged PDF")
            if path.stem == "oom":
                raise RuntimeError("CUDA out of memory")
            return (empty if path.stem == "empty" else good)(path, **kwargs)
        with patch("rag.chunk.parse", side_effect=parser):
            self.assertEqual(run(self.input, self.output), 1)
        docs = [json.loads(x) for x in (self.output / "documents.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([d["status"] for d in docs], ["failed", "empty", "failed", "ok"])
        self.assertFalse(verify(self.output)["batch_success"])

    def test_interrupted_run_can_restart(self):
        with patch("rag.chunk.parse", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run(self.input, self.output)
        with patch("rag.chunk.parse", side_effect=fake_parse(middle([node("text", "重跑正文")]))):
            self.assertEqual(run(self.input, self.output), 0)
        self.assertTrue(verify(self.output)["batch_success"])

    def test_missing_model_is_explicit(self):
        config = self.root / "mineru.yaml"
        config.write_text("model:\n  base_dir: " + (self.root / "missing").as_posix(), encoding="utf-8")
        with self.assertRaisesRegex(FileNotFoundError, "models missing"):
            configure(config)

    def test_missing_cache_and_changed_input_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            parse(self.path, artifact_dir=self.output, reuse=True)

    def saved_docx(self):
        path = self.input / "sample.docx"
        path.write_bytes(b"original native input")
        raw = middle([node("text", "保存的正文")], suffix="docx")
        def save(writer):
            writer.write_string("middle_json.json", json.dumps(raw))
            writer.write_string("markdown.md", "保存的正文")
            writer.write_string("structured_content.json", "{}")
        with patch("rag.parsers.configure"), patch("mineru.parser.parse") as sdk:
            sdk.return_value.save.side_effect = save
            parse(path, artifact_dir=self.output)
            self.assertFalse(sdk.call_args.kwargs["image_analysis"])
            self.assertEqual(sdk.call_args.kwargs["tier"], "flash")
        return path

    def test_reuse_rejects_changed_input_and_tampered_saved_parsing(self):
        path = self.saved_docx()
        blocks, _ = parse(path, artifact_dir=self.output, reuse=True)
        self.assertEqual(blocks[0]["text"], "保存的正文")
        path.write_bytes(b"different input")
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            parse(path, artifact_dir=self.output, reuse=True)
        path.write_bytes(b"original native input")
        (self.output / "middle_json.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            parse(path, artifact_dir=self.output, reuse=True)

    def test_interrupted_parser_invalidates_old_cache(self):
        path = self.saved_docx()
        with patch("rag.parsers.configure"), patch("mineru.parser.parse") as sdk:
            sdk.return_value.save.side_effect = KeyboardInterrupt
            with self.assertRaises(KeyboardInterrupt):
                parse(path, artifact_dir=self.output)
        with self.assertRaisesRegex(ValueError, "incomplete parsing"):
            parse(path, artifact_dir=self.output, reuse=True)

    def test_broken_pdf_pages_are_not_blank_successes(self):
        with patch("docvortex.document.pdf.PDFDocument") as pdf, \
             patch("docvortex.document.pdf.pdfium.get_loadable_pdfium_page_indices", return_value=([0, 2], [1])):
            pdf.return_value.__enter__.return_value.page_count = 3
            with self.assertRaisesRegex(ValueError, "damaged PDF pages:.*2"):
                pdf_health(self.path, "1-3")

    def test_failed_corpus_is_not_used_for_benchmark(self):
        with patch("rag.chunk.parse", side_effect=RuntimeError("out of memory")):
            self.assertEqual(run(self.input, self.output), 1)
        with self.assertRaisesRegex(ValueError, "failed/empty"):
            Corpus(self.output)

    def test_excluded_auxiliary_is_not_benchmark_evidence(self):
        with patch("rag.chunk.parse", side_effect=fake_parse(middle([node("header", "页眉"), node("text", "正文内容")]))):
            run(self.input, self.output)
        corpus = Corpus(self.output)
        doc_id = next(iter(corpus.documents))
        with self.assertRaisesRegex(ValueError, "excluded"):
            corpus.evidence(doc_id, 1, "页眉")

    def test_bge_counts_actual_tokens(self):
        count = TokenCounter()
        raw = middle([node("text", "数据安全管理。" * 180)])
        _, doc = split(raw)
        _, sections, _ = build_sections(copy.deepcopy(doc["blocks"]), "样例文档")
        chunks = chunk_document(doc, sections, count)
        self.assertTrue(all(count(c["text"]) == c["token_count"] <= 400 for c in chunks))


if __name__ == "__main__":
    unittest.main()
