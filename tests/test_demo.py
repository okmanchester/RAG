"""Demo lifecycle checks without Docker, models, business DB writes, or real services."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from rag import demo
from rag.management.config import ROOT, Settings
from rag.management.errors import ManagementError
from rag.ops.state import atomic_json, exclusive, identity, runtime_status


@unittest.skipUnless(os.name == "nt", "Windows demo launcher")
class DemoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="demo-test-", dir=ROOT / "tmp")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings("127.0.0.1", 3306, "demo_fixture", "fixture", "secret", self.root / "storage")
        self.enterContext(patch.dict(os.environ, {"RAG_RUNTIME_ROOT": str(self.root)}))
        self.enterContext(patch("rag.demo.Settings.from_env", return_value=self.settings))
        self.preflight = self.enterContext(patch("rag.demo.check_dependencies", return_value={}))
        self.children = [Mock(), Mock()]
        for child in self.children:
            child.poll.return_value = None
        self.spawn = self.enterContext(patch("rag.demo.Child", side_effect=self.children))
        # Enforce the demo's boundary even when future launcher code is changed.
        self.enterContext(patch("rag.ops.supervisor.provision", side_effect=AssertionError("automatic deployment")))
        self.enterContext(patch("rag.ops.__main__.scheduled_backup", side_effect=AssertionError("automatic backup")))
        self.enterContext(patch("rag.ops.__main__.task", side_effect=AssertionError("scheduled task change")))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.errors = self.enterContext(redirect_stderr(io.StringIO()))

    def assert_stopped(self):
        for child in self.children:
            child.terminate.assert_called_once_with()
            child.stop.assert_called_once()
        value = json.loads((self.root / "status.json").read_text())
        self.assertEqual(value["supervisor"], "stopped")
        self.assertEqual(value["api"]["state"], "stopped")
        self.assertEqual(value["worker"]["state"], "stopped")
        with exclusive(self.root / "supervisor.lock"):
            pass

    def test_ctrl_c_cleans_both_trees_without_restart_deployment_or_backup(self):
        with patch("rag.demo.time.sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(demo.main([]), 0)
        self.assertEqual(self.spawn.call_count, 2)
        self.assert_stopped()

    def test_component_exit_fails_the_demo_and_stops_its_sibling(self):
        for code in (0, 7):
            with self.subTest(code=code):
                for child in self.children:
                    child.reset_mock()
                    child.poll.return_value = None
                self.children[1].poll.return_value = code
                self.spawn.side_effect = self.children
                self.assertEqual(demo.run_demo(), 1)
                self.assert_stopped()
                value = json.loads((self.root / "status.json").read_text())
                self.assertEqual(value["worker"]["last_exit_code"], code)
        self.assertEqual(self.spawn.call_count, 4)

    def test_stop_signal_cleans_processes_without_task_scheduler(self):
        def request_stop(_seconds):
            value = json.loads((self.root / "status.json").read_text())
            atomic_json(self.root / "stop.json", {"instance": value["instance"]})
        with patch("rag.demo.time.sleep", side_effect=request_stop):
            self.assertEqual(demo.run_demo(), 0)
        self.assert_stopped()

    def test_failed_preflight_starts_no_process_and_hides_secrets(self):
        self.preflight.side_effect = RuntimeError("private password=secret")
        self.assertEqual(demo.main([]), 1)
        self.spawn.assert_not_called()
        self.assertNotIn("password=secret", self.errors.getvalue())
        with exclusive(self.root / "supervisor.lock"):
            pass

    def test_partial_start_failure_stops_the_already_started_api(self):
        self.spawn.side_effect = [self.children[0], OSError("private failure")]
        self.assertEqual(demo.main([]), 1)
        self.children[0].stop.assert_called_once()
        self.children[1].stop.assert_not_called()
        self.assertNotIn("private failure", self.errors.getvalue())

    def test_existing_supervisor_or_demo_prevents_any_preflight_or_spawn(self):
        with exclusive(self.root / "supervisor.lock"):
            self.assertEqual(demo.main([]), 1)
        self.preflight.assert_not_called()
        self.spawn.assert_not_called()

    def test_cleanup_failure_is_reported_and_other_tree_is_still_cleaned(self):
        self.children[0].stop.side_effect = OSError("private cleanup failure")
        with patch("rag.demo.time.sleep", side_effect=KeyboardInterrupt):
            self.assertEqual(demo.main([]), 1)
        self.children[1].stop.assert_called_once()
        value = json.loads((self.root / "status.json").read_text())
        self.assertEqual(value["api"]["state"], "unknown")
        self.assertNotIn("private cleanup failure", self.errors.getvalue())

    def test_stop_option_bypasses_dependencies_and_task_scheduler(self):
        with patch("rag.ops.__main__.stop", return_value={"status": "not_running"}) as stop:
            self.assertEqual(demo.main(["--stop"]), 0)
        stop.assert_called_once_with()
        self.preflight.assert_not_called()
        self.spawn.assert_not_called()

    def test_demo_status_hides_historical_backup_and_exposes_mode(self):
        atomic_json(self.root / "backup.json", {"identity": identity(self.settings), "status": "succeeded"})
        def inspect_then_interrupt(_seconds):
            value = runtime_status(self.settings, root=self.root)
            self.assertEqual(value["mode"], "demo")
            self.assertEqual(value["worker"]["state"], "running")
            self.assertIsNone(value["backup"])
            raise KeyboardInterrupt
        with patch("rag.demo.time.sleep", side_effect=inspect_then_interrupt):
            self.assertEqual(demo.run_demo(), 0)

    def test_separate_stop_command_reaps_real_isolated_helper_processes(self):
        from rag.ops.process import Child as RealChild
        script = self.root / "isolated_demo.py"
        script.write_text('''import json, sys, time
from pathlib import Path
from rag import demo
from rag.ops.process import Child
from rag.ops.state import state_root
demo.check_dependencies = lambda settings: {}
pids = []
def helper(*args, **kwargs):
    child = Child([sys.executable, '-c', 'import time; time.sleep(120)'], **kwargs)
    pids.append(child.process.pid)
    (state_root() / 'helpers.json').write_text(json.dumps(pids))
    return child
demo.Child = helper
raise SystemExit(demo.main([]))
''', encoding="utf-8")
        environment = {**os.environ, "PYTHONPATH": str(ROOT), "MYSQL_HOST": self.settings.host,
                       "MYSQL_PORT": str(self.settings.port), "MYSQL_DATABASE": self.settings.database,
                       "MYSQL_USER": self.settings.user, "MYSQL_PASSWORD": self.settings.password,
                       "RAG_STORAGE_ROOT": str(self.settings.storage_root)}
        child = RealChild([sys.executable, str(script)], cwd=ROOT, env=environment)
        self.addCleanup(child.stop)
        deadline = time.monotonic() + 10
        while not (self.root / "status.json").is_file() and time.monotonic() < deadline:
            if child.poll() is not None:
                self.fail("isolated launcher exited before publishing status")
            time.sleep(.05)
        self.assertTrue((self.root / "status.json").is_file())
        pids = json.loads((self.root / "helpers.json").read_text())
        self.assertEqual(len(pids), 2)
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handles = [kernel.OpenProcess(0x00100000, False, pid) for pid in pids]
        try:
            self.assertTrue(all(handles))
            result = subprocess.run([sys.executable, "-B", "-m", "rag.demo", "--stop"], cwd=ROOT,
                                    env=environment, capture_output=True, text=True, timeout=15,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "stopped")
            self.assertEqual(child.process.wait(timeout=5), 0)
            for handle in handles:
                self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
            with exclusive(self.root / "supervisor.lock"):
                pass
        finally:
            for handle in handles:
                if handle:
                    kernel.CloseHandle(handle)


class PreflightTests(unittest.TestCase):
    def test_preflight_checks_existing_services_and_disposes_clients_without_models(self):
        settings = Mock()
        engine = settings.engine.return_value
        with patch("rag.demo.ManagementService") as service, \
             patch("rag.search.config.SearchSettings") as search, patch("rag.demo.socket.socket") as socket, \
             patch("rag.search.shared.load_encoder", side_effect=AssertionError("model loaded")), \
             redirect_stdout(io.StringIO()):
            result = demo.check_dependencies(settings)
        service.return_value.db_check.assert_called_once_with()
        engine.dispose.assert_called_once_with()
        client = search.from_env.return_value.client.return_value.__enter__.return_value
        client.options.return_value.info.assert_called_once_with()
        socket.return_value.__enter__.return_value.bind.assert_called_once_with(("127.0.0.1", 8000))
        self.assertEqual(set(result), {"mysql", "elasticsearch"})

    def test_database_failure_disposes_engine_and_does_not_start_es(self):
        settings = Mock()
        with patch("rag.demo.ManagementService") as service, \
             patch("rag.search.config.SearchSettings") as search, redirect_stdout(io.StringIO()):
            service.return_value.db_check.side_effect = ManagementError("SCHEMA_OUTDATED")
            with self.assertRaises(ManagementError):
                demo.check_dependencies(settings)
        settings.engine.return_value.dispose.assert_called_once_with()
        search.from_env.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows scheduled-task script")
class ScheduledTaskBoundaryTests(unittest.TestCase):
    def script(self, scenario):
        path = str(ROOT / "scripts/runtime_tasks.ps1").replace("'", "''")
        # Shadow the native cmdlets; no real scheduled tasks are read or changed.
        code = r"""
$ErrorActionPreference = 'Stop'
function Get-ScheduledTask {
    [CmdletBinding()]param()
    ENUMERATION
}
function Disable-ScheduledTask {
    [CmdletBinding()]param([Parameter(ValueFromPipeline=$true)]$InputObject)
    process { DISABLE }
}
& 'SCRIPT' -Action Disable -Python 'unused' -Project 'unused'
exit $LASTEXITCODE
""".replace("SCRIPT", path)
        enumeration = r"[pscustomobject]@{TaskName='RAG-Local-Supervisor';TaskPath='\'}"
        disable = "throw 'disable should not be called'"
        if scenario == "absent":
            enumeration = "return"
        elif scenario == "denied":
            disable = "Write-Error -Message 'simulated denial' -Category PermissionDenied"
        elif scenario == "service_failure":
            enumeration = "throw 'simulated task service failure'"
        elif scenario == "present":
            disable = "Write-Host 'disabled'"
        code = code.replace("ENUMERATION", enumeration).replace("DISABLE", disable)
        return subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", code],
                              capture_output=True, text=True, timeout=15,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_only_absent_tasks_are_ignored(self):
        for scenario in ("absent", "present", "denied", "service_failure"):
            with self.subTest(scenario=scenario):
                result = self.script(scenario)
                self.assertEqual(result.returncode == 0, scenario in ("absent", "present"), result.stderr)
                if scenario == "present":
                    self.assertIn("disabled", result.stdout)
                elif scenario == "denied":
                    self.assertIn("simulated denial", result.stderr)
