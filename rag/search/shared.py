"""Embedding and provenance shared by release and managed retrieval."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MODEL_PATH = "D:/WorkSpace/AIModels/BAAI/bge-large-zh-v1.5"
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def embedding_identity(model_dir):
    model_dir = Path(model_dir)
    if not model_dir.is_dir():
        raise FileNotFoundError(f"Embedding model unavailable: {model_dir}")
    weight = model_dir / "pytorch_model.bin"
    if not weight.is_file():
        raise FileNotFoundError(f"Embedding weights unavailable: {weight}")
    return {"model_path": model_dir.as_posix(), "model_config_sha256": file_hash(model_dir / "config.json"),
            "model_weights_sha256": file_hash(weight), "query_instruction": QUERY_INSTRUCTION}


def load_encoder(model_dir):
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(str(model_dir), local_files_only=True)


def query_embedding(encoder, question):
    return encoder.encode([QUERY_INSTRUCTION + question], normalize_embeddings=True,
                          show_progress_bar=False)[0].astype("float32")


class BGEEncoder:
    def __init__(self, model_dir=MODEL_PATH):
        self.model_dir = Path(model_dir)
        self._model = None
        self._identity = None

    @property
    def identity(self):
        if self._identity is None:
            self._identity = {**embedding_identity(self.model_dir), "normalize_embeddings": True,
                              "dimensions": json.loads((self.model_dir / "config.json").read_text())["hidden_size"]}
        return self._identity

    @property
    def model(self):
        if self._model is None:
            self._model = load_encoder(self.model_dir)
        return self._model

    def documents(self, texts):
        return self.model.encode(texts, batch_size=16, normalize_embeddings=True,
                                 show_progress_bar=False).astype("float32")

    def query(self, question):
        return query_embedding(self.model, question)


def context(chunk, document_sha256, *, identifier=None):
    derived = [dict(source) for mapping in chunk.get("derived_sources", []) for source in mapping["source_refs"]]
    return {"id": identifier or chunk["chunk_id"], "text": chunk["text"], "doc_id": chunk["doc_id"],
            "document_sha256": document_sha256,
            "sources": [dict(s) for s in [*chunk["sources"], *chunk.get("context_sources", []), *derived]],
            "derived_sources": chunk.get("derived_sources", []),
            "structure_type": chunk.get("structure_type", "body"), "asset_refs": chunk.get("asset_refs", [])}


@dataclass(frozen=True)
class SearchFilters:
    kb_id: int | None = None
    category: str | None = None
    structure_type: str | None = None


class Retriever(Protocol):
    def search(self, question: str, top_k: int = 10, filters: SearchFilters | None = None) -> list[dict]: ...
