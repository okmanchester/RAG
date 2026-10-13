import copy
import json
import unittest
import tempfile
import os
from pathlib import Path
from unittest.mock import patch

from rag.parsers import adapt_middle
from rag.splitter import chunk_document, TokenCounter
from rag.structure import build_sections
from rag.tables import build_table_views, validate_views, validate_decision, rule_decision
from rag.table_llm import collect_decisions, options
from rag.chunk import run
from rag.verify import verify
from test_chunking import middle, node, fake_parse


FIXTURES = Path(__file__).parent / "fixtures/table_audit_middle.json"


def prepare(raw, geometry=None):
    blocks, meta = adapt_middle(raw, ".pdf")
    title, sections, _ = build_sections(blocks, "样例文档")
    document = {"doc_id": "test", "title": title, "source_path": "sample.pdf", "category": "technical",
                "blocks": blocks, **meta, "table_geometry": geometry or []}
    views = build_table_views(document, sections)
    document["table_views"] = views
    return document, sections, views


def table(html, caption="表1 示例", **kwargs):
    return node("table", [node("table_caption", caption), node("table_body", html)], **kwargs)


def assert_mappings(test, document, chunks):
    blocks = {b["block_id"]: b for b in document["blocks"]}
    for chunk in chunks:
        for mapping in chunk.get("derived_sources", []):
            actual = chunk["text"][mapping["text_char_start"]:mapping["text_char_end"]]
            copied = "".join(blocks[s["block_id"]]["text"][s["block_char_start"]:s["block_char_end"]]
                             for s in mapping["source_refs"])
            test.assertEqual(actual, copied)
            test.assertEqual(actual, mapping["text"])
        for source in chunk["context_sources"]:
            test.assertEqual(chunk["text"][source["text_char_start"]:source["text_char_end"]],
                             blocks[source["block_id"]]["text"][source["block_char_start"]:source["block_char_end"]])


class FrozenTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads(FIXTURES.read_text(encoding="utf-8"))
        cls.prepared = {case: prepare(data["middle"]) for case, data in cls.raw.items()}

    def test_native_fragments_become_eight_tables_and_fifteen_headers(self):
        expected = {"monitoring": (5, 1), "provider": (2, 1), "classification": (4, 3),
                    "chip": (2, 1), "labels": (4, 2)}
        headers = 0
        for case, (physical, logical) in expected.items():
            document, _, views = self.prepared[case]
            self.assertEqual(len(views), logical, case)
            self.assertEqual(sum(len(v["fragments"]) for v in views), physical, case)
            validate_views(document, views)
            headers += sum(bool(b.get("is_table_header")) for b in document["blocks"])
        self.assertEqual(headers, 15)

    def test_timestamp_and_explanation_have_two_page_sources(self):
        doc, _, views = self.prepared["labels"]
        view = views[1]
        self.assertEqual(view["logical_row_count"], 11)
        row = next(r for r in view["rows"] if any(view["anchors"][a]["text"] == "时间戳" for a in r["cell_ids"]))
        cells = {view["anchors"][a]["column"]: view["anchors"][a] for a in row["cell_ids"]}
        self.assertEqual(cells[3]["text"], "2025101508301500000086400")
        self.assertIn("8点30分15秒", cells[4]["text"])
        self.assertEqual({s["page"] for s in cells[3]["parts"]}, {29, 30})
        validate_views(doc, views)

    def test_parent_anchors_extend_without_repeated_text(self):
        for case, label in [("provider", "服务成效"), ("chip", "固件安全"), ("classification", "服务行业")]:
            _, _, views = self.prepared[case]
            cells = [cell for view in views for cell in view["anchors"].values()
                     if cell["text"] == label and cell["operation"] == "extend_parent"]
            self.assertTrue(cells, case)
            self.assertGreater(len({s["page"] for s in cells[0]["parts"]}), 1)
            self.assertGreater(cells[0]["logical_rowspan"], 1)

    def test_source_anomalies_are_queued_not_rewritten(self):
        _, _, views = self.prepared["monitoring"]
        self.assertEqual(views[0]["logical_row_count"], 35)
        self.assertIn("table_row_structure_requires_review", views[0]["warnings"])
        values = [c["text"] for c in views[0]["anchors"].values()]
        self.assertIn("不同行业分类公有云工业", values)
        self.assertIn("APP日均调用次数", values)
        _, _, labels = self.prepared["labels"]
        self.assertIn("table_text_order_requires_review", labels[1]["warnings"])

    def test_real_bge_budget_mapping_coverage_and_replay(self):
        count = TokenCounter()
        for case, prepared in self.prepared.items():
            document, sections, views = copy.deepcopy(prepared)
            chunks = chunk_document(document, sections, count)
            self.assertEqual(chunks, chunk_document(document, sections, count), case)
            self.assertTrue(all(count(c["text"]) == c["token_count"] <= 400 for c in chunks), case)
            tables = [c for c in chunks if c["structure_type"] == "table"]
            self.assertEqual(len({c["table_id"] for c in tables}), len(views), case)
            assert_mappings(self, document, tables)
            coverage = {}
            for chunk in chunks:
                for source in chunk["sources"]:
                    coverage.setdefault(source["block_id"], set()).update(range(source["block_char_start"], source["block_char_end"]))
            for block in document["blocks"]:
                if block["role"] not in {"document_title", "heading", "excluded"}:
                    self.assertEqual(coverage.get(block["block_id"], set()), set(range(len(block["text"]))), case)
            if case == "labels":
                self.assertTrue(any("2025101508301500000086400" in c["text"] for c in tables))


class TableBoundaryTests(unittest.TestCase):
    def test_same_width_independent_tables_and_different_captions(self):
        html = '<table><tr><th>名称</th><th>说明</th></tr><tr><td>A</td><td>B</td></tr></table>'
        for marker, caption in [(False, "表1 示例"), (True, "表2 示例")]:
            _, _, views = prepare(middle([table(html)], [table(html, caption, continues_prev=marker)]))
            self.assertEqual(len(views), 2)

    def test_section_and_missing_page_are_hard_boundaries(self):
        html = '<table><tr><th>名称</th><th>说明</th></tr><tr><td>A</td><td>B</td></tr></table>'
        raw = middle([table(html)], [node("paragraph_title", "2 新章节", level=1), table(html, continues_prev=True)])
        self.assertEqual(len(prepare(raw)[2]), 2)
        raw = middle([table(html)], [table(html, continues_prev=True)])
        raw["pages"][1]["page_idx"] = 3
        self.assertEqual(len(prepare(raw)[2]), 2)
        raw = middle([table(html), node("text", "表间说明")], [table(html, continues_prev=True)])
        self.assertEqual(len(prepare(raw)[2]), 2)

    def test_blank_key_is_not_sufficient_to_merge_records(self):
        html1 = '<table><tr><td>字段</td><td>说明</td></tr><tr><td>A</td><td>上行说明。</td></tr></table>'
        html2 = '<table><tr><td></td><td>独立的未命名记录。</td></tr></table>'
        _, _, views = prepare(middle([table(html1)], [table(html2, continues_prev=True)]))
        self.assertEqual(len(views[0]["rows"]), 2)
        self.assertEqual(views[0]["operations"][0]["decision"]["kind"], "uncertain")

    def test_multi_level_headers_and_duplicate_columns(self):
        html = ('<table><tr><th rowspan="2">名称</th><th colspan="2">一级指标</th>'
                '<th colspan="2">二级指标</th></tr><tr><th>单位</th><th>说明</th><th>单位</th><th>说明</th></tr>'
                '<tr><td>A</td><td>个</td><td>一级说明</td><td>次</td><td>二级说明</td></tr></table>')
        doc, sections, views = prepare(middle([table(html)]))
        labels = [h["label"] for h in views[0]["headers"]]
        self.assertEqual(len(set(labels)), 5)
        self.assertEqual(labels[1], "一级指标／单位")
        self.assertEqual(labels[3], "二级指标／单位")
        chunks = chunk_document(doc, sections, len, maximum=200)
        self.assertIn("二级指标／单位：次", chunks[0]["text"])
        assert_mappings(self, doc, chunks)

    def test_long_cell_split_keeps_column_and_parent_sources(self):
        html = '<table><tr><th>名称</th><th>说明</th></tr><tr><td rowspan="2">父项</td><td>' + "长说明。" * 100 + '</td></tr><tr><td>子项</td></tr></table>'
        doc, sections, _ = prepare(middle([table(html)]))
        chunks = chunk_document(doc, sections, len, maximum=100, overlap=15)
        self.assertTrue(all(c["token_count"] <= 100 and c["overlap_char_count"] == 0 for c in chunks))
        data = [c for c in chunks if "长说明" in c["text"]]
        self.assertGreater(len(data), 1)
        self.assertTrue(all("说明：" in c["text"] and "名称：父项" in c["text"] for c in data))
        self.assertEqual("".join(c["body"] for c in chunks).count("父项"), 1)
        assert_mappings(self, doc, chunks)

    def test_geometry_reorders_exact_source_parts_only(self):
        html = '<table><tr><th>名称</th><th>说明</th></tr><tr><td>A</td><td>乙甲</td></tr></table>'
        raw = middle([table(html)])
        doc, _, _ = prepare(raw)
        block = next(b for b in doc["blocks"] if b.get("cells") and b["cells"][-1]["text"] == "乙甲")
        cell = block["cells"][-1]
        start = cell["char_start"]
        geo = [{"block_id": block["block_id"], "column": 2, "cell_bbox": [0, 0, 1, 1], "segments": [
            {"block_char_start": start, "block_char_end": start + 1, "bbox": [.5, .1, .6, .2]},
            {"block_char_start": start + 1, "block_char_end": start + 2, "bbox": [.1, .1, .2, .2]}]}]
        doc, sections, views = prepare(raw, geo)
        self.assertIn("甲乙", [c["text"] for c in views[0]["anchors"].values()])
        validate_views(doc, views)
        chunks = chunk_document(doc, sections, len, maximum=100)
        assert_mappings(self, doc, chunks)
        geo[0]["segments"][1]["block_char_start"] = start
        _, _, views = prepare(raw, geo)
        self.assertIn("乙甲", [c["text"] for c in views[0]["anchors"].values()])
        self.assertIn("table_cell_geometry_requires_review", views[0]["warnings"])

    def test_reordered_parent_extends_with_all_source_groups(self):
        header = '<tr><th>名称</th><th>说明</th></tr>'
        left = '<table>' + header + '<tr><td rowspan="2">乙甲</td><td>X</td></tr><tr><td>Y</td></tr></table>'
        right = '<table>' + header + '<tr><td rowspan="2">甲乙</td><td>Z</td></tr><tr><td>W</td></tr></table>'
        raw = middle([table(left)], [table(right, continues_prev=True)])
        original, _, _ = prepare(raw)
        block = next(b for b in original["blocks"] if b.get("cells") and b["cells"][0]["text"] == "乙甲")
        geometry = [{"block_id": block["block_id"], "column": 1, "cell_bbox": [0, 0, 1, 1], "segments": [
            {"block_char_start": 0, "block_char_end": 1, "bbox": [.5, .1, .6, .2]},
            {"block_char_start": 1, "block_char_end": 2, "bbox": [.1, .1, .2, .2]}]}]
        doc, sections, views = prepare(raw, geometry)
        parent = next(c for c in views[0]["anchors"].values() if c["operation"] == "extend_parent")
        self.assertEqual(parent["text"], "甲乙")
        self.assertEqual(len(parent["source_groups"][0]), 2)
        self.assertEqual(len(parent["source_groups"]), 2)
        self.assertEqual(parent["logical_rowspan"], 4)
        validate_views(doc, views)
        assert_mappings(self, doc, chunk_document(doc, sections, len, maximum=100))


class BoundaryLLMTests(unittest.TestCase):
    def setUp(self):
        raw = json.loads(FIXTURES.read_text(encoding="utf-8"))["labels"]["middle"]
        self.doc, self.sections, self.views = prepare(raw)
        self.config = options({"table_processing": {"llm_enabled": True}})

    def test_context_contains_headers_parent_anchors_and_neighbor_records(self):
        context = self.views[1]["operations"][0]["input"]
        self.assertEqual(len(context["previous_records"]), 2)
        self.assertEqual(len(context["current_records"]), 2)
        self.assertEqual(context["headers"][2]["label"], "示例数据")
        self.assertEqual(rule_decision(context)["concat_columns"], [3, 4])

    def test_failure_invalid_columns_and_conflict_preserve_fragments(self):
        for request in [lambda *_: (_ for _ in ()).throw(TimeoutError("sensitive-request-details")),
                        lambda *_: {"kind": "continue_row", "concat_columns": [1, 2], "parent_columns": []},
                        lambda *_: {"kind": "new_record", "concat_columns": [], "parent_columns": []}]:
            decisions = collect_decisions(self.views, self.config, request)
            self.assertNotIn("sensitive-request-details", json.dumps(decisions))
            views = build_table_views(self.doc, self.sections, decisions)
            self.assertEqual(views[1]["logical_row_count"], 12)
            self.assertIn("table_boundary_requires_review", views[1]["warnings"])

    def test_valid_decisions_replay_without_request(self):
        decisions = collect_decisions(self.views, self.config, lambda ctx, _: rule_decision(ctx))
        views = build_table_views(self.doc, self.sections, decisions)
        self.assertEqual(views[1]["logical_row_count"], 11)
        self.assertEqual(views, build_table_views(self.doc, self.sections, decisions))
        self.assertTrue(all(o["status"] == "llm_validated" for v in views for o in v["operations"]))

    def test_partially_merged_record_is_rejected(self):
        context = self.views[1]["operations"][0]["input"]
        with self.assertRaisesRegex(ValueError, "unmerged nonempty"):
            validate_decision({"kind": "continue_row", "concat_columns": [3], "parent_columns": []}, context)

    def test_stale_decisions_are_rejected_instead_of_ignored(self):
        decisions = collect_decisions(self.views, self.config, lambda ctx, _: rule_decision(ctx))
        key = next(iter(decisions))
        decisions["different_boundary"] = decisions.pop(key)
        with self.assertRaisesRegex(ValueError, "actual boundaries"):
            build_table_views(self.doc, self.sections, decisions)

    def test_saved_pipeline_decisions_replay_without_key_or_network(self):
        import yaml
        from rag.parsers import DEFAULT_CONFIG
        raw = copy.deepcopy(json.loads(FIXTURES.read_text(encoding="utf-8"))["labels"]["middle"])
        def remove_assets(node):
            if isinstance(node, dict):
                node.pop("image_path", None)
                for value in node.values():
                    remove_assets(value)
            elif isinstance(node, list):
                for value in node:
                    remove_assets(value)
        remove_assets(raw)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs, output = root / "raw", root / "out"
            inputs.mkdir()
            (inputs / "sample.pdf").write_bytes(b"test input")
            settings = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
            settings["table_processing"]["llm_enabled"] = True
            config = root / "config.yaml"
            config.write_text(yaml.safe_dump(settings), encoding="utf-8")
            def local_decisions(views, config):
                return collect_decisions(views, config, lambda ctx, _: rule_decision(ctx))
            with patch("rag.chunk.parse", side_effect=fake_parse(raw)), \
                 patch("rag.chunk.collect_decisions", side_effect=local_decisions):
                self.assertEqual(run(inputs, output, config_path=config), 0)
            original_chunks = (output / "chunks.jsonl").read_bytes()
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}), \
                 patch("rag.chunk.parse", side_effect=fake_parse(raw)), \
                 patch("rag.chunk.collect_decisions", side_effect=AssertionError("unexpected request")):
                self.assertEqual(run(inputs, output, config_path=config, reuse_parsed=True), 0)
                self.assertTrue(verify(output)["batch_success"])
            self.assertEqual((output / "chunks.jsonl").read_bytes(), original_chunks)
            document = json.loads((output / "documents.jsonl").read_text(encoding="utf-8"))
            artifact = output / document["table_decision_artifact"]
            artifact.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "decision checksum"):
                verify(output)


if __name__ == "__main__":
    unittest.main()
