"""Independent ES credentials, with environment variables taking precedence."""
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from rag.management.config import ROOT
from rag.management.errors import ManagementError


@dataclass(frozen=True)
class SearchSettings:
    url: str
    username: str
    password: str
    prefix: str = "rag"
    ca_certs: str | None = None

    @classmethod
    def from_env(cls, env_file=ROOT / ".env.elasticsearch"):
        values = {**dotenv_values(env_file, interpolate=False), **os.environ}
        password = values.get("ELASTIC_PASSWORD")
        if not password:
            raise ManagementError("ELASTIC_PASSWORD_REQUIRED")
        url = values.get("ELASTICSEARCH_URL") or "http://127.0.0.1:9200"
        parts = urlsplit(url)
        prefix = values.get("RAG_INDEX_PREFIX") or "rag"
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or
                parts.query or parts.fragment or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", prefix)):
            raise ManagementError("INVALID_SEARCH_CONFIG")
        if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ManagementError("INVALID_SEARCH_CONFIG")
        return cls(url, values.get("ELASTICSEARCH_USER") or "elastic", password, prefix,
                   values.get("ELASTICSEARCH_CA_CERTS") or None)

    def client(self):
        from elasticsearch import Elasticsearch
        return Elasticsearch(self.url, basic_auth=(self.username, self.password), ca_certs=self.ca_certs,
                             request_timeout=30, max_retries=2, retry_on_timeout=False)
