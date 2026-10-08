"""Local dev20 protocol diagnostic using saved answers and faithfulness scores."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

from .core import file_hash, load_release, read_jsonl, write_json, write_jsonl
from .evaluation import evaluate_release, protocol_comparison
from .first_run import DEFAULT_RELEASE, load_local_env

SEED = "scoring-v2-dev20-20260930"


def select_dev20(items, scores):
    dev = [i for i in items if i["split"] == "dev"]
    by_id = {s["id"]: s for s in scores}
    chosen, selection = set(), []

    def add(rows, reason, count):
        candidates = [i for i in rows if i["id"] not in chosen]
        if len(candidates) < count:
            raise ValueError(f"Not enough dev20 candidates: {reason}")
        for item in candidates[:count]:
            chosen.add(item["id"])
            selection.append({"id": item["id"], "selection_reason": reason, "category": item["category"],
                              "v1_metrics": by_id[item["id"]]["metrics"]})

    for predicate, reason, count in (
        (lambda i: i["expected_behavior"] != "answer", "all_boundary", 6),
        (lambda i: i["expected_behavior"] == "answer" and i["evidence_span"] == "cross_document", "all_cross_document", 3),
        (lambda i: i["difficulty"] == "hard", "all_hard", 1),
    ):
        rows = sorted([i for i in dev if predicate(i)], key=lambda i: i["id"])
        if len(rows) != count:
            raise ValueError(f"Frozen dev quota changed: {reason}")
        add(rows, reason, count)
    ordinary = [i for i in dev if i["expected_behavior"] == "answer"]
    incomplete = [i for i in ordinary if by_id[i["id"]]["metrics"]["answer_point_completeness"] < 1]
    add(sorted(incomplete, key=lambda i: (by_id[i["id"]]["metrics"]["answer_point_completeness"], i["id"])), "low_completeness", 6)
    add(sorted(ordinary, key=lambda i: (by_id[i["id"]]["metrics"]["citation_coverage"], i["id"])), "low_citation_coverage", 2)
    perfect_metrics = ("answer_point_completeness", "faithfulness", "citation_support", "citation_coverage")
    for category in ("policy", "technical"):
        rows = [i for i in ordinary if i["category"] == category and all(by_id[i["id"]]["metrics"][m] == 1 for m in perfect_metrics)]
        add(sorted(rows, key=lambda i: hashlib.sha256(f"{SEED}:{i['id']}".encode("utf-8")).hexdigest()), f"perfect_{category}", 1)
    if len(selection) != 20:
        raise ValueError("dev20 selection must contain exactly 20 unique questions")
    return selection


def artifact_hashes(directory):
    return {path.relative_to(directory).as_posix(): file_hash(path) for path in sorted(directory.rglob("*")) if path.is_file()}


async def run_diagnostic(release, baseline, output, concurrency=4, retry_failed=False):
    release, baseline, output = Path(release).resolve(), Path(baseline).resolve(), Path(output).resolve()
    if output == baseline or baseline in output.parents or output == release or release in output.parents:
        raise ValueError("Diagnostic output must be separate from baseline and release")
    items, manifest = load_release(release)
    source = baseline / "dev" / "evaluation"
    selection = select_dev20(items, read_jsonl(source / "scores.jsonl"))
    hashes_path = output / "preserved_baseline_hashes.json"
    current = artifact_hashes(baseline)
    if hashes_path.exists():
        if json.loads(hashes_path.read_text(encoding="utf-8"))["files"] != current:
            raise ValueError("Original baseline artifacts changed")
    else:
        write_json(hashes_path, {"baseline": baseline.as_posix(), "gold_sha256": manifest["gold_sha256"], "files": current})
    ids_file = output / "selected_ids.jsonl"
    if ids_file.exists() and read_jsonl(ids_file) != selection:
        raise ValueError("Diagnostic selection changed; use a new output directory")
    write_jsonl(ids_file, selection)
    write_json(output / "selection_manifest.json", {"seed": SEED, "selection_order": selection,
               "baseline": baseline.as_posix(), "gold_sha256": manifest["gold_sha256"], "local_only": True})
    report = await evaluate_release(release, baseline / "dev" / "runs.jsonl", output / "evaluation",
                                    ids_path=ids_file, concurrency=concurrency, retry_failed=retry_failed,
                                    metric_version=2, reuse_faithfulness_from=source)
    comparison = protocol_comparison(source, output / "evaluation", output / "protocol_difference.json")
    comparison["v2_only_metrics"] = {name: report["overall"]["metrics"][name] for name in
                                    ("answer_point_missing_rate", "answer_all_points_correct")}
    write_json(output / "protocol_difference.json", comparison)
    after = artifact_hashes(baseline)
    if current != after:
        raise ValueError("Original baseline changed during diagnostic")
    load_release(release)
    write_json(output / "preservation_verification.json", {"baseline_files_checked": len(current), "unchanged": True,
               "gold_sha256": manifest["gold_sha256"], "question_count": len(selection)})
    lines = ["# dev20 评分协议差异", "", "同一检索结果及回答，仅重新调用千问 v2 判定；忠实度来自 v1 缓存。",
             "本样本用于诊断，仍为 uncalibrated_diagnostic，不代表150题新均值或系统质量提升。", "",
             "| 指标 | 同题 v1 | 同题 v2 | 有效配对 |", "|---|---:|---:|---:|"]
    def pct(value):
        return f"{value:.2%}" if value is not None else "—"
    for name, metric in comparison["metrics"].items():
        if not metric["paired_count"]:
            continue
        lines.append(f"| {name} | {pct(metric['v1_mean_same_questions'])} | {pct(metric['v2_mean_same_questions'])} | {metric['paired_count']} |")
    lines += ["", f"状态：{json.dumps(report['overall']['statuses'], ensure_ascii=False)}。",
              "新增指标见 evaluation/report.json；逐题要点原文、支持原文及修复记录见 evaluation/items/。",
              f"原 baseline 共 {len(current)} 个文件 SHA-256 校验保持不变。"]
    lines += ["", "v2新增指标（可回答题）：", "", "| 指标 | v2均值 | 有效题数 |", "|---|---:|---:|"]
    for name, metric in comparison["v2_only_metrics"].items():
        lines.append(f"| {name} | {pct(metric['mean_scored'])} | {metric['scored_count']} |")
    (output / "protocol_difference.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, default=DEFAULT_RELEASE)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
    load_local_env()
    report = asyncio.run(run_diagnostic(args.release, args.baseline, args.output, args.concurrency, args.retry_failed))
    print(json.dumps(report["overall"], ensure_ascii=False, indent=2))
    return 0 if report["overall"]["statuses"] == {"ok": 20} else 1


if __name__ == "__main__":
    raise SystemExit(main())
