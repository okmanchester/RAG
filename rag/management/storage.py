"""Validate existing corpora and publish immutable, content-addressed files."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import ManagementError


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_hash(path: Path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative_path(value: str):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ManagementError('INVALID_PATH')
    value = unicodedata.normalize("NFC", value.replace("\\", "/"))
    if value.startswith("/") or ":" in value or ".." in value.split("/"):
        raise ManagementError('UNSAFE_PATH')
    value = PurePosixPath(value).as_posix()
    if value == "." or len(value) > 1024:
        raise ManagementError('PATH_TOO_LONG')
    return value


def contained(root: Path, relative: str):
    root = root.resolve()
    path = (root / relative_path(relative)).resolve()
    if not path.is_relative_to(root):
        raise ManagementError('PATH_ESCAPE')
    return path


def _identity(value, field):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ManagementError("INVALID_" + field.upper())


def _integer(value, field, minimum=0):
    if type(value) is not int or not minimum <= value <= 4294967295:
        raise ManagementError("INVALID_" + field.upper())


@dataclass
class Snapshot:
    source: Path
    documents: list[dict]
    chunks_by_doc: dict[str, list[dict]]
    files: dict[str, bytes]
    hashes: dict[str, str]
    fingerprint: str
    missing_assets: dict[str, list[str]]

    def publish(self, storage_root: Path):
        root = storage_root.resolve() / "snapshots"
        root.mkdir(parents=True, exist_ok=True)
        target = root / self.fingerprint
        if not target.exists():
            with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=root) as temporary:
                staged = Path(temporary)
                for name, content in self.files.items():
                    path = contained(staged, name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                try:
                    staged.rename(target)
                except FileExistsError:
                    pass
        for name, expected in self.hashes.items():
            if file_hash(contained(target, name)) != expected:
                raise ManagementError('STORED_SNAPSHOT_CHECKSUM')
        return target


def load_snapshot(source: Path):
    source = source.resolve()
    files = {name: (source / name).read_bytes() for name in ("documents.jsonl", "chunks.jsonl")}
    for name in ("manifest.json", "report.json", "verification.json"):
        if (source / name).is_file():
            files[name] = (source / name).read_bytes()
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    declarations = []
    if "manifest.json" in files:
        manifest = json.loads(files["manifest.json"])
        declarations.append(manifest.get("corpus") or manifest.get("artifacts_sha256", {}))
    if "report.json" in files:
        declarations.append(json.loads(files["report.json"]).get("artifacts_sha256", {}))
    if not declarations:
        raise ManagementError('MANIFEST_REQUIRED')
    for declared in declarations:
        for name in ("documents.jsonl", "chunks.jsonl"):
            if declared.get(name) != hashes[name]:
                raise ManagementError('SNAPSHOT_CHECKSUM')
    documents = [json.loads(line) for line in files["documents.jsonl"].decode("utf-8-sig").splitlines() if line.strip()]
    chunks = [json.loads(line) for line in files["chunks.jsonl"].decode("utf-8-sig").splitlines() if line.strip()]
    if not documents:
        raise ManagementError('EMPTY_CORPUS')
    doc_ids, paths, chunk_ids = set(), set(), set()
    mapping = {}
    for doc in documents:
        _identity(doc.get("doc_id"), "doc_id")
        path = relative_path(doc.get("source_path"))
        if doc["doc_id"] in doc_ids or path in paths:
            raise ManagementError('DUPLICATE_DOCUMENT')
        doc_ids.add(doc["doc_id"])
        paths.add(path)
        if not re.fullmatch(r"[0-9a-f]{64}", doc.get("sha256", "")):
            raise ManagementError('INVALID_SOURCE_HASH')
        if doc.get("format") not in ("pdf", "docx") or PurePosixPath(path).suffix.lower() != "." + doc["format"]:
            raise ManagementError('INVALID_DOCUMENT_FORMAT')
        if doc.get("status") not in ("ok", "failed", "empty"):
            raise ManagementError('INVALID_DOCUMENT_STATUS')
        if not isinstance(doc.get("blocks"), list):
            raise ManagementError('MISSING_BLOCKS')
        blocks = {b["block_id"]: b for b in doc["blocks"]}
        if len(blocks) != len(doc["blocks"]):
            raise ManagementError('DUPLICATE_BLOCK')
        mapping[doc["doc_id"]] = (doc, blocks)
    grouped = defaultdict(list)
    positions = defaultdict(set)
    for chunk in chunks:
        _identity(chunk.get("chunk_id"), "chunk_id")
        if chunk["chunk_id"] in chunk_ids:
            raise ManagementError('DUPLICATE_CHUNK')
        chunk_ids.add(chunk["chunk_id"])
        if chunk.get("doc_id") not in mapping:
            raise ManagementError('ORPHAN_CHUNK')
        doc, blocks = mapping[chunk["doc_id"]]
        if relative_path(chunk.get("source_path")) != relative_path(doc["source_path"]):
            raise ManagementError('CHUNK_SOURCE_MISMATCH')
        if type(chunk.get("retrieval_enabled")) is not bool:
            raise ManagementError('INVALID_RETRIEVAL_FLAG')
        for field in ("body", "text"):
            if not isinstance(chunk.get(field), str) or not chunk[field].strip():
                raise ManagementError("INVALID_CHUNK_" + field.upper())
        structure = chunk.get("structure_type", chunk.get("content_type", "body"))
        if not isinstance(structure, str) or not structure.strip() or len(structure) > 64:
            raise ManagementError('INVALID_STRUCTURE')
        _integer(chunk.get("token_count"), "token_count")
        _integer(chunk.get("chunk_index"), "chunk_index", 1)
        if chunk["chunk_index"] in positions[chunk["doc_id"]]:
            raise ManagementError('DUPLICATE_CHUNK_POSITION')
        positions[chunk["doc_id"]].add(chunk["chunk_index"])
        if not isinstance(chunk.get("sources"), list) or not chunk["sources"]:
            raise ManagementError('MISSING_PROVENANCE')
        refs = [*chunk["sources"], *chunk.get("context_sources", [])]
        refs += [ref for derived in chunk.get("derived_sources", []) for ref in derived.get("source_refs", [])]
        for ref in refs:
            if ref.get("block_id") not in blocks:
                raise ManagementError('UNKNOWN_SOURCE_BLOCK')
            start, end = ref.get("block_char_start"), ref.get("block_char_end")
            if start is not None or end is not None:
                if not (type(start) is int and type(end) is int and 0 <= start < end <= len(blocks[ref["block_id"]]["text"])):
                    raise ManagementError('INVALID_SOURCE_INTERVAL')
        grouped[chunk["doc_id"]].append(chunk)
    missing = {}
    for doc in documents:
        values = sorted(grouped[doc["doc_id"]], key=lambda c: c["chunk_index"])
        grouped[doc["doc_id"]] = values
        if doc.get("chunk_count", len(values)) != len(values):
            raise ManagementError('CHUNK_COUNT_MISMATCH')
        if doc["status"] != "ok" and values:
            raise ManagementError('FAILED_DOCUMENT_CHUNKS')
        asset_dir = doc.get("artifacts")
        declared_assets = doc.get("artifact_files_sha256", {})
        if not declared_assets and isinstance(doc.get("parser"), dict):
            declared_assets = doc["parser"].get("artifact_files_sha256", {})
        names = set()
        extra_hashes = {}
        if asset_dir:
            asset_dir = relative_path(asset_dir)
            folder = contained(source, asset_dir)
            if folder.is_dir():
                names.update(p.relative_to(source).as_posix() for p in folder.rglob("*") if p.is_file())
            names.update(relative_path(asset_dir + "/" + name) for name in declared_assets)
        decision_path = doc.get("table_decision_artifact")
        if decision_path:
            decision_path = relative_path(decision_path)
            names.add(decision_path)
            if doc.get("table_decision_sha256"):
                extra_hashes[decision_path] = doc["table_decision_sha256"]
        for chunk in values:
            for asset in chunk.get("asset_refs", []):
                if isinstance(asset, dict) and asset.get("artifact_dir") and asset.get("path"):
                    names.add(relative_path(asset["artifact_dir"] + "/" + asset["path"]))
        missing[doc["doc_id"]] = []
        if asset_dir and not contained(source, asset_dir).is_dir():
            missing[doc["doc_id"]].append(asset_dir + "/")
        for name in sorted(names):
            path = contained(source, name)
            if not path.is_file():
                if name in extra_hashes or (asset_dir and name.startswith(asset_dir + "/") and name[len(asset_dir) + 1:] in declared_assets):
                    raise ManagementError('MISSING_DECLARED_ASSET')
                missing[doc["doc_id"]].append(name)
                continue
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if name in extra_hashes and extra_hashes[name] != digest:
                raise ManagementError('DERIVED_ASSET_CHECKSUM')
            if asset_dir and name.startswith(asset_dir + "/"):
                expected = declared_assets.get(name[len(asset_dir) + 1:])
                if expected and expected != digest:
                    raise ManagementError('ASSET_CHECKSUM')
            files[name], hashes[name] = content, digest
    return Snapshot(source, documents, dict(grouped), files, hashes, fingerprint(hashes), missing)


def publish_raw(raw_root: Path, source_path: str, expected: str, storage_root: Path):
    source = contained(raw_root, source_path)
    root = storage_root.resolve() / "raw"
    root.mkdir(parents=True, exist_ok=True)
    target = root / expected
    if not source.is_file():
        raise ManagementError('RAW_FILE_MISSING')
    # Always verify the supplied input, including repeat imports.
    with tempfile.TemporaryDirectory(prefix=".raw-", dir=root) as temporary:
        staged = Path(temporary) / "content"
        shutil.copyfile(source, staged)
        if file_hash(staged) != expected:
            raise ManagementError('RAW_CHECKSUM')
        if not target.exists():
            try:
                staged.rename(target)
            except FileExistsError:
                pass
    if file_hash(target) != expected:
        raise ManagementError('STORED_RAW_CHECKSUM')
    return target
