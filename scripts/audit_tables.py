"""Bounded local MinerU table audit using the selected project configuration.

Run with the rag environment Python from the repository root.
Native outputs and current chunks are saved together with page renders and
physical-cell inventories. --inspect-only refreshes inventories without inference.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = [
    ("monitoring", "工业互联网平台监测分析指南.pdf", "9-13"),
    ("provider", "工业互联网平台服务商评价方法.pdf", "13-14"),
    ("classification", "工业互联网平台解决方案分类方法.pdf", "11-13"),
    ("chip", "网络安全标准实践指南——人工智能加速芯片安全功能技术规范.pdf", "23-24"),
    ("labels", "网络安全标准实践指南——网络数据标签标识技术要求.pdf", "28-30"),
]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@contextmanager
def trace_llm_requests(target):
    """Observe the native validated request method without changing prompts/flags."""
    from unittest.mock import patch
    from mineru.backend.postprocess.llm_client import LLMAidedClient
    original = LLMAidedClient.request_validated_json
    records = []
    target.mkdir(parents=True, exist_ok=True)

    async def traced(client, **kwargs):
        started = time.monotonic()
        record = {"operation": kwargs["operation"], "model": client._config.model,
                  "base_url": client._config.base_url, "prompt": kwargs["prompt"],
                  "temperature": kwargs["temperature"]}
        try:
            result = await original(client, **kwargs)
            record["validated_flags"] = result
            record["status"] = "ok" if result is not None else "failed"
            return result
        except BaseException as exc:
            record.update(status="error", error_type=type(exc).__name__)
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 2)
            records.append(record)
            write_json(target / "llm_requests.json", records)

    # Only metadata, document boundary text and validated flags are saved.
    # The client configuration/API key and reasoning output are never serialized.
    with patch.object(LLMAidedClient, "request_validated_json", traced):
        yield records


def rows_for(markup):
    from rag.parsers import TableHTML
    parser = TableHTML()
    parser.feed(markup)
    parser.close()
    return [{"row": r, "text": text, "cells": cells}
            for r, text, cells in parser.positioned_rows()]


def inspect_case(target, source, pages):
    import pymupdf
    from docvortex.schema import MiddleJson, TableBlock
    from docvortex.render._internal.common.planner import build_render_plan
    docs = [json.loads(s) for s in (target / "documents.jsonl").read_text(encoding="utf-8").splitlines()]
    document = docs[0]
    if document["status"] != "ok":
        return {"status": document["status"], "error": document.get("error")}
    parsed = target / document["artifacts"]
    middle = json.loads((parsed / "middle_json.json").read_text(encoding="utf-8"))
    inventory, snippets = [], []
    review = target / "review"
    review.mkdir(exist_ok=True)
    with pymupdf.open(source) as pdf:
        for page in middle["pages"]:
            number = page["page_idx"] + 1
            pdf[number - 1].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5)).save(review / f"page_{number}.png")
            (review / f"page_{number}_text.txt").write_text(pdf[number - 1].get_text(), encoding="utf-8")
            for bi, block in enumerate(page["blocks"]):
                if block["type"] != "table":
                    continue
                bodies = [c for c in block["content"] if c["type"] == "table_body"]
                for body in bodies:
                    markup = body["content"]
                    rows = rows_for(markup)
                    item = {"page": number, "pointer": f"/pages/{middle['pages'].index(page)}/blocks/{bi}",
                            "continues_prev": block.get("continues_prev", False),
                            "cell_merge": block.get("cell_merge"),
                            "caption": [c["content"] for c in block["content"] if c["type"] == "table_caption"],
                            "row_count": len(rows),
                            "column_count": max((c["column"] + c["colspan"] - 1 for r in rows for c in r["cells"]), default=0),
                            "rows": rows}
                    inventory.append(item)
                    snippets.append(f"<h2>Middle JSON page {number}; continues_prev={item['continues_prev']}</h2>{markup}")
    native_markdown = (parsed / "markdown.md").read_text(encoding="utf-8")
    native_html_tables = re.findall(r"<table\b[^>]*>.*?</table>", native_markdown, re.S | re.I)
    # Simple tables are rendered as pipe Markdown, not HTML. Read the exact
    # native renderer's logical table plan to inventory both formats. This is
    # an inspection of the installed, version-pinned docvortex implementation.
    native_pipe_tables = re.findall(r"^\|[^\n]*\|\n\|\s*:?-{3,}.*\|\n(?:\|[^\n]*\|\n?)+", native_markdown, re.M)
    native_tables = []
    for page_plan in build_render_plan(MiddleJson.model_validate(middle)):
        for planned in page_plan:
            if planned.removed or not isinstance(planned.block, TableBlock):
                continue
            native_tables.extend(c.content for c in planned.block.content if c.type == "table_body")
    if len(native_tables) != len(native_html_tables) + len(native_pipe_tables):
        raise AssertionError("native render plan and actual Markdown table counts differ")
    rendered = [{"table": i + 1, "row_count": len(rows_for(t)), "rows": rows_for(t)} for i, t in enumerate(native_tables)]
    chunks = [json.loads(s) for s in (target / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    table_chunks = [c for c in chunks if c["structure_type"] == "table"]
    summary = {"status": "ok", "source": str(source), "pages": pages,
               "physical_table_count": len(inventory), "native_markdown_table_count": len(native_tables),
               "native_markdown_formats": {"html": len(native_html_tables), "pipe": len(native_pipe_tables)},
               "physical_tables": [{k: v for k, v in t.items() if k != "rows"} for t in inventory],
               "native_markdown_row_counts": [t["row_count"] for t in rendered],
               "table_chunk_count": len(table_chunks),
               "table_chunks_with_repeated_context": sum(bool(c["context_sources"]) for c in table_chunks),
               "cross_page_table_chunks": sum(len({s["page"] for s in c["sources"]}) > 1 for c in table_chunks),
               "table_header_rows_detected": sum(b.get("is_table_header", False) for b in document["blocks"]),
               "table_chunk_groups": sorted({(c["section_id"], c["group_index"]) for c in table_chunks}),
               "table_chunk_pages": [{"chunk": c["chunk_index"], "pages": sorted({s["page"] for s in c["sources"]}),
                                       "context_sources": c["context_sources"], "text": c["text"]} for c in table_chunks]}
    write_json(review / "physical_tables.json", inventory)
    write_json(review / "native_merged_tables.json", rendered)
    write_json(review / "summary.json", summary)
    style = "<meta charset='utf-8'><style>body{font:14px sans-serif;max-width:1600px;margin:24px}table{border-collapse:collapse;width:100%;margin-bottom:32px}td,th{border:1px solid #888;padding:5px;vertical-align:top}h2{margin-top:48px}</style>"
    content = style + f"<h1>{html.escape(source.name)} / PDF pages {pages}</h1>"
    content += "<h2>Native renderer logical tables (including tables emitted as pipe Markdown)</h2>" + "\n".join(native_tables)
    content += "<h2>Physical per-page HTML retained in Middle JSON</h2>" + "\n".join(snippets)
    (review / "tables.html").write_text(content, encoding="utf-8")
    return {k: v for k, v in summary.items() if k != "table_chunk_pages"}


def quality_checks(output, results):
    """Small independent expectations established by original-page visual review.

    Failed quality checks are findings, separate from execution/verification.
    This bounded suite is not an OCR accuracy score or an exhaustive cell check.
    """
    checks = []

    def add(name, passed, details):
        checks.append({"check": name, "passed": bool(passed), "details": details})

    def read(case, name):
        return json.loads((output / case / "review" / name).read_text(encoding="utf-8"))

    add("all_selected_pages_parsed_and_outputs_verified", all(
        json.loads((output / c["case"] / "verification.json").read_text(encoding="utf-8"))["batch_success"]
        for c in results), "Saved-output checks do not assert semantic extraction accuracy")
    expected = {"monitoring": (5, 1), "provider": (2, 1), "classification": (4, 3),
                "chip": (2, 1), "labels": (4, 2)}
    ranges = {name: list(range(int(pages.split("-")[0]), int(pages.split("-")[1]) + 1))
              for name, _, pages in CASES}
    for result in results:
        case = result["case"]
        physical, logical = expected[case]
        add(case + "_native_table_count", (result["physical_table_count"], result["native_markdown_table_count"]) == (physical, logical),
            {"expected_physical": physical, "expected_logical": logical, "actual_logical": result["native_markdown_table_count"]})
        document = json.loads((output / case / "documents.jsonl").read_text(encoding="utf-8"))
        add(case + "_selected_pages", document["parsed_page_numbers"] == ranges[case], document["parsed_page_numbers"])
    physical = read("classification", "physical_tables.json")
    add("classification_distinct_tables_not_merged", physical[1]["continues_prev"] and not physical[2]["continues_prev"] and not physical[3]["continues_prev"],
        "Table 1 joins pages 11-12; Tables 2 and 3 remain distinct")
    physical = read("labels", "physical_tables.json")
    add("labels_distinct_templates_not_merged", physical[1]["continues_prev"] and not physical[2]["continues_prev"] and physical[3]["continues_prev"],
        "B.1 joins 28-29; B.2 joins 29-30; the templates remain distinct")
    rows = read("labels", "native_merged_tables.json")[1]["rows"]
    stamp = next(r for r in rows if any(c["text"] == "时间戳" for c in r["cells"]))
    actual = next(c["text"] for c in stamp["cells"] if c["column"] == 3)
    expected_stamp = "2025101508301500000086400"
    add("labels_timestamp_cross_page_cell_complete", actual == expected_stamp,
        {"expected": expected_stamp, "actual": actual})
    rows = read("monitoring", "native_merged_tables.json")[0]["rows"]
    checks.append({"check": "monitoring_page12_source_structure_review", "passed": None,
                   "status": "pending_review", "details": {
                       "page": 12, "note": "Original page has a row divider and different units; do not force a merge"}})
    groups = sum(len(c["table_chunk_groups"]) for c in results)
    add("current_system_consumes_native_continuation", groups == 8,
        {"expected_logical_groups": 8, "actual_groups": groups})
    headers = sum(c["table_header_rows_detected"] for c in results)
    add("current_system_recognizes_all_sample_table_headers", headers == 15,
        {"expected": 15, "actual": headers, "note": "Two continuation fragments have no repeated header"})
    write_json(output / "quality_checks.json", {
        "basis": "Independent expectations from visual review of all selected original PDF pages",
        "checks": checks, "passing": sum(c["passed"] is True for c in checks),
        "failing": sum(c["passed"] is False for c in checks),
        "pending_review": sum(c["passed"] is None for c in checks), "failures_are_findings_not_execution_errors": True})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/processed/table_audit_20261009")
    parser.add_argument("--inspect-only", action="store_true")
    parser.add_argument("--case", choices=[c[0] for c in CASES], action="append")
    parser.add_argument("--trace-llm", action="store_true", help="Record native boundary prompts and validated flags")
    parser.add_argument("--mineru-config", type=Path, default=ROOT / "config/mineru.yaml")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    from rag.chunk import run, versions
    from rag.verify import verify
    results = []
    for name, filename, pages in CASES:
        if args.case and name not in args.case:
            continue
        target = args.output / name
        source = ROOT / "data/raw/technical" / filename
        started = time.monotonic()
        print(f"AUDIT START {name}: {filename} / {pages}", flush=True)
        try:
            if not args.inspect_only:
                if args.trace_llm:
                    # Configure before importing MinerU so its initial validation
                    # receives the correct endpoint and environment credential.
                    from rag.parsers import configure
                    configure(args.mineru_config)
                    with trace_llm_requests(target):
                        code = run(ROOT / "data/raw", target, include=[f"technical/{filename}"],
                                   page_range=pages, config_path=args.mineru_config)
                else:
                    code = run(ROOT / "data/raw", target, include=[f"technical/{filename}"],
                               page_range=pages, config_path=args.mineru_config)
                if code:
                    raise RuntimeError(f"parse/chunk failed; see {target / 'documents.jsonl'}")
                verification = verify(target)
                if not verification["batch_success"]:
                    raise RuntimeError("saved-output verification failed")
            result = inspect_case(target, source, pages)
        except Exception as exc:
            result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        result.update(case=name, elapsed_seconds=round(time.monotonic() - started, 2))
        results.append(result)
        write_json(args.output / "audit_summary.json", {"versions": versions(),
                   "operation": "inspection_only" if args.inspect_only else "fresh_inference_and_inspection", "cases": results})
        print(json.dumps(result, ensure_ascii=False), flush=True)
    if len(results) == len(CASES) and all(r["status"] == "ok" for r in results):
        quality_checks(args.output, results)
    return int(any(r["status"] != "ok" for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
