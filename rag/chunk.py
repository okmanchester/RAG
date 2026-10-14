"""CLI: python -m rag.chunk --input data/raw --output data/processed/mineru_v1."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from collections import Counter
from pathlib import Path

from .parsers import DEFAULT_CONFIG, parse, load_settings
from .splitter import DEFAULT_MODEL, STRATEGY, TokenCounter, chunk_document
from .structure import build_sections
from .retrieval import summarize
from .tables import TABLE_VERSION, build_table_views, fingerprint as table_fingerprint
from .table_llm import options as table_options, collect_decisions

PACKAGES = ("mineru", "docvortex", "torch", "mineru-llama-cpp", "transformers", "tokenizers")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def versions():
    result = {"python": platform.python_version()}
    for name in PACKAGES:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def distribution(values):
    if not values:
        return {"min": None, "max": None, "mean": None, "p50": None, "p95": None}
    values = sorted(values)
    return {"min": values[0], "max": values[-1], "mean": round(sum(values) / len(values), 2),
            "p50": values[(len(values) - 1) // 2], "p95": values[int((len(values) - 1) * .95)]}


def process_document(path: Path, relative: str, output_dir: Path, counter, maximum=400, overlap=50,
                     *, reuse_parsed=False, page_range="", config_path=DEFAULT_CONFIG,
                     refresh_table_decisions=False, geometry_documents=None, stage=None, catch_errors=False):
    """Shared single-document pipeline; managed tasks propagate failures to their safe boundary."""
    path, output_dir = Path(path), Path(output_dir)
    geometry_documents = geometry_documents or {}
    table_config = table_options(load_settings(config_path))
    document = {"schema_version": 2, "source_path": relative, "category": Path(relative).parts[0]
                if len(Path(relative).parts) > 1 else "uncategorized",
                "format": path.suffix.lower()[1:], "blocks": []}
    chunks = []
    try:
        file_hash = digest(path)
        document.update(sha256=file_hash, doc_id=hashlib.sha256(
            f"{relative}:{file_hash}".encode()).hexdigest()[:24])
        if relative in geometry_documents:
            geometry = geometry_documents[relative]
            if geometry["source_sha256"] != file_hash:
                raise ValueError("table geometry source checksum mismatch")
            document["table_geometry"] = geometry["cells"]
        artifact_relative = Path("parsed") / document["doc_id"]
        if stage: stage("parsing")
        blocks, metadata = parse(path, artifact_dir=output_dir / artifact_relative,
                                 reuse=reuse_parsed, page_range=page_range, config_path=config_path)
        metadata["artifacts"] = artifact_relative.as_posix()
        metadata["artifact_files_sha256"] = {p.relative_to(output_dir / artifact_relative).as_posix(): digest(p)
            for p in sorted((output_dir / artifact_relative).rglob("*")) if p.is_file()}
        document.update(metadata, blocks=blocks)
        if stage: stage("chunking")
        title, sections, warnings = build_sections(blocks, path.stem)
        document.update(title=title, warnings=warnings + document.get("parser_warnings", []),
                        sections=[{**s, "blocks": [b["block_id"] for b in s["blocks"]]} for s in sections])
        rules = build_table_views(document, sections)
        decision_path = output_dir / "tables" / document["doc_id"] / "table_decisions.json"
        decision_identity = {"config": table_config, "middle_sha256": digest(output_dir / artifact_relative / "middle_json.json"),
                             "geometry_sha256": table_fingerprint(document.get("table_geometry", []))}
        if table_config["llm_enabled"] and reuse_parsed and not refresh_table_decisions:
            if not decision_path.is_file():
                raise ValueError("saved table decisions missing; use --refresh-table-decisions with --reuse-parsed")
            saved_decisions = json.loads(decision_path.read_text(encoding="utf-8"))
            if saved_decisions.get("identity") != decision_identity:
                raise ValueError("saved table decision identity mismatch")
            decisions = saved_decisions["decisions"]
        else:
            decisions = collect_decisions(rules, table_config)
            # Derived artifacts stay outside the immutable native parse
            # directory/checksum manifest.
        if table_config["llm_enabled"]:
            contexts = {o["context_id"] for view in rules for o in view["operations"]}
            if set(decisions) != contexts:
                raise ValueError("saved table decisions do not cover the actual boundaries; refresh required")
        document.update(table_view_version=TABLE_VERSION, table_processing=table_config,
                        table_decisions=decisions, table_views=build_table_views(document, sections, decisions))
        document["table_processing_identity"] = table_fingerprint({"version": TABLE_VERSION, "config": table_config,
                                                             "decisions": decisions, "geometry": document.get("table_geometry", [])})
        derived_dir = output_dir / "tables" / document["doc_id"]
        derived_dir.mkdir(parents=True, exist_ok=True)
        saved_path = derived_dir / "table_decisions.json"
        saved_path.write_text(json.dumps({"identity": decision_identity, "decisions": decisions},
                                        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        document["table_decision_artifact"] = saved_path.relative_to(output_dir).as_posix()
        document["table_decision_sha256"] = digest(saved_path)
        if not sections:
            document.update(status="empty", warnings=warnings + ["no_extractable_text"])
        else:
            chunks = chunk_document(document, sections, counter, maximum, overlap)
            document["status"] = "ok"
    except Exception as exc:
        if not catch_errors:
            raise
        document.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    document["excluded_block_count"] = sum(b.get("role") == "excluded" for b in document["blocks"])
    document["exclusions"] = [{"block_id": b["block_id"], "reason": b.get("exclusion_reason"),
                               "source": b["source"]} for b in document["blocks"] if b.get("role") == "excluded"]
    document["chunk_count"] = len(chunks)
    return document, chunks


def run(input_dir: Path, output_dir: Path, model_path=DEFAULT_MODEL, maximum=400, overlap=50,
        *, reuse_parsed=False, page_range="", config_path=DEFAULT_CONFIG, include=None, refresh_table_decisions=False,
        table_geometry_path=None):
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    if maximum <= 0 or not 0 <= overlap < maximum:
        raise ValueError("require max-tokens > overlap-tokens >= 0")
    if not input_dir.is_dir():
        raise ValueError(f"input directory does not exist: {input_dir}")
    files = sorted((p for p in input_dir.rglob("*") if p.suffix.lower() in (".pdf", ".docx") and p.is_file()),
                   key=lambda p: p.relative_to(input_dir).as_posix())
    if include:
        requested = set(include)
        files = [p for p in files if p.relative_to(input_dir).as_posix() in requested]
        if {p.relative_to(input_dir).as_posix() for p in files} != requested:
            raise ValueError("--include must name existing PDF/DOCX paths relative to input")
    if not files:
        raise ValueError("no PDF or DOCX inputs found")
    counter = TokenCounter(model_path)
    table_config = table_options(load_settings(config_path))
    geometry_documents = {}
    if table_geometry_path:
        geometry_data = json.loads(Path(table_geometry_path).read_text(encoding="utf-8"))
        for item in geometry_data["documents"]:
            if item["source_path"] in geometry_documents:
                raise ValueError("duplicate source in table geometry")
            geometry_documents[item["source_path"]] = item
    model = Path(model_path)
    fingerprint = {p.name: digest(p) for p in sorted(model.iterdir())
                   if p.name in {"tokenizer.json", "tokenizer_config.json", "vocab.txt", "special_tokens_map.json",
                                 "config.json", "sentence_bert_config.json"}}
    report = {"schema_version": 2, "strategy": STRATEGY,
              "parameters": {"input": input_dir.as_posix(), "model_path": str(model_path),
                             "max_tokens": maximum, "overlap_tokens": overlap,
                             "title_in_budget": True, "special_tokens_in_budget": False,
                             "ocr": "auto", "pdf_tier": "standard", "docx_tier": "flash",
                             "image_analysis": False, "page_range": page_range,
                             "include": list(include) if include else None,
                             "mineru_config": str(config_path), "reuse_parsed": reuse_parsed,
                             "overlap_across_sections": False},
              "table_processing": table_config,
              "versions": versions(), "tokenizer_files_sha256": fingerprint,
              "implementation_sha256": {p.name: digest(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
              "limitations": ["Parsing quality depends on MinerU layout/OCR accuracy.",
                              "No generated descriptions for pictures or charts.",
                              "DOCX content positions are not physical page numbers.",
                              "Oversized structures are split without silently truncating text.",
                              "Replay verifies saved parsing, not repeated model inference."],
              "documents": []}
    output_dir.mkdir(parents=True, exist_ok=True)
    lengths, overlaps, doc_count, chunk_count = [], [], 0, 0
    all_chunks = []
    # Temporary names avoid exposing incomplete JSONL files during normal execution.
    with (output_dir / "documents.jsonl.tmp").open("w", encoding="utf-8", newline="\n") as documents_stream, \
            (output_dir / "chunks.jsonl.tmp").open("w", encoding="utf-8", newline="\n") as chunks_stream:
        for number, path in enumerate(files, 1):
            relative = path.relative_to(input_dir).as_posix()
            document, chunks = process_document(path, relative, output_dir, counter, maximum, overlap,
                reuse_parsed=reuse_parsed, page_range=page_range, config_path=config_path,
                refresh_table_decisions=refresh_table_decisions, geometry_documents=geometry_documents,
                catch_errors=True)
            documents_stream.write(json.dumps(document, ensure_ascii=False) + "\n")
            for chunk in chunks:
                chunks_stream.write(json.dumps(chunk, ensure_ascii=False) + "\n")
                lengths.append(chunk["token_count"])
                overlaps.append(chunk["overlap_token_count"])
            doc_count += 1
            chunk_count += len(chunks)
            all_chunks.extend(chunks)
            report["documents"].append({k: v for k, v in document.items() if k not in ("blocks", "sections")})
            print(f"[{number}/{len(files)}] {document['status']}: {relative} -> {len(chunks)} chunks", flush=True)
    failed = sum(d["status"] == "failed" for d in report["documents"])
    empty = sum(d["status"] == "empty" for d in report["documents"])
    report.update(summary={"document_count": doc_count, "ok": doc_count - failed - empty, "failed": failed,
                           "empty": empty, "chunk_count": chunk_count,
                           "empty_page_count": sum(len(d.get("empty_pages", [])) for d in report["documents"]),
                           "token_count": distribution(lengths), "overlap_token_count": distribution(overlaps)},
                  validation={"all_chunks_within_budget": all(x <= maximum for x in lengths),
                              "all_overlaps_within_budget": all(x <= overlap for x in overlaps),
                              "section_text_reconstruction": "passed for all successfully chunked documents"})
    report["retrieval_filter"] = summarize(all_chunks)
    report["audit"] = {"excluded_blocks": sum(d["excluded_block_count"] for d in report["documents"]),
                       "structure_types": {}}
    report["audit"]["structure_types"] = dict(Counter(c["structure_type"] for c in all_chunks))
    report["artifacts_sha256"] = {name: digest(output_dir / (name + ".tmp"))
                                  for name in ("documents.jsonl", "chunks.jsonl")}
    (output_dir / "report.json.tmp").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name in ("documents.jsonl", "chunks.jsonl", "report.json"):
        (output_dir / (name + ".tmp")).replace(output_dir / name)
    (output_dir / "verification.json").unlink(missing_ok=True)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 1 if failed or empty else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/raw"))
    parser.add_argument("--output", type=Path, default=Path("data/processed/mineru_v1"))
    parser.add_argument("--model-path", default=DEFAULT_MODEL)
    parser.add_argument("--max-tokens", type=int, default=400)
    parser.add_argument("--overlap-tokens", type=int, default=50)
    parser.add_argument("--reuse-parsed", action="store_true", help="Rechunk validated saved parsing without inference")
    parser.add_argument("--refresh-table-decisions", action="store_true",
                        help="Request new project table decisions while reusing native parsing")
    parser.add_argument("--table-geometry", type=Path, help="Verified source-bound cell geometry JSON (optional)")
    parser.add_argument("--pages", default="", help="PDF page range (one-based); empty means all pages")
    parser.add_argument("--include", nargs="+", help="Input-relative PDF/DOCX paths; useful for smoke tests")
    parser.add_argument("--mineru-config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    try:
        return run(args.input, args.output, args.model_path, args.max_tokens, args.overlap_tokens,
                   reuse_parsed=args.reuse_parsed, page_range=args.pages,
                   config_path=args.mineru_config, include=args.include,
                   refresh_table_decisions=args.refresh_table_decisions, table_geometry_path=args.table_geometry)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
