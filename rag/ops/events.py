"""Allowlisted local lifecycle logs, independent of third-party inference output."""
import json
import logging
from logging.handlers import RotatingFileHandler
import threading

from .state import now, state_root

_loggers = {}
_lock = threading.RLock()


def close_logging(role):
    """Release an explicitly configured sink (also used by isolated tests)."""
    with _lock:
        item = _loggers.pop(role, None)
        if item:
            for handler in item[1].handlers[:]:
                item[1].removeHandler(handler)
                handler.close()


def start_logging(role):
    """Entrypoints opt in. Telemetry must never make a service unavailable."""
    try:
        if role not in {"api", "worker", "supervisor"}:
            return None
        folder = state_root() / "logs"
        with _lock:
            previous = _loggers.get(role)
            if previous and previous[0] == folder:
                return previous[1]
            close_logging(role)
            folder.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(folder / (role + ".jsonl"), maxBytes=10 * 1024 * 1024,
                                          backupCount=4, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.Logger("rag.events." + role, logging.INFO)
            logger.propagate = False
            logger.addHandler(handler)
            _loggers[role] = (folder, logger)
            return logger
    except Exception:
        return None


def event(role, name, **values):
    try:
        with _lock:
            item = _loggers.get(role)
            if item is None:
                return
            fields = {key: value for key, value in values.items() if value is not None and key in
                      {"task_id", "kb_id", "kind", "attempt", "revision", "status", "error_code",
                       "seconds", "exit_code", "component", "restart_count"}}
            item[1].info(json.dumps({"time": now(), "event": name, **fields}, ensure_ascii=False))
    except Exception:
        # Includes initialization, serialization, rotation, and custom handler errors.
        # Do not log the logging failure or re-enter the business error boundary.
        return
