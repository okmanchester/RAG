"""Offline, identity-bound NumPy/ES retrieval comparison. No LLM or Langfuse calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag.benchmark.core import Corpus, file_hash, load_release, read_jsonl, write_json, write_jsonl
from rag.benchmark.evaluation import retrieval_metrics, summarize_scores
from rag.benchmark.first_run import DenseIndex
from rag.management.config import Settings
from rag.management.errors import ManagementError, public_error
from rag.search.config import SearchSettings
from rag.search.quality import default_mode_gate
from rag.search.service import SearchService
from rag.search.shared import SearchFilters, context, digest, query_embedding


def promote_default(managed, report, report_path):
    """Bind a decision to the observed generation AND current default under the index lock."""
    report_bytes = Path(report_path).read_bytes()
    if json.loads(report_bytes) != report:
        raise ManagementError("BENCHMARK_CORPUS_MISMATCH")
    identity = report["identity"]
    with managed.corpus.index_lock(identity["kb_id"]):
        current = managed.status(identity["kb_id"])
        mode = current["manifest"].get("default_mode", "dense") if current["manifest"] else None
        if (current["generation"] != identity["generation"] or current["sync_lag"] or
            current["selection_sha256"] != identity["selection_sha256"] or
            mode != identity["index_manifest"].get("default_mode", "dense") or
            any(current["manifest"].get(key) != identity["index_manifest"].get(key) for key in
                ("namespace", "model_sha256", "retrieval_sha256", "content_sha256", "records_sha256", "chunk_count"))):
            raise ManagementError("BENCHMARK_CORPUS_MISMATCH")
        gate = default_mode_gate(report["reports"], mode)
        if gate != report["gate"]:
            raise ManagementError("BENCHMARK_CORPUS_MISMATCH")
        if not gate["passed"]:
            return False
        managed.store.seal(current["index"], {**current["manifest"],
            "default_mode": gate["recommended_default"], "default_mode_selection_policy": gate["policy_version"],
            "default_mode_evaluation_sha256": hashlib.sha256(report_bytes).hexdigest()})
        return True


def evaluate(release, output, kb_id, engine, client, *, prefix="rag", allow_evolving=False, promote_best=False):
    release, output = Path(release).resolve(), Path(output).resolve()
    allowed = output.is_relative_to(ROOT / "tmp") or (
        output.is_relative_to(ROOT / "data/benchmark") and "full_runs" in output.relative_to(ROOT / "data/benchmark").parts)
    if not allowed or subprocess.run(["git", "check-ignore", "-q", str(output)], cwd=ROOT).returncode != 0:
        raise ManagementError("INVALID_BENCHMARK_OUTPUT")
    if output.exists():
        raise ManagementError("BENCHMARK_OUTPUT_EXISTS")
    load_release(release, allow_evolving=allow_evolving)
    corpus = Corpus(release)
    original = {p.relative_to(release).as_posix(): file_hash(p) for p in release.rglob("*")
                if p.is_file() and "full_runs" not in p.relative_to(release).parts}
    baseline = DenseIndex(release)
    managed = SearchService(engine, client, prefix=prefix)
    managed.encoder._model = baseline.encoder  # One shared local model; no second GPU allocation.
    selection = managed.corpus.selection(kb_id)
    status = managed.status(kb_id)
    if status["sync_lag"] or status["state"] != "ready":
        raise ManagementError("BENCHMARK_CORPUS_MISMATCH")
    rows = list(managed.corpus.iter_chunks(selection))
    legacy_ids = [row["legacy_chunk_id"] for row in rows]
    if (len(legacy_ids) != len(set(legacy_ids)) or set(legacy_ids) != set(corpus.chunks) or
        any(digest(row["payload_json"]) != digest(corpus.chunks[row["legacy_chunk_id"]]) or
            row["source_sha256"] != corpus.documents[row["legacy_doc_id"]]["sha256"] for row in rows)):
        raise ManagementError("BENCHMARK_CORPUS_MISMATCH")
    if status["manifest"]["model_sha256"] != digest(managed.encoder.identity):
        raise ManagementError("INDEX_MODEL_MISMATCH")
    items = read_jsonl(release / "gold.jsonl")
    records = {name: [] for name in ("numpy", "dense", "bm25", "hybrid")}
    timings = {name: [] for name in records}
    query_timings = []
    output.mkdir(parents=True)
    identity = {"release": release.as_posix(), "release_sha256": original, "kb_id": kb_id,
        "namespace": managed.corpus.namespace, "generation": status["generation"],
        "selection_sha256": selection.fingerprint, "index_manifest": status["manifest"],
        "baseline": baseline.identity, "question_count": len(items), "metric_version": 2,
        "llm_calls": 0, "dataset_kind": "development", "top_k": 10,
        "implementation_sha256": {p.relative_to(ROOT).as_posix(): file_hash(p) for p in
                                  [Path(__file__), *sorted((ROOT / "rag/search").glob("*.py"))]}}
    write_json(output / "execution.json", {"status": "running", **identity})
    try:
        for number, item in enumerate(items, 1):
            start = time.perf_counter()
            vector = query_embedding(baseline.encoder, item["user_input"])
            query_timings.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            scores = baseline.vectors @ vector
            order = sorted(range(len(baseline.chunks)),
                           key=lambda n: (-float(scores[n]), baseline.chunks[n]["chunk_id"]))[:10]
            contexts = [context(baseline.chunks[n], corpus.documents[baseline.chunks[n]["doc_id"]]["sha256"]) for n in order]
            timings["numpy"].append((time.perf_counter() - start) * 1000)
            results = {"numpy": contexts}
            for mode in ("dense", "bm25", "hybrid"):
                start = time.perf_counter()
                result = managed.query(item["user_input"], filters=SearchFilters(kb_id), mode=mode, vector=vector)
                timings[mode].append((time.perf_counter() - start) * 1000)
                if result["generation"] != status["generation"] or result["sync_lag"]:
                    raise ManagementError("BENCHMARK_CORPUS_MISMATCH")
                results[mode] = result["contexts"]
            for mode, contexts in results.items():
                records[mode].append({"id": item["id"], "status": "complete", "contexts": contexts,
                                      "metrics": retrieval_metrics(item, contexts, 2)})
            if number % 25 == 0:
                print(f"[RETRIEVAL] {number}/{len(items)}", flush=True)
        if managed.corpus.selection(kb_id).fingerprint != selection.fingerprint or managed.store.current(kb_id) != status["index"]:
            raise ManagementError("BENCHMARK_CORPUS_MISMATCH")
        if any(file_hash(release / name) != sha for name, sha in original.items()):
            raise ManagementError("BENCHMARK_CORPUS_MISMATCH")
        summaries = {name: summarize_scores(items, rows, retrieval_only=True, metric_version=2)
                     for name, rows in records.items()}
        import numpy as np
        timing_summary = {name: {"median_ms": float(np.median(values)), "p95_ms": float(np.percentile(values, 95))}
                          for name, values in {**timings, "shared_query_embedding": query_timings}.items()}
        deltas = [{"id": item["id"], "metrics": {mode: records[mode][n]["metrics"] for mode in records},
                   "dense_same_top10": [c["id"] for c in records["numpy"][n]["contexts"]] ==
                                       [c["legacy_chunk_id"] for c in records["dense"][n]["contexts"]]}
                  for n, item in enumerate(items)]
        report = {"identity": identity, "reports": summaries,
                  "gate": default_mode_gate(summaries, status["manifest"].get("default_mode", "dense")),
                  "latency": timing_summary, "latency_contract": "shared query embedding excluded; ES includes MySQL guards",
                  "dense_same_top10_count": sum(row["dense_same_top10"] for row in deltas)}
        write_json(output / "report.json", report)
        write_jsonl(output / "point_review.jsonl", deltas)
        for name, rows in records.items():
            write_jsonl(output / f"{name}_results.jsonl", rows)
        report["default_promoted"] = promote_default(managed, report, output / "report.json") if promote_best else False
        write_json(output / "execution.json", {"status": "complete", **identity, "default_promoted": report["default_promoted"]})
        write_json(output / "artifacts_sha256.json", {p.name: file_hash(p) for p in output.iterdir() if p.is_file()})
        return report
    except Exception as exc:
        write_json(output / "execution.json", {"status": "failed", **identity, **public_error(exc)})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kb-id", type=int, required=True)
    parser.add_argument("--allow-evolving", action="store_true")
    parser.add_argument("--promote-best", "--promote-hybrid", dest="promote_best", action="store_true",
                        help="select the best of dense/bm25/hybrid; --promote-hybrid is a legacy alias")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.mysql")
    parser.add_argument("--es-env-file", type=Path, default=ROOT / ".env.elasticsearch")
    args = parser.parse_args()
    engine = None
    try:
        engine = Settings.from_env(args.env_file).engine()
        config = SearchSettings.from_env(args.es_env_file)
        with config.client() as client:
            report = evaluate(args.release, args.output, args.kb_id, engine, client, prefix=config.prefix,
                              allow_evolving=args.allow_evolving, promote_best=args.promote_best)
        print(json.dumps({"output": args.output.as_posix(), "gate": report["gate"],
                          "dense_same_top10_count": report["dense_same_top10_count"],
                          "default_promoted": report["default_promoted"]}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps(public_error(exc)), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
