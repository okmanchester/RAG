"""Frozen, local BGE table retrieval comparison; no answer-model requests."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audit_tables import CASES
from rag.benchmark.core import read_jsonl, write_json, write_jsonl, file_hash, digest
from rag.benchmark.first_run import QUERY_INSTRUCTION
from rag.splitter import DEFAULT_MODEL
from rag.tables import columns, compact


def corpus(root, target):
    documents, chunks = [], []
    for case, _, _ in CASES:
        documents.extend(read_jsonl(root / case / "documents.jsonl"))
        chunks.extend(read_jsonl(root / case / "chunks.jsonl"))
    target.mkdir(parents=True, exist_ok=True)
    write_jsonl(target / "documents.jsonl", documents)
    write_jsonl(target / "chunks.jsonl", chunks)
    return documents, sorted((c for c in chunks if c["retrieval_enabled"]), key=lambda c: c["chunk_id"])


def references(rules, specifications):
    result = []
    for spec in specifications:
        doc = read_jsonl(rules / spec["case"] / "documents.jsonl")[0]
        matched = []
        for index, view in enumerate(doc["table_views"]):
            if "table_index" in spec and spec["table_index"] != index:
                continue
            for row in view["rows"]:
                cells = columns(row, view["anchors"])
                if all(compact(cells.get(int(col), {}).get("text", "")) == compact(value)
                       for col, value in spec["match"].items()):
                    matched.append((view, row, cells))
        if len(matched) != 1:
            raise ValueError(f"query {spec['id']} must select exactly one frozen logical record")
        view, row, cells = matched[0]
        headers = {h["column"]: h for h in view["headers"]}
        required = []
        for col in spec["columns"]:
            cell = cells[col]
            required.append({"column": col, "text": cell["text"], "header_path": headers[col]["path"],
                             "sources": cell["source_groups"][0] if cell["operation"] == "extend_parent" else cell["parts"]})
        result.append({**spec, "doc_id": doc["doc_id"], "table_id": view["table_id"],
                       "row_id": row["row_id"], "required_cells": required})
    return result


def source_refs(chunk):
    return [*chunk["sources"], *chunk.get("context_sources", []),
            *(s for m in chunk.get("derived_sources", []) for s in m["source_refs"])]


def covered(source, retrieved):
    chars = set()
    for chunk in retrieved:
        for actual in source_refs(chunk):
            if actual["block_id"] == source["block_id"]:
                chars.update(range(max(source["block_char_start"], actual["block_char_start"]),
                                   min(source["block_char_end"], actual["block_char_end"])))
    return chars == set(range(source["block_char_start"], source["block_char_end"]))


def complete_context(cell, candidates):
    # All values and their exact sources must coexist with visible column names
    # in at least one retrieved chunk. This also accepts the baseline's pipe
    # table representation when it actually contains the header and cell value.
    return any(compact(cell["text"]) in compact(c["text"])
               and all(compact(h) in compact(c["text"]) for h in cell["header_path"])
               and all(covered(source, [c]) for source in cell["sources"]) for c in candidates)


def evaluate(baseline, rules, output, llm=None):
    import numpy as np
    from sentence_transformers import SentenceTransformer
    spec_path = ROOT / "tests/fixtures/table_queries.json"
    specs = json.loads(spec_path.read_text(encoding="utf-8"))
    gold = references(rules, specs)
    for source in (baseline, rules, *([llm] if llm else [])):
        if output.resolve() == source.resolve() or output.resolve() in source.resolve().parents or source.resolve() in output.resolve().parents:
            raise ValueError("retrieval output must be isolated from input audits")
    if output.exists() and any(output.iterdir()):
        raise ValueError("retrieval output must be empty; choose a new directory")
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "questions.json", gold)
    encoder = SentenceTransformer(DEFAULT_MODEL, local_files_only=True)
    query_vectors = encoder.encode([QUERY_INSTRUCTION + q["query"] for q in gold], batch_size=16,
                                   normalize_embeddings=True, show_progress_bar=False)
    model = Path(DEFAULT_MODEL)
    model_identity = {"model_path": DEFAULT_MODEL, "config_sha256": file_hash(model / "config.json"),
                      "weights_sha256": file_hash(model / "pytorch_model.bin"), "query_instruction": QUERY_INSTRUCTION}
    runs = [("baseline", baseline), ("rules", rules)] + ([("llm", llm)] if llm else [])
    report = {"question_count": len(gold), "questions_sha256": file_hash(spec_path),
              "model": model_identity, "metrics": {}, "per_query": {}}
    for name, root in runs:
        target = output / name
        _, chunks = corpus(root, target)
        identity = {**model_identity, "chunks_sha256": file_hash(target / "chunks.jsonl"),
                    "documents_sha256": file_hash(target / "documents.jsonl"),
                    "retrieval_policy": "retrieval_enabled_only", "index_version": 1}
        vectors = encoder.encode([c["text"] for c in chunks], batch_size=16, normalize_embeddings=True,
                                 show_progress_bar=False).astype("float32")
        np.save(target / "embeddings.npy", vectors)
        write_json(target / "index_manifest.json", {**identity, "identity_sha256": digest(identity), "chunk_count": len(chunks)})
        results = []
        for qi, query in enumerate(gold):
            scores = vectors @ np.asarray(query_vectors[qi], dtype="float32")
            order = sorted(range(len(chunks)), key=lambda n: (-float(scores[n]), chunks[n]["chunk_id"]))[:10]
            entry = {"id": query["id"], "retrieved": [chunks[i]["chunk_id"] for i in order]}
            for k in (1, 5, 10):
                candidates = [chunks[i] for i in order[:k] if chunks[i]["doc_id"] == query["doc_id"]]
                entry[f"evidence_complete@{k}"] = all(covered(s, candidates) for cell in query["required_cells"] for s in cell["sources"])
                entry[f"column_context_complete@{k}"] = all(complete_context(cell, candidates) for cell in query["required_cells"])
            results.append(entry)
        report["per_query"][name] = results
        report["metrics"][name] = {metric: round(sum(r[metric] for r in results) / len(results), 4)
                                   for metric in results[0] if metric not in {"id", "retrieved"}}
    report["changes"] = {metric: {"improved": [a["id"] for a, b in zip(report["per_query"]["baseline"], report["per_query"]["rules"])
                                             if not a[metric] and b[metric]],
                                   "regressed": [a["id"] for a, b in zip(report["per_query"]["baseline"], report["per_query"]["rules"])
                                              if a[metric] and not b[metric]]}
                         for metric in report["metrics"]["baseline"]}
    report["limitations"] = ["15 manually selected dev questions, not overall corpus accuracy",
                              "No answer-model evaluation; column context completeness is a source-grounded structural proxy"]
    write_json(output / "retrieval_comparison.json", report)
    print(json.dumps({"question_count": len(gold), "metrics": report["metrics"], "changes": report["changes"]}, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=ROOT / "data/processed/table_audit_deepseek_20261009")
    parser.add_argument("--rules", type=Path, default=ROOT / "data/processed/table_repair_20261009")
    parser.add_argument("--llm", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "data/processed/table_retrieval_20261009")
    args = parser.parse_args()
    evaluate(args.baseline, args.rules, args.output, args.llm)
