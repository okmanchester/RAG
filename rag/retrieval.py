"""Annotate retained TOC chunks and provide the retrieval ingestion boundary."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

POLICY = "exclude_navigation_v2"
FIELDS = {"content_type", "retrieval_enabled", "retrieval_exclusion_reasons"}


def annotate_chunk(chunk: dict, toc_block_ids=()) -> None:
    # Exact navigation headings only: substantive industry/product catalogs
    # mentioning '目录' must remain eligible. Include overlap source spans too.
    toc = any(re.fullmatch(r"(?:目\s*[录次]|[图表]\s*目\s*录|contents|table of contents|list of (?:figures|tables))",
                          title.strip(), re.I) for title in chunk["heading_path"])
    toc = toc or any(span["block_id"] in toc_block_ids for span in chunk["sources"])
    chunk.update(content_type="toc" if toc else chunk.get("structure_type", "body"), retrieval_enabled=not toc,
                 retrieval_exclusion_reasons=["table_of_contents"] if toc else [])


def summarize(chunks: list[dict]) -> dict:
    excluded = [c for c in chunks if not c["retrieval_enabled"]]
    counts = Counter(c["source_path"] for c in excluded)
    total_tokens = sum(c["token_count"] for c in chunks)
    excluded_tokens = sum(c["token_count"] for c in excluded)
    return {"policy": POLICY, "total_chunks": len(chunks), "excluded_chunks": len(excluded),
            "eligible_chunks": len(chunks) - len(excluded),
            "excluded_chunk_percent": round(100 * len(excluded) / len(chunks), 2) if chunks else 0,
            "excluded_tokens": excluded_tokens,
            "excluded_token_percent": round(100 * excluded_tokens / total_tokens, 2) if total_tokens else 0,
            "excluded_by_document": dict(sorted(counts.items())),
            "excluded_chunk_ids": [c["chunk_id"] for c in excluded]}


def iter_retrieval_chunks(path: Path):
    """Use before embedding/indexing; reject old, unannotated input explicitly."""
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            chunk = json.loads(line)
            if type(chunk.get("retrieval_enabled")) is not bool:
                raise ValueError("Missing/invalid retrieval_enabled; run python -m rag.retrieval first")
            if chunk["retrieval_enabled"]:
                yield chunk


def mark_existing(output_dir: Path):
    path = output_dir / "chunks.jsonl"
    documents = [json.loads(line) for line in (output_dir / "documents.jsonl").read_text(encoding="utf-8").splitlines()]
    toc_ids = {d["doc_id"]: {b["block_id"] for b in d["blocks"] if b["role"] == "toc"}
               for d in documents if "doc_id" in d}
    original = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    updated = []
    for chunk in original:
        marked = dict(chunk)
        annotate_chunk(marked, toc_ids[chunk["doc_id"]])
        assert {k: v for k, v in marked.items() if k not in FIELDS} == {k: v for k, v in chunk.items() if k not in FIELDS}
        updated.append(marked)
    report_path = output_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["retrieval_filter"] = summarize(updated)
    # Preserve the original parse/chunk implementation fingerprint: this is a
    # metadata-only migration, not a new extraction or a new split.
    report["retrieval_annotation"] = {
        "policy": POLICY, "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "original_chunk_fields_preserved": True,
    }
    temp = path.with_suffix(".jsonl.tmp")
    temp.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in updated), encoding="utf-8")
    report_temp = report_path.with_suffix(".json.tmp")
    if "artifacts_sha256" in report:
        report["artifacts_sha256"]["chunks.jsonl"] = hashlib.sha256(temp.read_bytes()).hexdigest()
    report_temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
    report_temp.replace(report_path)
    (output_dir / "verification.json").unlink(missing_ok=True)
    print(json.dumps({k: v for k, v in report["retrieval_filter"].items()
                      if k not in ("excluded_chunk_ids", "excluded_by_document")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/processed/mineru_v1"))
    mark_existing(parser.parse_args().output)
