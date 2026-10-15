"""Small atomic state files. No credentials or arbitrary exceptions cross this boundary."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from rag.management.config import ROOT, Settings
from rag.management.errors import ManagementError, MESSAGES


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def state_root():
    return Path(os.environ.get("RAG_RUNTIME_ROOT", str(ROOT / "tmp/runtime"))).resolve()


def identity(settings=None):
    settings = settings or Settings.from_env()
    value = [str(ROOT.resolve()), settings.host, settings.port, settings.database,
             str(settings.storage_root.resolve())]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".state-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows readers can briefly hold a non-delete-sharing handle.
                if attempt == 4:
                    raise
                time.sleep(.01 * (attempt + 1))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def exclusive(path, code="RUNTIME_BUSY"):
    """OS-held lock, automatically released after a crash; lock files are never unlinked."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if not path.stat().st_size:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ManagementError(code) from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)


def unknown_status(*, signal="unknown", updated_at=None):
    from rag.api.schemas import RuntimeStatus
    return RuntimeStatus(signal=signal, updated_at=updated_at, supervisor="unknown",
                         api={"state": "unknown"}, worker={"state": "unknown"},
                         dependencies={}, backup=None).model_dump(mode="json")


def read_object(path):
    if path.stat().st_size > 65536:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None


def runtime_status(settings=None, *, root=None, timestamp=None, expected_identity=None):
    from rag.api.schemas import BackupStatus, DependencyStatus, ProcessStatus, RuntimeStatus
    root = Path(root) if root is not None else state_root()
    result = unknown_status()
    try:
        value = read_object(root / "status.json")
        if value is None:
            return result
        expected = expected_identity if expected_identity is not None else identity(settings)
        if value.get("identity") != expected:
            return result
        updated = datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00"))
        age = ((timestamp or datetime.now(timezone.utc)) - updated).total_seconds()
        if not 0 <= age <= 15:
            return unknown_status(signal="stale", updated_at=updated.isoformat())
        supervisor = value.get("supervisor")
        result.update(signal="fresh", updated_at=updated.isoformat(),
                      supervisor=supervisor if supervisor in ("running", "stopped") else "unknown")
        for component in ("api", "worker"):
            item = value.get(component)
            item = item if isinstance(item, dict) else {}
            state = item.get("state")
            fields = {"state": state if state in ("running", "stopped", "waiting", "backoff") else "unknown",
                      "restart_count": item.get("restart_count", 0), "last_exit_code": item.get("last_exit_code"),
                      "next_start_at": item.get("next_start_at")}
            try:
                result[component] = ProcessStatus.model_validate(fields).model_dump(mode="json")
            except (TypeError, ValueError):
                result[component] = {"state": "unknown"}
        dependencies = value.get("dependencies")
        dependencies = dependencies if isinstance(dependencies, dict) else {}
        for name in ("docker", "mysql", "elasticsearch"):
            item = dependencies.get(name)
            item = item if isinstance(item, dict) else {}
            fields = {"state": item.get("state") if item.get("state") in ("available", "unavailable") else "unknown",
                      "checked_at": item.get("checked_at")}
            try:
                result["dependencies"][name] = DependencyStatus.model_validate(fields).model_dump(mode="json")
            except (TypeError, ValueError):
                result["dependencies"][name] = {"state": "unknown", "checked_at": None}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return unknown_status()

    # Independent signal: malformed backup metadata must not discard valid liveness.
    try:
        backup = read_object(root / "backup.json")
        if backup is not None and backup.get("identity") == expected:
            fields = {key: backup.get(key) for key in
                      ("status", "started_at", "finished_at", "last_success_at", "error_code")}
            code = fields["error_code"]
            if code is not None and (not isinstance(code, str) or code not in MESSAGES):
                fields["error_code"] = "LEGACY_ERROR"
            result["backup"] = BackupStatus.model_validate(fields).model_dump(mode="json")
    except (OSError, ValueError, TypeError, AttributeError):
        result["backup"] = None
    return RuntimeStatus.model_validate(result).model_dump(mode="json")
