"""Single local supervisor. Dependencies and children are observed independently."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from rag.management.config import ROOT, Settings
from rag.management.errors import ManagementError, public_error
from .events import event, start_logging
from .state import atomic_json, exclusive, identity, now, state_root

PYTHON = str(Path(sys.executable).with_name("python.exe")) if os.name == "nt" else sys.executable


def run(command, *, timeout=2, cancel=None):
    if cancel is not None:
        if cancel.is_set():
            return False
        from .process import Child
        child = Child(command, cwd=ROOT)
        try:
            deadline = time.monotonic() + timeout
            while child.poll() is None:
                if cancel.wait(.1):
                    return False
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
            return child.poll() == 0
        finally:
            child.stop(timeout=3)
    return subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0).returncode == 0


def probe(name):
    try:
        if name == "docker":
            available = run(["docker", "version", "--format", "{{.Server.Version}}"])
        elif name == "mysql":
            from sqlalchemy import create_engine, text
            engine = create_engine(Settings.from_env().url, hide_parameters=True,
                connect_args={"connect_timeout": 2, "read_timeout": 2, "write_timeout": 2})
            try:
                with engine.connect() as connection:
                    available = connection.scalar(text("SELECT 1")) == 1
            finally:
                engine.dispose()
        else:
            from rag.search.config import SearchSettings
            with SearchSettings.from_env().client() as client:
                health = client.options(request_timeout=2, max_retries=0).cluster.health(wait_for_status="yellow", timeout="1s")
                available = not health.get("timed_out") and health.get("status") in ("yellow", "green")
        return {"state": "available" if available else "unavailable", "checked_at": now()}
    except Exception:
        return {"state": "unavailable", "checked_at": now()}


def provision(cancel=None):
    """Runs away from the five-second status writer; never migrates a database."""
    if not run(["docker", "version", "--format", "{{.Server.Version}}"], cancel=cancel):
        if cancel is not None and cancel.is_set():
            return False
        desktop = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/DockerDesktop/Docker Desktop.exe"
        if not desktop.exists():
            desktop = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Docker/Docker/Docker Desktop.exe"
        if desktop.exists():
            # Hidden shell startup also covers Docker's GUI launcher.
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
            subprocess.Popen([str(desktop)], startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return False
    for env_file, compose in ((".env.mysql", "compose.yaml"), (".env.elasticsearch", "compose.elasticsearch.yaml")):
        if not run(["docker", "compose", "--env-file", env_file, "-f", compose, "up", "-d"], timeout=120, cancel=cancel):
            return False
    return run([PYTHON, "-m", "rag.manage", "db", "check"], timeout=15, cancel=cancel)


def safe_provision(cancel=None):
    try:
        return provision(cancel)
    except Exception as exc:
        event("supervisor", "dependency_start_failed", error_code=public_error(exc)["error_code"])
        return False


def backup_due(root=None, *, timestamp=None):
    root = root or state_root()
    local = (timestamp or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=8)))
    if local.hour < 3:
        return False
    try:
        previous = json.loads((root / "backup.json").read_text(encoding="utf-8"))
        if previous.get("identity") != identity():
            return True
        success = datetime.fromisoformat(previous["last_success_at"].replace("Z", "+00:00"))
        return success.astimezone(local.tzinfo).date() < local.date()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return True


def supervise():
    if os.name != "nt":
        raise ManagementError("RUNTIME_NOT_AVAILABLE")
    from .process import Child
    start_logging("supervisor")
    root = state_root()
    with exclusive(root / "supervisor.lock"):
        instance = uuid.uuid4().hex
        status = {"identity": identity(), "instance": instance, "supervisor": "running", "supervisor_pid": os.getpid(),
                  "dependencies": {}, "api": {}, "worker": {}}
        children, started, next_start, failures = {}, {}, {}, {"api": 0, "worker": 0}
        next_check, next_provision, next_backup = 0., 0., 0.
        ready, provision_future, checks, backup = False, None, {}, None
        pool = ThreadPoolExecutor(max_workers=4)
        cancel = threading.Event()
        event("supervisor", "started")
        try:
            while True:
                tick = time.monotonic()
                try:
                    stop = json.loads((root / "stop.json").read_text(encoding="utf-8"))
                    if stop.get("instance") == instance:
                        break
                except (OSError, ValueError, AttributeError):
                    pass
                for name, future in list(checks.items()):
                    if future.done():
                        status["dependencies"][name] = future.result()
                        del checks[name]
                if tick >= next_check:
                    for name in ("docker", "mysql", "elasticsearch"):
                        if name not in checks:
                            checks[name] = pool.submit(probe, name)
                    next_check = tick + 10
                if provision_future is not None and provision_future.done():
                    ready = provision_future.result()
                    provision_future = None
                needs_provision = not ready or any(item["state"] == "unavailable" for item in status["dependencies"].values())
                if needs_provision and provision_future is None and tick >= next_provision:
                    provision_future = pool.submit(safe_provision, cancel)
                    next_provision = tick + 60
                for name in ("api", "worker"):
                    child = children.get(name)
                    if child is not None and child.poll() is not None:
                        code = child.poll()
                        child.stop()
                        del children[name]
                        failures[name] = 1 if tick - started[name] >= 300 else failures[name] + 1
                        delay = min(60, 2 ** min(failures[name], 6))
                        next_start[name] = tick + delay
                        status[name].update(state="backoff", last_exit_code=code,
                            next_start_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat())
                        event("supervisor", "child_exited", component=name, exit_code=code)
                    if ready and name not in children and tick >= next_start.get(name, 0):
                        command = [PYTHON, "-m", "rag.api"] if name == "api" else [PYTHON, "-m", "rag.manage", "worker", "run"]
                        try:
                            children[name] = Child(command, cwd=ROOT)
                            started[name] = tick
                            status[name].update(state="running", next_start_at=None,
                                restart_count=status[name].get("restart_count", -1) + 1, pid=children[name].process.pid)
                            event("supervisor", "child_started", component=name)
                        except Exception as exc:
                            next_start[name] = tick + 60
                            status[name].update(state="backoff")
                            event("supervisor", "child_start_failed", component=name, error_code=public_error(exc)["error_code"])
                    elif not ready and name not in children:
                        status[name]["state"] = "waiting"
                if backup is not None and backup.poll() is not None:
                    backup.stop()
                    backup = None
                if ready and backup is None and tick >= next_backup:
                    if backup_due(root):
                        backup = Child([PYTHON, "-m", "rag.ops", "backup", "--due"], cwd=ROOT)
                    next_backup = tick + 300
                status["updated_at"] = now()
                atomic_json(root / "status.json", status)
                time.sleep(5)
        finally:
            # Closing owned Job Objects also covers descendants after a supervisor crash.
            cancel.set()
            owned = [*children.values(), *([backup] if backup else [])]
            failures_on_stop = []
            for child in owned:
                try:
                    child.terminate()
                except OSError as exc:
                    failures_on_stop.append(exc)
            deadline = time.monotonic() + 15
            for child in owned:
                try:
                    child.stop(timeout=max(0, deadline - time.monotonic()))
                except (OSError, subprocess.TimeoutExpired) as exc:
                    failures_on_stop.append(exc)
            status.update(supervisor="stopped", updated_at=now())
            for name in ("api", "worker"):
                status[name]["state"] = "stopped"
            pool.shutdown(wait=True, cancel_futures=True)
            if failures_on_stop:
                raise failures_on_stop[0]
            event("supervisor", "stopped")
            atomic_json(root / "status.json", status)
