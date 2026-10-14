"""Environment-backed MySQL configuration without importing model dependencies."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ManagementError

from dotenv import dotenv_values
from sqlalchemy import URL, create_engine, event

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    database: str
    user: str
    password: str
    storage_root: Path

    @classmethod
    def from_env(cls, env_file: Path | str = ROOT / ".env.mysql"):
        values = {**dotenv_values(env_file, interpolate=False), **os.environ}
        password = values.get("MYSQL_PASSWORD")
        if not password:
            raise ManagementError('MYSQL_PASSWORD_REQUIRED')
        storage = Path(values.get("RAG_STORAGE_ROOT") or "data/storage")
        return cls(values.get("MYSQL_HOST") or "127.0.0.1", int(values.get("MYSQL_PORT") or 3306),
                   values.get("MYSQL_DATABASE") or "rag", values.get("MYSQL_USER") or "rag_app",
                   password, storage if storage.is_absolute() else ROOT / storage)

    @property
    def url(self):
        return URL.create("mysql+pymysql", username=self.user, password=self.password,
                          host=self.host, port=self.port, database=self.database,
                          query={"charset": "utf8mb4"})

    def engine(self):
        engine = create_engine(self.url, pool_pre_ping=True, pool_recycle=1800,
                               connect_args={"connect_timeout": 10}, hide_parameters=True)

        @event.listens_for(engine, "connect")
        def configure_session(connection, _record):
            with connection.cursor() as cursor:
                cursor.execute("SET time_zone = '+00:00'")
                cursor.execute("SET SESSION sql_mode = 'STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'")
        return engine


def migration_config(connection=None):
    from alembic.config import Config
    config = Config(str(ROOT / "alembic.ini"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config
