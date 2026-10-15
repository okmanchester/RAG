"""Per-process resources and lazily initialized search."""
from threading import RLock

from rag.management.config import ROOT
from rag.management.index_sync import IndexSyncService
from rag.management.processing import ProcessingService

from .errors import APIError


class Runtime:
    def __init__(self, management, *, processing=None, search_factory=None, es_env_file=None,
                 processing_options=None):
        self.management = management
        self.processing = processing or ProcessingService(management)
        self.indexing = IndexSyncService(management.engine)
        self.processing_options = dict(processing_options or {})
        self.es_env_file = es_env_file or ROOT / ".env.elasticsearch"
        self._search_factory = search_factory
        self._search = None
        self._client = None
        self._lock = RLock()

    def search(self):
        # Called under the same lock as query/status, including first model load and encoding.
        if self._search is None:
            if self._search_factory is not None:
                self._search = self._search_factory()
            else:
                from rag.search.config import SearchSettings
                from rag.search.service import SearchService
                settings = SearchSettings.from_env(self.es_env_file)
                client = settings.client()
                try:
                    search = SearchService(self.management.engine, client, prefix=settings.prefix)
                except BaseException:
                    client.close()
                    raise
                self._client, self._search = client, search
        return self._search

    def query(self, kb_id, body):
        from rag.search.shared import SearchFilters
        try:
            with self._lock:
                return self.search().query(body.query, body.top_k,
                    SearchFilters(kb_id, body.category, body.structure_type), mode=body.mode)
        except FileNotFoundError:
            raise APIError(503, "PROCESSING_MODELS_MISSING", "required local model files are missing") from None

    def index_status(self, kb_id, db_only, limit, offset):
        if db_only:
            return {**self.indexing.show(kb_id, limit=limit, offset=offset),
                    "index_checked": False, "sync_lag": None}
        with self._lock:
            return {**self.search().status(kb_id, limit=limit, offset=offset), "index_checked": True}

    def close(self):
        with self._lock:
            if self._client is not None:
                self._client.close()
            self._client, self._search = None, None
