"""Import validation tests: no database or model inference required."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from rag.management.storage import file_hash, load_snapshot, publish_raw, relative_path


def write_corpus(root, *, documents=None, chunks=None, raw_bytes=b"fixture pdf"):
    root.mkdir(parents=True, exist_ok=True)
    if documents is None:
        documents = [{"doc_id": "legacy_doc", "source_path": "policy/sample.pdf", "title": "测试文档",
                      "category": "policy", "format": "pdf", "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                      "status": "ok", "chunk_count": 1,
                      "blocks": [{"block_id": 1, "text": "正文内容", "source": {"page": 1}, "role": "body"}]}]
    if chunks is None:
        chunks = [{"chunk_id": "legacy_chunk", "doc_id": "legacy_doc", "source_path": "policy/sample.pdf",
                   "chunk_index": 1, "body": "正文内容", "text": "测试文档\n正文内容", "token_count": 8,
                   "content_type": "body", "retrieval_enabled": True, "heading_path": ["第一章"],
                   "sources": [{"block_id": 1, "block_char_start": 0, "block_char_end": 4}],
                   "derived_sources": [{"operation": "copy", "source_refs": [{"block_id": 1}]}]}]
    for name, values in (("documents.jsonl", documents), ("chunks.jsonl", chunks)):
        (root / name).write_bytes(("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values)).encode())
    (root / "manifest.json").write_text(json.dumps({"corpus": {n: file_hash(root / n)
        for n in ("documents.jsonl", "chunks.jsonl")}}), encoding="utf-8")
    return copy.deepcopy(documents), copy.deepcopy(chunks)


class StorageTests(unittest.TestCase):
    def test_normalized_paths_reject_escape(self):
        self.assertEqual(relative_path("policy\\./sample.pdf"), "policy/sample.pdf")
        for path in ("../escape.pdf", "a/../escape.pdf", "D:/file.pdf", "/file.pdf", "", ".", "a\x00b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                relative_path(path)

    def test_snapshot_preserves_bytes_and_detects_existing_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docs, chunks = write_corpus(root / "corpus")
            snapshot = load_snapshot(root / "corpus")
            target = snapshot.publish(root / "storage")
            self.assertEqual(snapshot.documents, docs)
            self.assertEqual(snapshot.chunks_by_doc["legacy_doc"], chunks)
            self.assertEqual((target / "chunks.jsonl").read_bytes(), (root / "corpus/chunks.jsonl").read_bytes())
            self.assertEqual(snapshot.publish(root / "storage"), target)
            (target / "chunks.jsonl").write_bytes(b"corrupted")
            with self.assertRaisesRegex(ValueError, "checksum"):
                snapshot.publish(root / "storage")

    def test_raw_copy_hashes_input_even_on_repeat_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw"
            (raw / "policy").mkdir(parents=True)
            (raw / "policy/sample.pdf").write_bytes(b"fixture pdf")
            expected = hashlib.sha256(b"fixture pdf").hexdigest()
            target = publish_raw(raw, "policy/sample.pdf", expected, root / "storage")
            self.assertEqual(target.read_bytes(), b"fixture pdf")
            (raw / "policy/sample.pdf").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "checksum"):
                publish_raw(raw, "policy/sample.pdf", expected, root / "storage")

    def test_hash_and_relational_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            documents, chunks = write_corpus(root)
            (root / "chunks.jsonl").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_snapshot(root)
            variants = [
                ("orphan", lambda c: c.update(doc_id="missing")),
                ("retrieval_enabled", lambda c: c.update(retrieval_enabled=1)),
                ("unknown block", lambda c: c["sources"][0].update(block_id=99)),
                ("interval", lambda c: c["sources"][0].update(block_char_end=99)),
            ]
            for message, mutate in variants:
                changed = copy.deepcopy(chunks)
                mutate(changed[0])
                write_corpus(root, documents=documents, chunks=changed)
                with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                    load_snapshot(root)
            write_corpus(root, documents=documents, chunks=chunks + chunks)
            with self.assertRaisesRegex(ValueError, "duplicate chunk ID"):
                load_snapshot(root)

    def test_parser_assets_are_copied_and_declared_hashes_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            documents, chunks = write_corpus(root)
            artifact = root / "parsed/doc/image.png"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"image")
            documents[0].update(artifacts="parsed/doc", artifact_files_sha256={"image.png": file_hash(artifact)})
            decision = root / "tables/doc/table_decisions.json"
            decision.parent.mkdir(parents=True)
            decision.write_bytes(b"{}")
            documents[0].update(table_decision_artifact="tables/doc/table_decisions.json", table_decision_sha256=file_hash(decision))
            chunks[0]["asset_refs"] = [{"path": "image.png", "artifact_dir": "parsed/doc"}]
            write_corpus(root, documents=documents, chunks=chunks)
            snapshot = load_snapshot(root)
            target = snapshot.publish(root / "storage")
            self.assertEqual((target / "parsed/doc/image.png").read_bytes(), b"image")
            self.assertEqual((target / "tables/doc/table_decisions.json").read_bytes(), b"{}")
            artifact.write_bytes(b"wrong")
            with self.assertRaisesRegex(ValueError, "artifact checksum"):
                load_snapshot(root)


if __name__ == "__main__":
    unittest.main()
