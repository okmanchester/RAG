"""python -m rag.ops {install,uninstall,start,stop,status,supervise,backup}."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from rag.management.config import ROOT, Settings
from rag.management.errors import ManagementError, public_error
from .events import event, start_logging
from .state import atomic_json, exclusive, identity, now, read_object, runtime_status, state_root


def scheduled_backup(*, due=False, destination=None, retain=7):
    from scripts.backup_knowledge_base import backup_lock
    settings = Settings.from_env(ROOT / ".env.mysql")
    try:
        with backup_lock(settings):
            return _scheduled_backup(settings, due=due, destination=destination, retain=retain)
    except ManagementError as exc:
        if exc.code != "BACKUP_BUSY":
            raise
        # Contention is a no-op observation, not a failed backup attempt.
        return {"status": "busy"}


def _scheduled_backup(settings, *, due, destination, retain):
    """Caller owns the same lock as manual backup, before writing any status."""
    from scripts.backup_knowledge_base import _backup
    from .supervisor import backup_due
    root = state_root()
    if due and not backup_due(root):
        return {"status": "not_due"}
    previous = {}
    try:
        previous = read_object(root / "backup.json") or {}
        if previous.get("identity") != identity():
            previous = {}
    except (OSError, ValueError, AttributeError):
        pass
    result = {"identity": identity(), "status": "running", "started_at": now(),
              "finished_at": None, "last_success_at": previous.get("last_success_at"), "error_code": None}
    atomic_json(root / "backup.json", result)
    try:
        destination = Path(destination or os.environ.get("RAG_BACKUP_ROOT", "D:/RagBackups")).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        name = "rag-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        output = destination / name
        _backup(settings, ROOT / ".env.mysql", output)
        # Marker scopes retention to this runtime; old/manual/unrelated backups are untouched.
        atomic_json(output / "managed.json", {"identity": identity(), "completed_at": now()})
        result.update(status="succeeded", finished_at=now(), last_success_at=now())
        atomic_json(root / "backup.json", result)
        rotate_backups(destination, retain)
        event("supervisor", "backup_completed", status="succeeded")
        return {"status": "succeeded", "backup": str(output)}
    except Exception as exc:
        result.update(status="failed", finished_at=now(), error_code=public_error(exc)["error_code"])
        atomic_json(root / "backup.json", result)
        event("supervisor", "backup_failed", error_code=result["error_code"])
        raise


def rotate_backups(destination, retain):
    destination = Path(destination).resolve()
    candidates = []
    for folder in destination.iterdir():
        if not re.fullmatch(r"rag-\d{8}T\d{6}Z-[0-9a-f]{8}", folder.name) or folder.is_symlink():
            continue
        if folder.resolve().parent != destination:
            continue
        try:
            marker = json.loads((folder / "managed.json").read_text(encoding="utf-8"))
            manifest = json.loads((folder / "backup.json").read_text(encoding="utf-8"))
            if marker["identity"] == identity() and manifest.get("files_sha256"):
                candidates.append((marker["completed_at"], folder))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    for _, folder in sorted(candidates, reverse=True)[retain:]:
        # Resolve and check again immediately before recursively deleting a managed backup.
        if folder.is_symlink() or folder.resolve().parent != destination:
            raise ManagementError("BACKUP_CHECKSUM")
        shutil.rmtree(folder)


def task(action):
    if os.name != "nt":
        raise ManagementError("RUNTIME_NOT_AVAILABLE")
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.is_file():
        raise ManagementError("RUNTIME_NOT_AVAILABLE")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(ROOT / "scripts/runtime_tasks.ps1"), "-Action", action,
        "-Python", str(python), "-Project", str(ROOT)], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise ManagementError("RUNTIME_COMMAND_FAILED")


def stop():
    root = state_root()
    def lock_released():
        try:
            with exclusive(root / "supervisor.lock"):
                return True
        except ManagementError as exc:
            if exc.code != "RUNTIME_BUSY":
                raise
            return False
    if lock_released():
        return {"status": "not_running"}
    try:
        value = json.loads((root / "status.json").read_text(encoding="utf-8"))
        if value.get("identity") != identity():
            raise ManagementError("RUNTIME_COMMAND_FAILED")
        if value.get("supervisor") == "stopped":
            return {"status": "stopped"}
        atomic_json(root / "stop.json", {"instance": value["instance"]})
        # Loop tick + a shared child deadline + bounded probes. Provision is cancelled.
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            value = json.loads((root / "status.json").read_text(encoding="utf-8"))
            if value.get("supervisor") == "stopped" and lock_released():
                return {"status": "stopped"}
            time.sleep(.25)
        # Never kill a PID recovered from disk; task ownership is the fallback boundary.
        raise ManagementError("RUNTIME_COMMAND_FAILED")
    except FileNotFoundError:
        return {"status": "not_running"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall", "start", "stop", "status", "supervise", "backup"))
    parser.add_argument("--due", action="store_true")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "supervise":
            from .supervisor import supervise
            supervise()
            return 0
        if args.action == "backup":
            start_logging("supervisor")
            result = scheduled_backup(due=args.due, destination=args.destination)
        elif args.action == "status":
            result = runtime_status()
        elif args.action == "stop":
            task("Disable")
            result = stop()
        elif args.action == "uninstall":
            task("Disable")
            try:
                stop()
            finally:
                task("Uninstall")
            result = {"status": "uninstalled"}
        else:
            task(args.action.title())
            result = {"status": "installed" if args.action == "install" else "starting"}
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        event("supervisor", "command_failed", error_code=public_error(exc)["error_code"])
        if sys.stderr is not None:
            print(json.dumps(public_error(exc), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
