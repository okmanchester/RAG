"""Run the management demo: python -m rag.demo (MySQL/ES started manually)."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time
import uuid

from rag.management.config import ROOT, Settings
from rag.management.errors import ManagementError, public_error
from rag.management.service import ManagementService
from rag.ops.process import Child
from rag.ops.state import atomic_json, exclusive, identity, now, read_object, state_root


def check_dependencies(settings):
    """Read-only preflight: no Docker commands, migrations, or model loading."""
    print("检查 MySQL 和迁移版本（需要手动启动）…", flush=True)
    engine = settings.engine()
    try:
        ManagementService(engine, settings.storage_root).db_check()
    finally:
        engine.dispose()
    print("检查 Elasticsearch（需要手动启动）…", flush=True)
    from rag.search.config import SearchSettings
    from rag.search.elastic import safe_es
    with SearchSettings.from_env().client() as client:
        safe_es(client.options(request_timeout=3, max_retries=0).info)()
    # Fail before starting a worker when a manually launched API owns the port.
    with socket.socket() as listener:
        if os.name == "nt":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            listener.bind(("127.0.0.1", 8000))
        except OSError:
            raise ManagementError("RUNTIME_BUSY") from None
    return {name: {"state": "available", "checked_at": now()}
            for name in ("mysql", "elasticsearch")}


def run_demo():
    if os.name != "nt":
        raise ManagementError("RUNTIME_NOT_AVAILABLE")
    settings = Settings.from_env()
    root = state_root()
    # Share ownership with the optional supervisor; never run two launchers.
    with exclusive(root / "supervisor.lock"):
        dependencies = check_dependencies(settings)
        instance = uuid.uuid4().hex
        status = {"identity": identity(settings), "instance": instance, "mode": "demo",
                  "supervisor": "running", "dependencies": dependencies,
                  "api": {"state": "waiting"}, "worker": {"state": "waiting"}}
        children = {}
        python = str(Path(sys.executable).with_name("python.exe"))
        try:
            for name, module in (("api", ["rag.api"]), ("worker", ["rag.manage", "worker", "run"])):
                children[name] = Child([python, "-m", *module], cwd=ROOT)
                status[name] = {"state": "running", "restart_count": 0}
            print("演示已启动：http://127.0.0.1:8000/；Ctrl+C 停止 API 和 worker。", flush=True)
            next_status = 0.
            while True:
                for name, child in children.items():
                    code = child.poll()
                    if code is not None:
                        status[name].update(state="stopped", last_exit_code=code)
                        print(json.dumps({"component": name, "exit_code": code,
                                          "error_code": "RUNTIME_COMMAND_FAILED"}), file=sys.stderr)
                        return 1  # No restart/backoff: stop the other component too.
                stop = read_object(root / "stop.json") if (root / "stop.json").is_file() else None
                if stop is not None and stop.get("instance") == instance:
                    return 0
                if time.monotonic() >= next_status:
                    status["updated_at"] = now()
                    atomic_json(root / "status.json", status)
                    next_status = time.monotonic() + 5
                time.sleep(.5)
        except KeyboardInterrupt:
            return 0
        finally:
            # Kill all owned trees before waiting, using one bounded shutdown budget.
            failures = []
            for child in children.values():
                try:
                    child.terminate()
                except OSError as exc:
                    failures.append(exc)
            deadline = time.monotonic() + 15
            for name, child in children.items():
                try:
                    child.stop(timeout=max(0, deadline - time.monotonic()))
                    status[name]["state"] = "stopped"
                except Exception as exc:
                    failures.append(exc)
                    status[name]["state"] = "unknown"
            status.update(supervisor="stopped", updated_at=now())
            atomic_json(root / "status.json", status)
            if failures:
                raise ManagementError("RUNTIME_COMMAND_FAILED") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stop", action="store_true", help="Stop the current launcher without touching scheduled tasks")
    args = parser.parse_args(argv)
    try:
        if args.stop:
            from rag.ops.__main__ import stop
            print(json.dumps(stop(), ensure_ascii=False))
            return 0
        return run_demo()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(json.dumps(public_error(exc), ensure_ascii=False), file=sys.stderr)
        print("请手动启动 MySQL/Elasticsearch 并检查配置；demo 不会启动 Docker 或自动升级数据库。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
