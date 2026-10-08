"""Run every split of an explicitly supplied frozen release."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from .core import file_hash, load_release, now, read_jsonl, write_json, write_jsonl
from .evaluation import evaluate_release, summarize_scores
from .first_run import (DenseIndex, preflight, publish_scores,
                        run_baseline, usable_release, verify_uploaded)


def summarize_full_run(release: Path, output: Path) -> dict:
    items, manifest = load_release(release)
    scores, runs, traces, reports, remote = [], [], {}, {}, {}
    for split in ("dev", "holdout"):
        folder = output / split
        scores.extend(read_jsonl(folder / "evaluation" / "scores.jsonl"))
        runs.extend(read_jsonl(folder / "runs.jsonl"))
        traces.update(json.loads((folder / "trace_map.json").read_text(encoding="utf-8")))
        reports[split] = json.loads((folder / "evaluation" / "report.json").read_text(encoding="utf-8"))
        remote[split] = json.loads((folder / "remote_verification.json").read_text(encoding="utf-8"))
    expected = {item["id"] for item in items}
    if len(runs) != len(expected) or len(scores) != len(expected):
        raise ValueError("Full baseline requires one run and score per frozen question")
    if {row["id"] for row in runs} != expected or {row["id"] for row in scores} != expected or set(traces) != expected:
        raise ValueError("Full baseline question coverage differs from frozen release")
    versions = {report["identity"].get("metric_version", 1) for report in reports.values()}
    if len(versions) != 1:
        raise ValueError("Cannot aggregate different scoring protocols")
    metric_version = versions.pop()
    write_jsonl(output / "runs.jsonl", runs)
    write_jsonl(output / "scores.jsonl", scores)
    write_json(output / "trace_map.json", traces)
    report = {"completed_at": now(), "release": release.as_posix(),
              "gold_sha256": manifest["gold_sha256"], "calibration_status": "uncalibrated_diagnostic",
              "holdout_policy": "fixed_config_baseline_no_tuning", "metric_version": metric_version,
              **summarize_scores(items, scores, metric_version=metric_version),
              "splits": {split: reports[split]["overall"] for split in reports},
              "evaluation_identities": {split: reports[split]["identity"] for split in reports},
              "remote_verification": remote,
              "artifact_sha256": {name: file_hash(output / name) for name in ("runs.jsonl", "scores.jsonl", "trace_map.json")}}
    write_json(output / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--retry-failed", action="store_true", help="Retry failed saved evaluations")
    parser.add_argument("--evaluation-concurrency", type=int, default=4)
    parser.add_argument("--metric-version", type=int, choices=[1, 2], default=2)
    parser.add_argument("--remote-concurrency", type=int, default=8)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument("--sync-scores", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
    os.environ.setdefault("LANGFUSE_TIMEOUT", "30")
    release = usable_release(args.release)
    preflight(release)
    from langfuse import get_client
    langfuse = get_client()
    items, _ = load_release(release)
    counts = {split: sum(item["split"] == split for item in items) for split in ("dev", "holdout")}
    if any(count == 0 for count in counts.values()) or sum(counts.values()) != len(items):
        raise ValueError(f"Expected a release with nonempty dev/holdout splits, got {counts}")
    if not (args.verify_only or args.sync_scores):
        index = DenseIndex(release)
        for split in ("dev", "holdout"):
            folder = args.output / split
            ids = sorted(item["id"] for item in items if item["split"] == split)
            ids_file = folder / "selected_ids.jsonl"
            if ids_file.exists() and [row["id"] for row in read_jsonl(ids_file)] != ids:
                raise ValueError("Selected IDs changed; use a new output directory")
            write_jsonl(ids_file, [{"id": identifier} for identifier in ids])
            run_baseline(release, ids, folder, index, langfuse, split=split)
        for split in ("dev", "holdout"):
            folder = args.output / split
            report = asyncio.run(evaluate_release(release, folder / "runs.jsonl", folder / "evaluation",
                split=split, retry_failed=args.retry_failed, ids_path=folder / "selected_ids.jsonl",
                concurrency=args.evaluation_concurrency, metric_version=args.metric_version))
            report["calibration_status"] = "uncalibrated_diagnostic"
            write_json(folder / "evaluation" / "report.json", report)
    for split in ("dev", "holdout"):
        folder = args.output / split
        if not args.verify_only:
            publish_scores(folder, folder / "evaluation", langfuse, concurrency=args.remote_concurrency)
        verify_uploaded(folder, release, langfuse, split=split, concurrency=args.remote_concurrency)
    report = summarize_full_run(release, args.output)
    print(json.dumps({"output": args.output.as_posix(), "question_count": report["overall"]["count"],
                      "statuses": report["overall"]["statuses"],
                      "remote_verification": report["remote_verification"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
