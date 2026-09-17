"""Build a resumable pilot or replenishment batch; never approve generated items."""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")

from rag_baseline.benchmark.core import read_jsonl
from rag_baseline.benchmark.generation import generate_batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("data/processed/baseline_v1"))
    parser.add_argument("--kg", type=Path, default=Path("data/benchmark/knowledge_graph.json"))
    parser.add_argument("--output", type=Path, default=Path("data/benchmark/pilot_v3"))
    parser.add_argument("--size", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20260916)
    parser.add_argument("--full-batch", action="store_true", help="Candidates may enter holdout after review; pilot candidates never do")
    parser.add_argument("--reviewed", type=Path, help="Reviewed pool used to target deficient question types")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--target-type", choices=["fact", "condition", "within_document", "cross_document", "boundary"], help="Legacy candidate sampling target only; not a final label")
    parser.add_argument("--target-span", choices=["single_unit", "within_document", "cross_document"])
    parser.add_argument("--target-form", nargs="+", choices=["fact", "condition", "procedure", "comparison", "synthesis"])
    parser.add_argument("--target-category", choices=["policy", "technical"])
    parser.add_argument("--target-difficulty", choices=["easy", "medium", "hard", "boundary"])
    parser.add_argument("--target-style", choices=["document", "natural"])
    parser.add_argument("--target-behavior", choices=["answer", "abstain", "correct_premise", "clarify"])
    args = parser.parse_args()
    targets = {k: v for k, v in {"question_type": args.target_type, "category": args.target_category,
                                "difficulty": args.target_difficulty, "query_style": args.target_style,
                                "expected_behavior": args.target_behavior}.items() if v}
    if args.target_span:
        target = "fact" if args.target_span == "single_unit" else args.target_span
        if args.target_type and args.target_type != target:
            parser.error("--target-span conflicts with --target-type")
        targets["question_type"] = target
    if args.target_form:
        targets["answer_form"] = args.target_form
    if args.target_behavior and args.target_behavior != "answer":
        if args.target_span:
            parser.error("Boundary generation cannot require an answer evidence span")
        targets["question_type"] = "boundary"
    summary = asyncio.run(generate_batch(args.corpus, args.kg, args.output, args.size, args.seed,
                                         not args.full_batch, read_jsonl(args.reviewed) if args.reviewed else [], args.retry_failed, targets))
    return 0 if summary["completed"] == args.size else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (ValueError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
