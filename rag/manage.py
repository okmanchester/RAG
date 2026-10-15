"""CLI for MySQL knowledge-base management: python -m rag.manage --help."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from .management.config import ROOT, Settings, migration_config
from .management.service import ManagementService, STATES, ITEM_STATES
from .management.errors import public_error


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--env-file", type=Path, default=ROOT / ".env.mysql")
    root.add_argument("--es-env-file", type=Path, default=ROOT / ".env.elasticsearch")
    groups = root.add_subparsers(dest="group", required=True)
    for name, actions in {"db": ("check", "upgrade", "ddl"), "kb": ("create", "list", "show", "update", "status"),
                          "document": ("list", "show", "status", "versions", "builds", "activate", "register", "process"),
                          "corpus": ("import",), "import": ("show", "items", "activate"), "chunk": ("list", "show")}.items():
        group = groups.add_parser(name)
        subs = group.add_subparsers(dest="action", required=True)
        for action in actions:
            command = subs.add_parser(action)
            if name == "kb" and action == "create":
                command.add_argument("--code", required=True)
                command.add_argument("--name", required=True)
                command.add_argument("--description", default="")
            if name == "kb" and action in ("show", "update", "status"):
                command.add_argument("--kb-id", type=int, required=True)
            if name == "kb" and action == "update":
                command.add_argument("--name")
                command.add_argument("--description")
            if name == "document":
                identifier = "--version-id" if action == "process" else "--kb-id" if action in ("list", "register") else "--document-id"
                command.add_argument(identifier, type=int, required=True)
                if action == "register":
                    command.add_argument("--raw-root", type=Path, required=True)
                    command.add_argument("--source-path", required=True)
                if action == "process":
                    command.add_argument("--model-path", type=Path)
                    command.add_argument("--mineru-config", type=Path)
                    command.add_argument("--max-tokens", type=int, default=400)
                    command.add_argument("--overlap-tokens", type=int, default=50)
                if action == "activate":
                    command.add_argument("--build-id", type=int, required=True)
            if action == "status":
                command.add_argument("--status", choices=STATES, required=True)
            if name == "import":
                command.add_argument("--import-id", type=int, required=True)
                if action == "items":
                    command.add_argument("--status", choices=ITEM_STATES)
            if name == "corpus":
                command.add_argument("--kb-id", type=int, required=True)
                command.add_argument("--source", type=Path, required=True)
                command.add_argument("--raw-root", type=Path, default=ROOT / "data/raw")
            if name == "chunk" and action == "show":
                command.add_argument("--chunk-id", type=int, required=True)
            if name == "chunk" and action == "list":
                command.add_argument("--document-id", type=int)
                command.add_argument("--build-id", type=int)
                command.add_argument("--eligible-only", action="store_true")
            if action in ("list", "versions", "builds") or (name == "import" and action in ("show", "items")):
                command.add_argument("--limit", type=int, default=100)
                command.add_argument("--offset", type=int, default=0)
                if name in ("kb", "document") and action == "list":
                    command.add_argument("--status", choices=STATES)
    task = groups.add_parser("task")
    actions = task.add_subparsers(dest="action", required=True)
    for action in ("list", "show", "retry"):
        command = actions.add_parser(action)
        command.add_argument("--kb-id" if action == "list" else "--task-id", type=int, required=True)
        if action != "retry":
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--offset", type=int, default=0)
        if action == "list":
            command.add_argument("--status", choices=("pending", "running", "succeeded", "failed"))
    worker = groups.add_parser("worker")
    actions = worker.add_subparsers(dest="action", required=True)
    command = actions.add_parser("run")
    command.add_argument("--once", action="store_true")
    command.add_argument("--kind", choices=("all", "processing", "index"), default="all")
    index = groups.add_parser("index")
    actions = index.add_subparsers(dest="action", required=True)
    for action in ("sync", "status", "rollback", "retry", "pause", "resume"):
        command = actions.add_parser(action)
        command.add_argument("--kb-id", type=int, required=True)
        if action == "rollback":
            command.add_argument("--generation", required=True)
        if action == "status":
            command.add_argument("--db-only", action="store_true")
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--offset", type=int, default=0)
    search = groups.add_parser("search")
    search.set_defaults(action="query")
    search.add_argument("--kb-id", type=int, required=True)
    search.add_argument("--query", required=True)
    search.add_argument("--top-k", type=int, default=10)
    search.add_argument("--mode", choices=("dense", "bm25", "hybrid"), help="default: generation's validated mode (initially dense)")
    search.add_argument("--category")
    search.add_argument("--structure-type")
    return root


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds") + "Z"
    raise TypeError(type(value).__name__)


def dispatch(service, args):
    group, action = args.group, args.action
    if group in ("task", "worker") or group == "document" and action in ("register", "process"):
        from .management.processing import ProcessingService
        processing = ProcessingService(service)
        if group == "worker":
            from .management.worker import run_worker
            return run_worker(service, once=args.once, kind=args.kind, es_env_file=args.es_env_file)
        if group == "document":
            if action == "register":
                return processing.register(args.kb_id, args.raw_root, args.source_path)
            return processing.submit(args.version_id, model_path=args.model_path, maximum=args.max_tokens,
                                     overlap=args.overlap_tokens, config_path=args.mineru_config)
        if action == "show":
            return processing.show(args.task_id, args.limit, args.offset)
        if action == "retry":
            return processing.retry(args.task_id)
        return processing.list(args.kb_id, args.status, args.limit, args.offset)
    if group in ("index", "search"):
        if group == "index":
            from .management.index_sync import IndexSyncService
            indexing = IndexSyncService(service.engine)
            if action in ("pause", "resume", "retry"):
                return indexing.control(args.kb_id, action)
            if action == "status" and args.db_only:
                return indexing.show(args.kb_id, limit=args.limit, offset=args.offset)
        from .search.config import SearchSettings
        from .search.service import SearchService
        from .search.shared import SearchFilters
        config = SearchSettings.from_env(args.es_env_file)
        with config.client() as client:
            search = SearchService(service.engine, client, prefix=config.prefix)
            if group == "search":
                return search.query(args.query, args.top_k,
                    SearchFilters(args.kb_id, args.category, args.structure_type), mode=args.mode)
            if action == "sync":
                return search.sync(args.kb_id)
            if action == "rollback":
                return search.rollback(args.kb_id, args.generation)
            return search.status(args.kb_id, limit=args.limit, offset=args.offset)
    if group == "db":
        return service.db_upgrade() if action == "upgrade" else service.db_check()
    if group == "kb":
        if action == "create":
            return service.kb_create(args.code, args.name, args.description)
        if action == "update":
            return service.kb_update(args.kb_id, name=args.name, description=args.description)
        if action == "status":
            return service.set_status("kb", args.kb_id, args.status)
        if action == "show":
            return service.show("kb", args.kb_id)
        return service.list_records("kb", status=args.status, limit=args.limit, offset=args.offset)
    if group == "document":
        if action == "list":
            return service.list_records("document", kb_id=args.kb_id, status=args.status, limit=args.limit, offset=args.offset)
        if action == "show":
            return service.show("document", args.document_id)
        if action == "status":
            return service.set_status("document", args.document_id, args.status)
        if action == "activate":
            return service.activate_document(args.document_id, args.build_id)
        return service.list_records("version" if action == "versions" else "build", document_id=args.document_id,
                                    limit=args.limit, offset=args.offset)
    if group == "corpus":
        return service.import_corpus(args.kb_id, args.source, args.raw_root)
    if group == "import":
        if action == "show":
            return service.show_import(args.import_id, limit=args.limit, offset=args.offset)
        if action == "items":
            return service.import_items(args.import_id, limit=args.limit, offset=args.offset, status=args.status)
        return service.activate_import(args.import_id)
    if action == "show":
        return service.show("chunk", args.chunk_id)
    return service.list_records("chunk", document_id=args.document_id, build_id=args.build_id,
                                eligible_only=args.eligible_only, limit=args.limit, offset=args.offset)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    engine = None
    try:
        if args.group == "worker":
            from .ops.events import start_logging
            start_logging("worker")
        if args.group == "db" and args.action == "ddl":
            from alembic import command
            command.upgrade(migration_config(), "head", sql=True)
            return 0
        settings = Settings.from_env(args.env_file)
        engine = settings.engine()
        service = ManagementService(engine, settings.storage_root)
        if args.group != "db":
            service.db_check()
        result = dispatch(service, args)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=json_default))
        return 1 if (args.group == "corpus" and result["status"] != "succeeded" or
                     args.group == "worker" and result["status"] in ("failed", "pending", "running")) else 0
    except Exception as exc:
        print(json.dumps(public_error(exc), ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
