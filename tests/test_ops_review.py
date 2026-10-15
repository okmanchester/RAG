"""Regression coverage for telemetry failure, independent status signals and shutdown."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch, call

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from rag.api.app import create_app
from rag.api.runtime import Runtime
from rag.management.config import ROOT, Settings
from rag.management.errors import ManagementError
from rag.ops import events
from rag.ops.state import atomic_json, identity, runtime_status


class OpsReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ops-review-", dir=ROOT / "tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {"RAG_RUNTIME_ROOT": str(self.root)}))
        # Close handlers before TemporaryDirectory cleanup on Windows.
        for role in ("api", "worker", "supervisor"):
            events.close_logging(role)
            self.addCleanup(events.close_logging, role)
        self.settings = Settings("127.0.0.1", 3306, "fixture", "fixture", "secret", self.root / "storage")
        self.clock = datetime.now(timezone.utc)

    def valid_status(self, **overrides):
        atomic_json(self.root / "status.json", {"identity": identity(self.settings),
            "updated_at": self.clock.isoformat(), "supervisor": "running",
            "api": {"state": "running"}, "worker": {"state": "running"}, **overrides})

    def test_bad_backup_signal_does_not_erase_valid_liveness(self):
        self.valid_status()
        for backup in ([], "private-secret", 3, None, {"identity": identity(self.settings), "status": "unexpected"}):
            with self.subTest(backup=backup):
                atomic_json(self.root / "backup.json", backup)
                result = runtime_status(self.settings, root=self.root, timestamp=self.clock)
                self.assertEqual(result["signal"], "fresh")
                self.assertEqual(result["worker"]["state"], "running")
                self.assertIsNone(result["backup"])

    def test_unknown_supervisor_does_not_erase_process_signals(self):
        self.valid_status(supervisor="unexpected")
        result = runtime_status(self.settings, root=self.root, timestamp=self.clock)
        self.assertEqual(result["supervisor"], "unknown")
        self.assertEqual(result["worker"]["state"], "running")
        self.assertEqual(result["signal"], "fresh")
        (self.root / "status.json").unlink()
        unknown = runtime_status(self.settings, root=self.root, timestamp=self.clock)
        self.assertEqual(set(result["worker"]), set(unknown["worker"]))

    def test_fixed_identity_avoids_reloading_configuration(self):
        self.valid_status()
        with patch("rag.ops.state.Settings.from_env", side_effect=AssertionError("configuration reloaded")):
            result = runtime_status(root=self.root, timestamp=self.clock, expected_identity=identity(self.settings))
        self.assertEqual(result["signal"], "fresh")

    def test_library_events_do_not_initialize_any_disk_sink(self):
        with patch("rag.ops.events.start_logging", side_effect=AssertionError("implicit log initialization")):
            events.event("worker", "task_completed", task_id=1, status="succeeded")
        self.assertFalse((self.root / "logs").exists())

    def test_failed_log_setup_does_not_prevent_api_startup(self):
        from rag.api.__main__ import main
        blocked = self.root / "not-a-directory"
        blocked.write_text("blocked", encoding="utf-8")
        with patch.dict(os.environ, {"RAG_RUNTIME_ROOT": str(blocked)}), \
             patch("uvicorn.run") as serve:
            self.assertEqual(main(), 0)
        serve.assert_called_once()

    def test_disk_failure_during_error_logging_preserves_database_error_json(self):
        logger = events.start_logging("api")
        self.assertIsNotNone(logger)
        management = Mock()
        management.show.side_effect = OperationalError("private SQL", {}, Exception("private credentials"))
        with TestClient(create_app(runtime=Runtime(management), tmp_root=self.root / "http"),
                        base_url="http://127.0.0.1:8000") as client, \
             patch.object(logger, "info", side_effect=OSError("private disk error")):
            response = client.get("/api/v1/documents/1")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "DATABASE_ERROR")
        self.assertNotIn("private", response.text)

    def test_uninstall_removes_tasks_even_if_shutdown_reports_failure(self):
        from rag.ops.__main__ import main
        with patch("sys.argv", ["rag.ops", "uninstall"]), patch("rag.ops.__main__.task") as task, \
             patch("rag.ops.__main__.stop", side_effect=ManagementError("RUNTIME_COMMAND_FAILED")), \
             redirect_stderr(io.StringIO()):
            self.assertEqual(main(), 1)
        self.assertEqual(task.call_args_list, [call("Disable"), call("Uninstall")])

    def test_backup_contention_does_not_write_failed_status(self):
        from rag.ops.__main__ import scheduled_backup
        from scripts.backup_knowledge_base import backup_lock
        previous = {"identity": identity(self.settings), "status": "succeeded", "last_success_at": self.clock.isoformat()}
        atomic_json(self.root / "backup.json", previous)
        with patch("rag.ops.__main__.Settings.from_env", return_value=self.settings), \
             backup_lock(self.settings), patch("rag.ops.__main__.event") as log:
            self.assertEqual(scheduled_backup(destination=self.root / "backups"), {"status": "busy"})
        self.assertEqual(json.loads((self.root / "backup.json").read_text()), previous)
        log.assert_not_called()

    def test_new_storage_prefix_and_unknown_object_fail_explicitly(self):
        from scripts.backup_knowledge_base import copy_published
        source = self.root / "storage"
        (source / "new-assets").mkdir(parents=True)
        with self.assertRaisesRegex(ManagementError, "unknown layout"):
            copy_published(source, self.root / "copy")
        (source / "new-assets").rmdir()
        (source / "raw").mkdir()
        (source / "raw/unexpected").write_text("unknown", encoding="utf-8")
        with self.assertRaisesRegex(ManagementError, "unknown layout"):
            copy_published(source, self.root / "copy2")

    def test_non_windows_import_does_not_load_windows_ctypes(self):
        code = """import importlib.util,os,sys
source=sys.argv[1]
os.name='posix'
spec=importlib.util.spec_from_file_location('platform_probe',source)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert 'ctypes.wintypes' not in sys.modules
"""
        result = subprocess.run([sys.executable, "-c", code, str(ROOT / "rag/ops/process.py")], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows process cancellation")
    def test_long_provision_command_is_cancelled_without_waiting_for_timeout(self):
        from rag.ops.supervisor import run
        cancel = threading.Event()
        marker = self.root / "started"
        code = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(120)"
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run, [sys.executable, "-c", code, str(marker)], timeout=120, cancel=cancel)
            try:
                deadline = time.monotonic() + 10
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                started = time.monotonic()
                cancel.set()
                self.assertFalse(future.result(timeout=5))
                self.assertLess(time.monotonic() - started, 5)
            finally:
                cancel.set()

    @unittest.skipUnless(os.name == "nt", "Windows Job Object wait retry")
    def test_child_retains_job_handle_after_wait_timeout(self):
        from rag.ops.process import Child
        child = Child([sys.executable, "-c", "import time;time.sleep(120)"], cwd=ROOT)
        self.addCleanup(child.stop)
        with patch.object(child.process, "wait", side_effect=subprocess.TimeoutExpired("fixture", 0)):
            with self.assertRaises(subprocess.TimeoutExpired):
                child.stop(timeout=0)
        self.assertIsNotNone(child.job)
        child.stop()
        self.assertIsNone(child.job)

    @unittest.skipUnless(os.name == "nt", "Windows supervisor shutdown")
    def test_supervisor_stop_cancels_inflight_provision_and_releases_lock(self):
        from rag.ops.process import Child
        from rag.ops.__main__ import stop
        runtime_root = self.root / "supervisor"
        marker = self.root / "provision-started"
        code = """import sys
from rag.ops import supervisor as s
from rag.ops.state import now
s.probe=lambda name:{'state':'available','checked_at':now()}
def provision(cancel):
    code='import pathlib,sys,time;pathlib.Path(sys.argv[1]).touch();time.sleep(120)'
    return s.run([sys.executable,'-c',code,sys.argv[1]],timeout=120,cancel=cancel)
s.provision=provision
s.supervise()
"""
        environment = {**os.environ, "RAG_RUNTIME_ROOT": str(runtime_root),
                       "MYSQL_DATABASE": "rag_shutdown_probe", "RAG_STORAGE_ROOT": str(self.root / "storage")}
        child = Child([sys.executable, "-c", code, str(marker)], cwd=ROOT, env=environment)
        self.addCleanup(child.stop)
        deadline = time.monotonic() + 15
        while (not marker.exists() or not (runtime_root / "status.json").exists()) and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(marker.exists())
        with patch.dict(os.environ, environment):
            started = time.monotonic()
            self.assertEqual(stop(), {"status": "stopped"})
            self.assertLess(time.monotonic() - started, 12)
        self.assertEqual(child.process.wait(timeout=5), 0)
