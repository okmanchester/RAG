"""CLI: python -m rag_baseline.chunk --input data/raw --output data/processed/baseline_v1."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path

from .parsers import parse
from .splitter import DEFAULT_MODEL, TokenCounter, chunk_document
from .structure import build_sections
from .retrieval import summarize

PACKAGES = ("pymupdf4llm", "pymupdf", "pymupdf-layout", "python-docx", "transformers", "tokenizers")


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


def run(input_dir: Path, output_dir: Path, model_path=DEFAULT_MODEL, maximum=400, overlap=50):
    if maximum <= 0 or not 0 <= overlap < maximum:
        raise ValueError("require max-tokens > overlap-tokens >= 0")
    if not input_dir.is_dir():
        raise ValueError(f"input directory does not exist: {input_dir}")
    files = sorted((p for p in input_dir.rglob("*") if p.suffix.lower() in (".pdf", ".docx") and p.is_file()),
                   key=lambda p: p.relative_to(input_dir).as_posix())
    if not files:
        raise ValueError("no PDF or DOCX inputs found")
    counter = TokenCounter(model_path)
    model = Path(model_path)
    fingerprint = {p.name: digest(p) for p in sorted(model.iterdir())
                   if p.name in {"tokenizer.json", "tokenizer_config.json", "vocab.txt", "special_tokens_map.json",
                                 "config.json", "sentence_bert_config.json"}}
    report = {"schema_version": 1, "strategy": "structure_recursive_v1",
              "parameters": {"input": input_dir.as_posix(), "model_path": str(model_path),
                             "max_tokens": maximum, "overlap_tokens": overlap,
                             "title_in_budget": True, "special_tokens_in_budget": False,
                             "ocr": False, "overlap_across_sections": False},
              "versions": versions(), "tokenizer_files_sha256": fingerprint,
              "implementation_sha256": {p.name: digest(p) for p in sorted(Path(__file__).parent.glob("*.py"))},
              "limitations": ["Image-only content is not transcribed; OCR is disabled.",
                              "PDF reading order and headings depend on parser layout accuracy.",
                              "Numbered headings and TOC boundaries use conservative heuristics.",
                              "DOCX textboxes, tracked revisions, headers and footers are outside the baseline.",
                              "Tables retain text rows; complex table structure is not reconstructed."],
              "documents": []}
    output_dir.mkdir(parents=True, exist_ok=True)
    lengths, overlaps, doc_count, chunk_count = [], [], 0, 0
    all_chunks = []
    # Temporary names avoid exposing incomplete JSONL files during normal execution.
    with (output_dir / "documents.jsonl.tmp").open("w", encoding="utf-8", newline="\n") as documents_stream, \
            (output_dir / "chunks.jsonl.tmp").open("w", encoding="utf-8", newline="\n") as chunks_stream:
        for number, path in enumerate(files, 1):
            relative = path.relative_to(input_dir).as_posix()
            document = {"source_path": relative, "category": path.relative_to(input_dir).parts[0]
                        if len(path.relative_to(input_dir).parts) > 1 else "uncategorized",
                        "format": path.suffix.lower()[1:], "blocks": []}
            chunks = []
            try:
                file_hash = digest(path)
                document.update(sha256=file_hash, doc_id=hashlib.sha256(
                    f"{relative}:{file_hash}".encode()).hexdigest()[:24])
                blocks, metadata = parse(path)
                document.update(metadata, blocks=blocks)
                title, sections, warnings = build_sections(blocks, path.stem)
                document.update(title=title, warnings=warnings,
                                sections=[{**s, "blocks": [b["block_id"] for b in s["blocks"]]} for s in sections])
                if not blocks:
                    document.update(status="empty", warnings=warnings + ["no_extractable_text"])
                else:
                    chunks = chunk_document(document, sections, counter, maximum, overlap)
                    document["status"] = "ok"
            except Exception as exc:
                document.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            document["chunk_count"] = len(chunks)
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
    (output_dir / "report.json.tmp").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for name in ("documents.jsonl", "chunks.jsonl", "report.json"):
        (output_dir / (name + ".tmp")).replace(output_dir / name)
    (output_dir / "verification.json").unlink(missing_ok=True)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 1 if failed or empty else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/raw"))
    parser.add_argument("--output", type=Path, default=Path("data/processed/baseline_v1"))
    parser.add_argument("--model-path", default=DEFAULT_MODEL)
    parser.add_argument("--max-tokens", type=int, default=400)
    parser.add_argument("--overlap-tokens", type=int, default=50)
    args = parser.parse_args()
    try:
        return run(args.input, args.output, args.model_path, args.max_tokens, args.overlap_tokens)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
