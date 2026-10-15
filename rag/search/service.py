"""Manual synchronization and queries guarded by current MySQL state."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import uuid

from rag.management.errors import ManagementError, public_error
from .corpus import ManagedCorpus
from .elastic import ESStore, RETRIEVAL_CONFIG, rrf
from .shared import BGEEncoder, SearchFilters, digest


class SearchService:
    def __init__(self, engine, client, *, encoder=None, prefix="rag", corpus=None, store=None):
        self.corpus = corpus or ManagedCorpus(engine)
        self.store = store or ESStore(client, self.corpus.namespace, prefix)
        self.encoder = encoder or BGEEncoder()

    def sync(self, kb_id):
        from rag.management.index_sync import IndexSyncService
        result = IndexSyncService(self.corpus.engine).sync(self, kb_id)
        return {**self.status(kb_id), "synced_generation": result["synced_generation"]}

    def build(self, selection, check=lambda: None, on_candidate=lambda meta: None):
        kb_id = selection.kb_id
        generation = uuid.uuid4().hex
        index = self.store.generation_name(kb_id, generation)
        metadata = {"format_version": 1, "namespace": self.corpus.namespace, "kb_id": kb_id,
            "generation": generation, "state": "building", "default_mode": "dense",
            "created_at": datetime.now(timezone.utc).isoformat(), "selection_sha256": selection.fingerprint,
            "builds": list(selection.builds), "model": self.encoder.identity,
            "retrieval": RETRIEVAL_CONFIG, "model_sha256": digest(self.encoder.identity),
            "retrieval_sha256": digest(RETRIEVAL_CONFIG), "chunk_count": 0}
        check()
        on_candidate(metadata)
        self.store.create(index, metadata)
        expected = {}
        content_hash = hashlib.sha256()
        batch = []

        def actions():
            def encode(rows):
                check()
                vectors = self.encoder.documents([row["text"] for row in rows])
                check()
                if vectors.shape != (len(rows), metadata["model"]["dimensions"]):
                    raise ManagementError("INVALID_EMBEDDING")
                import numpy as np
                if not np.isfinite(vectors).all() or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-4):
                    raise ManagementError("INVALID_EMBEDDING")
                for row, vector in zip(rows, vectors):
                    doc = self.corpus.projection(row, selection)
                    expected[str(row["id"])] = digest(doc)
                    content_hash.update(bytes.fromhex(digest(doc)))
                    yield {**doc, "embedding": vector.tolist()}
            for row in self.corpus.iter_chunks(selection):
                batch.append(row)
                if len(batch) == 128:
                    yield from encode(batch)
                    batch.clear()
            if batch:
                yield from encode(batch)

        try:
            check()
            count = self.store.bulk(index, actions())
            if count != len(expected) or count != sum(b["eligible_chunks"] for b in selection.builds):
                raise ManagementError("INDEX_CORRUPT")
            check()
            self.store.verify(index, expected)
            metadata.update(state="ready", chunk_count=count, content_sha256=content_hash.hexdigest(),
                            records_sha256=digest(expected))
            self.store.seal(index, metadata)
        except Exception as exc:
            try:
                self.store.seal(index, {**metadata, "state": "failed", **public_error(exc)})
            except Exception:
                pass  # Preserve the original failure, including unavailable ES.
            raise
        return index, metadata

    def status(self, kb_id, *, limit=100, offset=0):
        from rag.management.index_sync import IndexSyncService
        queue = IndexSyncService(self.corpus.engine).show(kb_id, limit=limit, offset=offset)
        selection = self.corpus.selection(kb_id, allow_disabled=True)
        index = self.store.current(kb_id)
        meta = self.store.metadata(index) if index else None
        return {"kb_id": kb_id, "kb_status": selection.status, "index": index,
                "generation": meta["generation"] if meta else None,
                "state": meta["state"] if meta else "missing", "chunk_count": meta["chunk_count"] if meta else 0,
                "sync_lag": meta is None or meta["selection_sha256"] != selection.fingerprint,
                "selection_sha256": selection.fingerprint, "manifest": meta,
                "generations": self.store.generations(kb_id), **queue}

    def rollback(self, kb_id, generation):
        with self.corpus.index_lock(kb_id):
            # Disabled KBs have an empty effective corpus. This is not validation
            # of the requested historical generation; store.validate below is.
            self.corpus.selection(kb_id, allow_disabled=True)
            index = self.store.generation_name(kb_id, generation)
            meta = self.store.metadata(index)
            if meta.get("state") != "ready":
                raise ManagementError("INDEX_NOT_READY")
            self.store.validate(kb_id, index)
            from rag.management.index_sync import IndexSyncService
            IndexSyncService(self.corpus.engine).control(kb_id, "pause", invalidate=True)
            self.store.publish(kb_id, index, validated=True)
            return self.status(kb_id)

    def query(self, question, top_k=10, filters=None, *, mode=None, vector=None):
        filters = filters or SearchFilters()
        if (not isinstance(question, str) or not question.strip() or type(top_k) is not int or
                not 1 <= top_k <= 50 or mode not in (None, "dense", "bm25", "hybrid") or filters.kb_id is None or
                any(value is not None and (not isinstance(value, str) or not value.strip())
                    for value in (filters.category, filters.structure_type))):
            raise ManagementError("INVALID_SEARCH")
        selection = self.corpus.selection(filters.kb_id)
        # Resolve once so both branches use one physical generation during concurrent publication.
        index = self.store.current(filters.kb_id)
        if index is None:
            raise ManagementError("INDEX_NOT_FOUND")
        metadata = self.store.metadata(index)
        if metadata.get("state") != "ready":
            raise ManagementError("INDEX_NOT_READY")
        mode = mode or metadata.get("default_mode", "dense")
        if mode not in ("dense", "bm25", "hybrid"):
            raise ManagementError("INDEX_CORRUPT")
        if mode != "bm25":
            if metadata["model_sha256"] != digest(self.encoder.identity):
                raise ManagementError("INDEX_MODEL_MISMATCH")
            vector = self.encoder.query(question) if vector is None else vector
            import numpy as np
            if vector.shape != (metadata["model"]["dimensions"],) or not np.isfinite(vector).all() or not np.any(vector):
                raise ManagementError("INVALID_EMBEDDING")
        candidates = []
        if selection.build_ids:
            dense = self.store.search(index, question, vector, selection.build_ids, filters, "dense", 50) if mode != "bm25" else []
            lexical = self.store.search(index, question, None, selection.build_ids, filters, "bm25", 50) if mode != "dense" else []
            candidates = rrf(dense, lexical) if mode == "hybrid" else dense if mode == "dense" else lexical
        contexts = self.corpus.hydrate(filters.kb_id, candidates)[:top_k]
        current = self.corpus.selection(filters.kb_id)
        contexts = [c for c in contexts if c["build_id"] in current.build_ids]
        return {"kb_id": filters.kb_id, "generation": metadata["generation"], "mode": mode,
                "sync_lag": metadata["selection_sha256"] != current.fingerprint, "contexts": contexts}

    def search(self, question, top_k=10, filters=None):
        return self.query(question, top_k, filters)["contexts"]
