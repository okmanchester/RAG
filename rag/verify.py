"""Validate saved provenance and deterministically replay structure + splitting."""
from __future__ import annotations

import argparse
import copy
import json
from collections import defaultdict
from pathlib import Path

from .splitter import TokenCounter, chunk_document, STRATEGY
from .structure import build_sections
from .retrieval import iter_retrieval_chunks, summarize
from .chunk import digest
from .parsers import adapt_middle
from .tables import build_table_views, validate_views, fingerprint, TABLE_VERSION


def verify(output_dir):
    output_dir = Path(output_dir)
    report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 2, "expected MinerU corpus schema v2"
    assert report.get("strategy") == STRATEGY, "saved derived strategy is outdated; rebuild from native parsing"
    for name, sha in report["artifacts_sha256"].items():
        assert digest(output_dir / name) == sha, f"incomplete or modified output: {name}"
    documents = [json.loads(line) for line in (output_dir / "documents.jsonl").read_text(encoding="utf-8").splitlines()]
    chunks = [json.loads(line) for line in (output_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    params = report["parameters"]
    count = TokenCounter(params["model_path"])
    grouped = defaultdict(list)
    ids = set()
    for chunk in chunks:
        assert chunk["chunk_id"] not in ids, "duplicate chunk ID"
        ids.add(chunk["chunk_id"])
        grouped[chunk["doc_id"]].append(chunk)
        assert chunk["body"].strip(), "empty chunk body"
        assert count(chunk["text"]) == chunk["token_count"] <= params["max_tokens"]
        assert count(chunk["body"][:chunk["overlap_char_count"]]) == chunk["overlap_token_count"] <= params["overlap_tokens"]
    checked_docs, checked_spans, multi_page, checked_assets = 0, 0, 0, 0
    for document in documents:
        saved = grouped.pop(document.get("doc_id"), [])
        if document["status"] != "ok":
            assert not saved
            continue
        blocks = {b["block_id"]: b for b in document["blocks"]}
        artifact_dir = output_dir / document["artifacts"]
        assert output_dir.resolve() in artifact_dir.resolve().parents, "unsafe artifact path"
        for name, sha in document["artifact_files_sha256"].items():
            path = (artifact_dir / name).resolve()
            assert artifact_dir.resolve() in path.parents and path.is_file(), "missing/unsafe parse artifact"
            assert digest(path) == sha, f"changed parse artifact: {name}"
            checked_assets += 1
        adapted, _ = adapt_middle(json.loads((artifact_dir / "middle_json.json").read_text(encoding="utf-8")),
                                  "." + document["format"])
        title, sections, _ = build_sections(copy.deepcopy(adapted), Path(document["source_path"]).stem)
        assert adapted == [{k: v for k, v in b.items() if k not in ("role", "resolved_heading_level")} for b in document["blocks"]], "normalized parsing differs from raw Middle JSON"
        assert title == document["title"]
        if document.get("table_view_version"):
            assert document["table_view_version"] == TABLE_VERSION, "unsupported table view version"
            expected_views = build_table_views(document, sections, document.get("table_decisions"))
            assert document["table_views"] == expected_views, "logical table replay mismatch"
            validate_views(document, expected_views)
            decision_path = (output_dir / document["table_decision_artifact"]).resolve()
            assert output_dir.resolve() in decision_path.parents and decision_path.is_file(), "unsafe/missing table decisions"
            assert digest(decision_path) == document["table_decision_sha256"], "table decision checksum mismatch"
            stored = json.loads(decision_path.read_text(encoding="utf-8"))
            assert stored["decisions"] == document["table_decisions"], "table decision artifact mismatch"
            assert stored["identity"] == {"config": document["table_processing"],
                "middle_sha256": digest(artifact_dir / "middle_json.json"),
                "geometry_sha256": fingerprint(document.get("table_geometry", []))}, "table decision identity mismatch"
            assert document["table_processing_identity"] == fingerprint({"version": TABLE_VERSION,
                "config": document["table_processing"], "decisions": document["table_decisions"],
                "geometry": document.get("table_geometry", [])}), "table processing identity mismatch"
        replayed = chunk_document(document, sections, count, params["max_tokens"], params["overlap_tokens"])
        assert saved == replayed, f"replay mismatch: {document['source_path']}"
        represented = {b["block_id"] for b in blocks.values() if b["role"] in ("document_title", "excluded")}
        coverage = defaultdict(set)
        for chunk in saved:
            assert chunk["source_path"] == document["source_path"]
            assert chunk["category"] == document["category"]
            assert chunk["title"] == document["title"]
            represented.update(chunk["heading_block_ids"])
            for span in chunk["sources"]:
                block = blocks[span["block_id"]]
                represented.add(block["block_id"])
                assert all(span[k] == v for k, v in block["source"].items())
                assert chunk["body"][span["body_char_start"]:span["body_char_end"]] == block["text"][span["block_char_start"]:span["block_char_end"]]
                assert span["is_overlap"] == (span["body_char_end"] <= chunk["overlap_char_count"])
                checked_spans += 1
                coverage[block["block_id"]].update(range(span["block_char_start"], span["block_char_end"]))
            for span in chunk["context_sources"]:
                block = blocks[span["block_id"]]
                assert chunk["text"][span["text_char_start"]:span["text_char_end"]] == block["text"][span["block_char_start"]:span["block_char_end"]]
            for mapping in chunk.get("derived_sources", []):
                start, end = mapping["text_char_start"], mapping["text_char_end"]
                assert 0 <= start < end <= len(chunk["text"]), "invalid derived text range"
                actual = chunk["text"][start:end]
                assert actual == mapping["text"], "derived text differs from mapping"
                copied = []
                for source in mapping["source_refs"]:
                    block = blocks[source["block_id"]]
                    assert all(source[k] == v for k, v in block["source"].items()), "derived source location mismatch"
                    lo, hi = source["block_char_start"], source["block_char_end"]
                    assert 0 <= lo < hi <= len(block["text"]), "invalid derived source range"
                    copied.append(block["text"][lo:hi])
                assert "".join(copied) == actual, "derived value source reconstruction mismatch"
            for asset in chunk["asset_refs"]:
                target = (output_dir / asset["artifact_dir"] / asset["path"]).resolve()
                assert artifact_dir.resolve() in target.parents and target.is_file(), "missing chunk asset"
            if len({s.get("page") for s in chunk["sources"]}) > 1:
                multi_page += 1
        assert represented == set(blocks), f"unrepresented text blocks: {document['source_path']}"
        for b in blocks.values():
            if b["role"] not in ("document_title", "excluded", "heading"):
                assert coverage[b["block_id"]] == set(range(len(b["text"]))), "source character coverage incomplete"
        checked_docs += 1
    assert not grouped, "chunks reference unknown documents"
    assert len(documents) == report["summary"]["document_count"]
    assert len(chunks) == report["summary"]["chunk_count"]
    assert report["retrieval_filter"] == summarize(chunks), "retrieval filter report mismatch"
    eligible = list(iter_retrieval_chunks(output_dir / "chunks.jsonl"))
    assert all(c["content_type"] != "toc" and not c["retrieval_exclusion_reasons"] for c in eligible)
    assert len(eligible) == report["retrieval_filter"]["eligible_chunks"]
    result = {"status": "passed", "checked_documents": checked_docs, "checked_chunks": len(chunks),
              "retrieval_eligible_chunks": len(eligible), "retrieval_excluded_chunks": len(chunks) - len(eligible),
              "checked_source_spans": checked_spans, "cross_page_chunks": multi_page,
              "checked_artifact_files": checked_assets,
              "batch_success": all(d["status"] == "ok" for d in documents),
              "deterministic_replay": "identical chunk IDs and complete records from saved parsed blocks",
              "checks": ["token budgets", "nonempty bodies", "unique IDs", "document isolation",
                         "section isolation", "exact section reconstruction", "all parsed blocks represented",
                         "source character ranges", "overlap provenance", "deterministic replay", "TOC retrieval exclusion",
                         "logical table reconstruction", "derived text provenance", "saved boundary decisions"]}
    (output_dir / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/processed/mineru_v1"))
    result = verify(parser.parse_args().output)
    raise SystemExit(0 if result["batch_success"] else 1)
