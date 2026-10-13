"""python -m rag.benchmark --help"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .core import (Corpus, digest, duplicate_clusters, export_review, freeze, import_review,
                   quotas, read_jsonl, validate_item, write_json, write_jsonl)
from .core import schema_version, freeze_reviewed, load_release, GOLD150_CORE_FILES
from .quality import boundary


def main():
    os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
    parser = argparse.ArgumentParser(description="人工审核、冻结和评测固定 RAG benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser('verify-release', help='Verify the runtime core, optionally including all local history')
    p.add_argument('--release', type=Path, required=True)
    p.add_argument('--include-history', action='store_true')
    p.add_argument('--allow-evolving', action='store_true')
    for name in ("validate", "export-review", "import-review", "precheck-boundary"):
        p = sub.add_parser(name)
        p.add_argument("--items", type=Path, required=True)
        p.add_argument("--corpus", type=Path, required=True)
        if name != "validate":
            p.add_argument("--output", type=Path, required=True)
        if name == "import-review":
            p.add_argument("--review", type=Path, required=True)
    p = sub.add_parser("pool", help="Merge batches with identical corpus/judge; retain generation provenance")
    p.add_argument("--batches", nargs="+", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--candidates", action="store_true", help="Combine candidates without approving them")
    p.add_argument("--screened-only", action="store_true", help="Exclude candidates refuted by boundary precheck; keep exclusion ledger")
    p.add_argument("--corpus", type=Path, required=True)
    for name in ('freeze', 'freeze-reviewed'):
        p = sub.add_parser(name)
        p.add_argument("--items", type=Path, required=True)
        p.add_argument("--manifest", type=Path, required=True)
        p.add_argument("--families", type=Path, required=True)
        p.add_argument("--corpus", type=Path, required=True)
        p.add_argument("--output", type=Path, required=True)
        if name in ('freeze-reviewed',):
            p.add_argument('--authorization', required=True, help='Record the explicit user freeze instruction')
            p.add_argument('--reference-releases', nargs='*', type=Path, default=[])
            p.add_argument('--review-artifacts', nargs='*', type=Path, default=[])
    for name in ("evaluate", "export-ragas"):
        p = sub.add_parser(name)
        p.add_argument("--release", type=Path, required=True)
        p.add_argument("--runs", type=Path, required=True)
        p.add_argument("--output", type=Path, required=True)
        p.add_argument("--split", choices=["dev", "holdout"], default="dev")
        if name == "evaluate":
            p.add_argument('--allow-evolving', action='store_true')
            p.add_argument("--retrieval-only", action="store_true")
            p.add_argument("--retry-failed", action="store_true")
            p.add_argument("--ids-file", type=Path, help="JSONL rows with id; score only these IDs in the selected split")
            p.add_argument("--metric-version", type=int, choices=[1, 2], default=2)
            p.add_argument("--reuse-faithfulness-from", type=Path)
            p.add_argument("--concurrency", type=int, default=1)
    p = sub.add_parser("compare")
    p.add_argument("--left", type=Path, required=True)
    p.add_argument("--right", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--protocol-difference", action="store_true", help="Compare saved identical answers across scoring protocols")
    for name in ("calibration-template", "calibrate"):
        p = sub.add_parser(name)
        p.add_argument("--release", type=Path, required=True)
        p.add_argument("--output", type=Path, required=True)
        if name == "calibrate":
            p.add_argument("--labels", type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'verify-release':
        rows, manifest = load_release(args.release, verify_history=args.include_history, allow_evolving=args.allow_evolving)
        core_only = manifest.get('dataset_kind') == 'gold150' and not args.include_history
        print(json.dumps({'release':args.release.as_posix(), 'item_count':len(rows),
                          'verification':('evolving_core' if manifest.get('dataset_kind')=='gold150_evolving'
                                          else 'core' if core_only else 'full_release'),
                          'verified_file_count':len(GOLD150_CORE_FILES) if core_only else
                              len(manifest.get('artifacts_sha256', {}))+1}, ensure_ascii=False, indent=2))
    elif args.command in ("validate", "export-review", "import-review", "precheck-boundary"):
        rows = read_jsonl(args.items)
        corpus = Corpus(args.corpus)
        if len({r["id"] for r in rows}) != len(rows):
            raise ValueError("Duplicate item IDs")
        for row in rows:
            validate_item(row, corpus, approved=row.get("review_status") == "approved")
        if args.command == "validate":
            _, links = duplicate_clusters(rows)
            print(json.dumps({"quotas": quotas(rows), "duplicate_links": links}, ensure_ascii=False, indent=2))
        elif args.command == "export-review":
            export_review(rows, args.output, corpus)
        elif args.command == "import-review":
            if args.output.exists():
                raise FileExistsError("Review history is append-only; use a new reviewed filename")
            result = import_review(rows, args.review, corpus)
            write_jsonl(args.output, result)
            print(json.dumps({"reviewed_rows": len(result), "approved": sum(i["review_status"] == "approved" for i in result)}))
        else:
            from .generation import build_llm, precheck_boundary
            if args.output.exists():
                raise FileExistsError(args.output)
            async def precheck():
                llm, client = build_llm("generator", args.output.with_suffix(".usage.jsonl"))
                try:
                    for row in rows:
                        if boundary(row):
                            row["boundary_precheck"] = await precheck_boundary(row, corpus, llm)
                finally:
                    await client.close()
            asyncio.run(precheck())
            write_jsonl(args.output, rows)
    elif args.command == "pool":
        if args.output.exists():
            raise FileExistsError(args.output)
        if args.screened_only and not args.candidates:
            raise ValueError("--screened-only is only for candidate pools")
        items, sources, excluded, base = [], [], [], None
        for batch in args.batches:
            manifest = json.loads((batch/"manifest.json").read_text(encoding="utf-8"))
            filename = "candidates_checked.jsonl" if args.candidates and (batch/"candidates_checked.jsonl").exists() else "candidates.jsonl" if args.candidates else "reviewed.jsonl"
            rows = read_jsonl(batch/filename)
            if base is None:
                base = manifest
            # Candidate prompts may evolve after pilot review; preserve every source protocol.
            for key in ("corpus", "judge"):
                if base[key] != manifest[key]:
                    raise ValueError(f"Batch protocols differ: {key}")
            if args.screened_only:
                excluded.extend(r for r in rows if r.get("boundary_precheck", {}).get("behavior_supported") is False)
                rows = [r for r in rows if r.get("boundary_precheck", {}).get("behavior_supported") is not False]
            items.extend(rows)
            sources.append({"batch": str(batch), "items_file": filename, "included_sha256": digest(rows), "manifest": manifest})
        if len({i["id"] for i in items}) != len(items):
            raise ValueError("Duplicate IDs across batches")
        schema_version(items)
        corpus = Corpus(args.corpus)
        if corpus.identity() != base["corpus"]:
            raise ValueError("Pool corpus differs from source batches")
        write_jsonl(args.output/("candidates.jsonl" if args.candidates else "reviewed.jsonl"), items)
        write_json(args.output/"manifest.json", {**base, "source_batches": sources,
                                               "status": "candidates_pending_review" if args.candidates else "reviewed_pool"})
        write_jsonl(args.output/"machine_excluded.jsonl", excluded)
        write_json(args.output/"families.json", corpus.family_report())
    elif args.command in ("freeze", "freeze-reviewed"):
        finalize = {'freeze': freeze, 'freeze-reviewed': freeze_reviewed}[args.command]
        options = ({'authorization':args.authorization, 'reference_releases':args.reference_releases,
                    'review_artifacts':args.review_artifacts}
                   if args.command in ('freeze-reviewed',) else {})
        finalize(read_jsonl(args.items), Corpus(args.corpus), json.loads(args.families.read_text(encoding="utf-8")),
               args.output, json.loads(args.manifest.read_text(encoding="utf-8")), **options)
        print(f"Frozen release: {args.output}")
    else:
        from .evaluation import (calibrate, calibration_template, evaluate_release,
                                 paired_comparison, protocol_comparison, ragas_export)
        if args.command == "evaluate":
            report = asyncio.run(evaluate_release(args.release, args.runs, args.output, args.split,
                                                  args.retrieval_only, args.retry_failed, args.ids_file,
                                                  concurrency=args.concurrency, metric_version=args.metric_version,
                                                  reuse_faithfulness_from=args.reuse_faithfulness_from,
                                                  allow_evolving=args.allow_evolving))
            print(json.dumps(report["overall"], ensure_ascii=False, indent=2))
            if any(k != "ok" for k in report["overall"]["statuses"]):
                return 1
        elif args.command == "export-ragas":
            ragas_export(args.release, args.runs, args.output, args.split)
        elif args.command == "compare":
            (protocol_comparison if args.protocol_difference else paired_comparison)(args.left, args.right, args.output)
        elif args.command == "calibration-template":
            calibration_template(args.release, args.output)
        elif args.command == "calibrate":
            asyncio.run(calibrate(args.release, args.labels, args.output))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (ValueError, FileExistsError, FileNotFoundError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
