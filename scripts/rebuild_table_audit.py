"""Rebuild the frozen five-case audit from verified native caches, without inference.

Use --llm for enriched-context A/B. Default writes rule-only derived output.
Old audits are never overwritten. No API credential is copied into artifacts.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audit_tables import CASES
from rag.chunk import run
from rag.parsers import DEFAULT_CONFIG, load_settings
from rag.verify import verify


def quality(documents):
    views = {case: doc["table_views"] for case, doc in documents.items()}
    labels = views["labels"][1]
    timestamp = next(row for row in labels["rows"] if any(labels["anchors"][a]["text"] == "时间戳"
                                                         for a in row["cell_ids"]))
    actual = next(labels["anchors"][a]["text"] for a in timestamp["cell_ids"]
                  if labels["anchors"][a]["column"] == 3)
    headers = sum(bool(b.get("is_table_header")) for doc in documents.values() for b in doc["blocks"])
    checks = [
        {"check": "logical_table_counts", "passed": [len(views[c]) for c, _, _ in CASES] == [1, 1, 3, 1, 2]},
        {"check": "all_visible_physical_headers", "passed": headers == 15, "actual": headers, "expected": 15},
        {"check": "timestamp_complete", "passed": actual == "2025101508301500000086400", "actual": actual},
        {"check": "timestamp_record_joined", "passed": labels["logical_row_count"] == 11,
         "actual": labels["logical_row_count"], "expected": 11},
        {"check": "monitoring_structure_kept_for_review", "passed": any(
            "table_row_structure_requires_review" in v["warnings"] for v in views["monitoring"])},
        {"check": "labels_text_order_queued_for_review", "passed": any(
            "table_text_order_requires_review" in v["warnings"] for v in views["labels"])},
    ]
    return {"basis": "Frozen source expectations; source anomaly is not forcibly merged",
            "checks": checks, "passing": sum(c["passed"] for c in checks),
            "failing": sum(not c["passed"] for c in checks), "pending_review": [
                {"case": "monitoring", "page": 12, "reason": "Source has a row divider and different units"},
                {"case": "labels", "page": 29, "reason": "No cell-level text geometry in saved native model output"}]}


def rebuild(source, output, llm=False, refresh=False):
    import yaml
    source, output = Path(source).resolve(), Path(output).resolve()
    if output == source or source in output.parents or output in source.parents:
        raise ValueError("output must be isolated from the source audit")
    if output.exists() and any(output.iterdir()) and not refresh:
        raise ValueError("output must be empty; choose a new directory")
    if refresh:
        summary_path = output / "audit_summary.json"
        if not summary_path.is_file() or json.loads(summary_path.read_text(encoding="utf-8")).get("operation") != "saved_native_rebuild":
            raise ValueError("--refresh requires a previous derived rebuild")
    output.mkdir(parents=True, exist_ok=True)
    settings = load_settings(DEFAULT_CONFIG)
    settings.setdefault("table_processing", {})["llm_enabled"] = llm
    config = output / "postprocess_config.yaml"
    config.write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
    documents, summary = {}, []
    for case, filename, pages in CASES:
        original = json.loads((source / case / "documents.jsonl").read_text(encoding="utf-8"))
        target = output / case
        native = (source / case / original["artifacts"]).resolve()
        if source not in native.parents:
            raise ValueError("unsafe source artifact path")
        artifact = target / original["artifacts"]
        if not refresh:
            shutil.copytree(native, artifact)
        code = run(ROOT / "data/raw", target, reuse_parsed=True, refresh_table_decisions=llm,
                   include=[f"technical/{filename}"], page_range=pages, config_path=config)
        if code:
            raise RuntimeError(f"derived rebuild failed: {case}; inspect documents.jsonl")
        verified = verify(target)
        document = json.loads((target / "documents.jsonl").read_text(encoding="utf-8"))
        documents[case] = document
        views = document["table_views"]
        summary.append({"case": case, "physical_tables": sum(len(v["fragments"]) for v in views),
                        "logical_tables": len(views), "logical_row_counts": [v["logical_row_count"] for v in views],
                        "chunks": document["chunk_count"], "verification": verified["status"],
                        "llm_requests": len(document["table_decisions"]),
                        "accepted_llm_decisions": sum(o["status"] == "llm_validated" for v in views for o in v["operations"]),
                        "warnings": sorted({w for v in views for w in v["warnings"]})})
    result = {"operation": "saved_native_rebuild", "llm_enabled": llm, "cases": summary}
    (output / "audit_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    checks = quality(documents)
    (output / "quality_checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return int(checks["failing"] > 0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data/processed/table_audit_deepseek_20261009")
    parser.add_argument("--output", type=Path, default=ROOT / "data/processed/table_repair_20261009")
    parser.add_argument("--llm", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="Refresh only derived output of a previous rebuild")
    args = parser.parse_args()
    raise SystemExit(rebuild(args.source, args.output, args.llm, args.refresh))
