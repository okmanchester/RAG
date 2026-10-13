"""Compare original audit with LLM audit and ablate flags on the same parsing.

No inference or network requests. Ablation separates flag effects from any
differences caused by repeating the local parser.
"""
from __future__ import annotations

import argparse
import copy
import html
import json
from pathlib import Path

from audit_tables import CASES, ROOT, rows_for, write_json


def render_tables(middle):
    from docvortex.schema import MiddleJson, TableBlock
    from docvortex.render._internal.common.planner import build_render_plan
    return [child.content for page in build_render_plan(MiddleJson.model_validate(middle))
            for planned in page if not planned.removed and isinstance(planned.block, TableBlock)
            for child in planned.block.content if child.type == "table_body"]


def middle_from(target):
    document = json.loads((target / "documents.jsonl").read_text(encoding="utf-8"))
    if document["status"] != "ok":
        raise ValueError(f"incomplete audit: {target}")
    return json.loads((target / document["artifacts"] / "middle_json.json").read_text(encoding="utf-8"))


def physical_bodies(middle):
    return [child["content"] for page in middle["pages"] for block in page["blocks"]
            if block["type"] == "table" for child in block["content"] if child["type"] == "table_body"]


def metrics(markups):
    result = []
    for markup in markups:
        rows = rows_for(markup)
        result.append({"row_count": len(rows), "column_count": max(
            (c["column"] + c["colspan"] - 1 for r in rows for c in r["cells"]), default=0), "rows": rows})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=ROOT / "data/processed/table_audit_20261009")
    parser.add_argument("--llm", type=Path, default=ROOT / "data/processed/table_audit_deepseek_20261009")
    args = parser.parse_args()
    comparisons = []
    for case, _, _ in CASES:
        before = middle_from(args.baseline / case)
        after = middle_from(args.llm / case)
        without_flags = copy.deepcopy(after)
        for page in without_flags["pages"]:
            for block in page["blocks"]:
                if block["type"] == "table":
                    block.pop("cell_merge", None)
        old_html = render_tables(before)
        new_html = render_tables(after)
        ablated_html = render_tables(without_flags)
        old_metrics, new_metrics, ablated_metrics = map(metrics, (old_html, new_html, ablated_html))
        requests = json.loads((args.llm / case / "llm_requests.json").read_text(encoding="utf-8"))
        item = {"case": case, "physical_html_identical_between_runs": physical_bodies(before) == physical_bodies(after),
                "current_chunks_byte_identical": (args.baseline / case / "chunks.jsonl").read_bytes() == (args.llm / case / "chunks.jsonl").read_bytes(),
                "native_tables_before": len(old_html), "native_tables_after": len(new_html),
                "row_counts_before": [t["row_count"] for t in old_metrics],
                "row_counts_after": [t["row_count"] for t in new_metrics],
                "row_counts_same_parse_without_flags": [t["row_count"] for t in ablated_metrics],
                "column_counts_before": [t["column_count"] for t in old_metrics],
                "column_counts_after": [t["column_count"] for t in new_metrics],
                "flags_changed_rendered_tables": new_metrics != ablated_metrics,
                "validated_llm_requests": sum(r["status"] == "ok" for r in requests),
                "failed_llm_requests": sum(r["status"] != "ok" for r in requests),
                "boundary_decisions": [{"page": page["page_idx"] + 1, "cell_merge": block.get("cell_merge")}
                    for page in after["pages"] for block in page["blocks"]
                    if block["type"] == "table" and block.get("continues_prev")],
                "changed_rows_against_same_parse": [
                    {"table": ti + 1, "row": ri + 1, "without_flags": a["text"], "with_flags": b["text"]}
                    for ti, (a_table, b_table) in enumerate(zip(ablated_metrics, new_metrics))
                    for ri, (a, b) in enumerate(zip(a_table["rows"], b_table["rows"])) if a != b]}
        comparisons.append(item)
        target = args.llm / case / "review"
        write_json(target / "same_parse_without_llm_flags.json", ablated_metrics)
        style = "<meta charset='utf-8'><style>body{font:14px sans-serif;margin:24px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #aaa;padding:5px;vertical-align:top}</style>"
        content = style + f"<h1>{html.escape(case)}: same parsing, flags ablation</h1>"
        content += "<h2>LLM flags applied</h2>" + "\n".join(new_html)
        content += "<h2>Same parsing, LLM flags removed</h2>" + "\n".join(ablated_html)
        (target / "llm_ablation.html").write_text(content, encoding="utf-8")
        print(json.dumps({k: v for k, v in item.items() if k != "changed_rows_against_same_parse"}, ensure_ascii=False))
    write_json(args.llm / "comparison.json", {"baseline": str(args.baseline.resolve()),
        "llm": str(args.llm.resolve()), "method": "Fresh-run comparison plus same-input native-renderer flag ablation",
        "cases": comparisons})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
