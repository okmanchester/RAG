"""One model worker, alternating durable processing and index tasks."""
from contextlib import ExitStack
import json
import time

from .processing import ProcessingService
from .index_sync import IndexSyncService
from rag.ops.events import event


def run_worker(management, *, once=False, kind="all", es_env_file=None, _processing=None):
    if kind not in ("all", "processing", "index"):
        from .errors import ManagementError
        raise ManagementError("INVALID_RESOURCE")
    processing = _processing if _processing is not None else ProcessingService(management)
    indexing = IndexSyncService(management.engine)
    preferred = "processing" if kind != "index" else "index"
    with processing.worker_lock() as check, ExitStack() as stack:
        search = None

        def search_factory():
            nonlocal search
            if search is None:
                from rag.search.config import SearchSettings
                from rag.search.service import SearchService
                config = SearchSettings.from_env() if es_env_file is None else SearchSettings.from_env(es_env_file)
                client = stack.enter_context(config.client())
                search = SearchService(management.engine, client, prefix=config.prefix)
            return search

        while True:
            check()
            order = (preferred, "index" if preferred == "processing" else "processing") if kind == "all" else (kind,)
            for selected in order:
                task = processing.claim() if selected == "processing" else indexing.claim()
                if task is None:
                    continue
                started = time.monotonic()
                task_identity = {"kind": selected, "attempt": task.get("attempt_count"),
                                 "kb_id": task.get("kb_id", task.get("knowledge_base_id"))}
                if selected == "processing":
                    task_identity["task_id"] = task["id"]
                else:
                    task_identity["revision"] = task.get("desired_revision")
                event("worker", "task_started", **task_identity)
                if selected == "processing":
                    result = {**processing.execute(task, check), "kind": "processing"}
                else:
                    result = indexing.execute(task, search_factory, check)
                preferred = "index" if selected == "processing" else "processing"
                event("worker", "task_completed", **task_identity,
                      status=result["status"], error_code=result.get("error_code"), seconds=round(time.monotonic() - started, 3))
                if once:
                    return result
                print(json.dumps({**task_identity,
                                  "status": result["status"], "error_code": result.get("error_code")}, ensure_ascii=False), flush=True)
                break
            else:
                if once:
                    return {"status": "idle"}
                time.sleep(5)
