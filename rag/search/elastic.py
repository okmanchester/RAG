"""Basic-license exact vector/BM25 search and immutable generation publication."""
from __future__ import annotations

from functools import wraps
import re

from elasticsearch import ApiError, ConnectionError, ConnectionTimeout, NotFoundError
from elasticsearch.helpers import streaming_bulk, scan

from rag.management.errors import ManagementError
from .shared import digest

RETRIEVAL_CONFIG = {"version": 1, "vector": "exact_cosine_float32", "analyzer": "cjk",
                    "candidate_window": 50, "rrf_constant": 60, "default_mode": "dense"}
TRANSIENT_STATUSES = (408, 429, 500, 502, 503, 504)


def safe_es(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except (ConnectionError, ConnectionTimeout):
            raise ManagementError("SEARCH_UNAVAILABLE", retryable=True) from None
        except ApiError as exc:
            raise ManagementError("SEARCH_ERROR", retryable=exc.status_code in TRANSIENT_STATUSES) from None
    return call


def rrf(dense, lexical):
    merged = {}
    for name, hits in (("dense", dense), ("bm25", lexical)):
        for rank, hit in enumerate(hits, 1):
            item = merged.setdefault(hit["_id"], {**hit, "_score": 0.0, "ranks": {}})
            if name not in item["ranks"]:
                item["_score"] += 1.0 / (60 + rank)
                item["ranks"][name] = rank
    return sorted(merged.values(), key=lambda h: (-h["_score"], h["_source"]["legacy_chunk_id"], int(h["_id"])))


class ESStore:
    def __init__(self, client, namespace, prefix="rag"):
        self.client = client
        self.namespace = namespace
        self.prefix = prefix

    def alias(self, kb_id):
        return f"{self.prefix}-kb-{self.namespace}-{kb_id}"

    def generation_name(self, kb_id, generation):
        if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise ManagementError("INVALID_GENERATION")
        return f"{self.alias(kb_id)}-g-{generation}"

    @safe_es
    def current(self, kb_id):
        try:
            indexes = self.client.indices.get_alias(name=self.alias(kb_id))
        except NotFoundError:
            return None
        if len(indexes) != 1:
            raise ManagementError("INDEX_CORRUPT")
        return next(iter(indexes))

    @safe_es
    def metadata(self, index):
        try:
            response = self.client.indices.get_mapping(index=index)
        except NotFoundError:
            raise ManagementError("INDEX_NOT_FOUND") from None
        if len(response) != 1:
            raise ManagementError("INDEX_CORRUPT")
        meta = response[index]["mappings"].get("_meta", {})
        if (meta.get("namespace") != self.namespace or meta.get("format_version") != 1 or
                index != self.generation_name(meta.get("kb_id"), meta.get("generation"))):
            raise ManagementError("INDEX_CORRUPT")
        return meta

    @safe_es
    def create(self, index, metadata):
        properties = {name: {"type": "keyword"} for name in
                      ("legacy_doc_id", "legacy_chunk_id", "document_sha256", "category", "structure_type", "payload_sha256")}
        properties.update({name: {"type": "unsigned_long"} for name in ("chunk_id", "kb_id", "document_id", "build_id")})
        properties.update(text={"type": "text", "analyzer": "cjk"},
                          embedding={"type": "dense_vector", "dims": metadata["model"]["dimensions"],
                                     "element_type": "float", "index": False})
        self.client.indices.create(index=index, settings={"number_of_shards": 1, "number_of_replicas": 0},
                                   mappings={"dynamic": "strict", "_meta": metadata, "properties": properties})

    @safe_es
    def bulk(self, index, documents):
        failed = 0
        transient_only = True
        count = 0
        actions = ({"_op_type": "index", "_index": index, "_id": str(doc["chunk_id"]), "_source": doc}
                   for doc in documents)
        for ok, item in streaming_bulk(self.client, actions, chunk_size=128, max_chunk_bytes=4 * 1024 * 1024,
                                     raise_on_error=False, max_retries=2, initial_backoff=1, max_backoff=4):
            count += int(ok)
            failed += int(not ok)
            if not ok:
                transient_only = transient_only and all(v.get("status") in TRANSIENT_STATUSES for v in item.values())
        if failed:
            raise ManagementError("INDEX_BULK_FAILED", retryable=transient_only)
        return count

    @safe_es
    def verify(self, index, expected):
        self.client.indices.refresh(index=index)
        if self.client.count(index=index)["count"] != len(expected):
            raise ManagementError("INDEX_CORRUPT")
        seen = {}
        for hit in scan(self.client, index=index, query={"query": {"match_all": {}},
                                                       "_source": {"excludes": ["embedding"]}}, size=128):
            seen[hit["_id"]] = digest(hit["_source"])
        if seen != expected:
            raise ManagementError("INDEX_CORRUPT")

    @safe_es
    def seal(self, index, metadata):
        self.client.indices.put_mapping(index=index, meta=metadata)
        self.client.indices.put_settings(index=index, settings={"index.blocks.write": True})

    @safe_es
    def validate(self, kb_id, index):
        metadata = self.metadata(index)
        if metadata.get("state") != "ready" or metadata.get("kb_id") != kb_id:
            raise ManagementError("INDEX_NOT_READY")
        settings = self.client.indices.get_settings(index=index)[index]["settings"]["index"]
        if settings.get("blocks", {}).get("write") != "true":
            raise ManagementError("INDEX_NOT_READY")
        # Recheck a rollback candidate too: count alone cannot detect changed identities/text.
        self.client.indices.refresh(index=index)
        records = {hit["_id"]: digest(hit["_source"]) for hit in scan(self.client, index=index,
                   query={"query": {"match_all": {}}, "_source": {"excludes": ["embedding"]}}, size=128)}
        if len(records) != metadata["chunk_count"] or digest(records) != metadata.get("records_sha256"):
            raise ManagementError("INDEX_CORRUPT")

    @safe_es
    def publish(self, kb_id, index, *, validated=False):
        if not validated:
            self.validate(kb_id, index)
        current = self.current(kb_id)
        if current == index:
            return
        actions = [] if current is None else [{"remove": {"index": current, "alias": self.alias(kb_id), "must_exist": True}}]
        actions.append({"add": {"index": index, "alias": self.alias(kb_id)}})
        # An interrupted response may still mean success. The alias is the authority.
        try:
            response = self.client.indices.update_aliases(actions=actions)
            if not response.get("acknowledged") or response.get("errors"):
                if self.current(kb_id) != index:
                    raise ManagementError("INDEX_PUBLISH_FAILED", retryable=True)
        except (ConnectionError, ConnectionTimeout):
            if self.current(kb_id) != index:
                raise ManagementError("INDEX_PUBLISH_FAILED", retryable=True) from None

    @safe_es
    def generations(self, kb_id):
        response = self.client.indices.get_mapping(index=f"{self.alias(kb_id)}-g-*", allow_no_indices=True)
        return sorted((value["mappings"].get("_meta", {}) for value in response.values()),
                      key=lambda meta: meta.get("created_at", ""), reverse=True)

    @safe_es
    def search(self, index, question, vector, build_ids, filters, mode, size):
        clauses = [{"term": {"kb_id": filters.kb_id}}, {"terms": {"build_id": build_ids}}]
        for field in ("category", "structure_type"):
            if getattr(filters, field) is not None:
                clauses.append({"term": {field: getattr(filters, field)}})
        selected = {"bool": {"filter": clauses}}
        if mode == "dense":
            query = {"script_score": {"query": selected,
                "script": {"source": "cosineSimilarity(params.query_vector, 'embedding') + 1.0",
                           "params": {"query_vector": vector.tolist()}}}}
        else:
            query = {"bool": {"filter": clauses, "must": [{"match": {"text": question}}]}}
        response = self.client.search(index=index, query=query, size=size,
            source=["kb_id", "build_id", "payload_sha256", "legacy_chunk_id"],
            sort=[{"_score": "desc"}, {"legacy_chunk_id": "asc"}, {"chunk_id": "asc"}])
        if response.get("timed_out") or response["_shards"].get("failed", 0):
            raise ManagementError("SEARCH_ERROR")
        return response["hits"]["hits"]
