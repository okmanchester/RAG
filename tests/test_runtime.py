"""Runtime observability, crash ownership, and published-only backup boundaries."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from rag.api.schemas import RuntimeStatus
from rag.management.config import ROOT, Settings
from rag.management.storage import load_snapshot, publish_raw
from rag.ops.state import atomic_json, exclusive, identity, runtime_status
from rag.ops.supervisor import backup_due
from scripts.backup_knowledge_base import copy_published
from test_management_storage import write_corpus


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="runtime-test-", dir=ROOT / "tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.settings = Settings("localhost", 3306, "fixture", "user", "secret", self.root / "storage")
        self.clock = datetime(2026, 10, 15, 1, tzinfo=timezone.utc)

    def write_status(self, **extra):
        value = {"identity": identity(self.settings), "updated_at": self.clock.isoformat(),
                 "supervisor": "running", "api": {"state": "running"},
                 "worker": {"state": "running", "restart_count": 2, "api_key": "secret"},
                 "dependencies": {"mysql": {"state": "available", "checked_at": self.clock.isoformat()}},
                 "private": "secret", **extra}
        atomic_json(self.root / "status.json", value)

    def read_status(self, **kwargs):
        return runtime_status(self.settings, root=self.root, timestamp=self.clock, **kwargs)

    def test_missing_corrupt_foreign_and_old_signals_never_claim_worker_dead(self):
        self.assertEqual(self.read_status()["worker"]["state"], "unknown")
        (self.root / "status.json").write_text("{bad", encoding="utf-8")
        self.assertEqual(self.read_status()["signal"], "unknown")
        self.write_status(identity="other-runtime")
        self.assertEqual(self.read_status()["signal"], "unknown")
        self.write_status(updated_at=(self.clock-timedelta(seconds=16)).isoformat())
        self.assertEqual(self.read_status()["signal"], "stale")
        self.assertEqual(self.read_status()["worker"]["state"], "unknown")
        self.write_status(updated_at=(self.clock+timedelta(seconds=1)).isoformat())
        self.assertEqual(self.read_status()["signal"], "stale")

    def test_failed_atomic_state_write_preserves_previous_file_and_cleans_stage(self):
        path=self.root / "atomic.json"
        atomic_json(path,{"previous":"valid"})
        with self.assertRaises(TypeError):
            atomic_json(path,{"bad":object()})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")),{"previous":"valid"})
        self.assertFalse(list(self.root.glob(".state-*.tmp")))

    def test_fresh_idle_signal_projects_allowlisted_fields(self):
        self.write_status()
        result = RuntimeStatus.model_validate(self.read_status()).model_dump_json()
        self.assertIn('"restart_count":2', result)
        self.assertNotIn("secret", result)
        self.assertNotIn("identity", result)
        self.assertEqual(self.read_status()["signal"], "fresh")

    def test_lock_is_cross_process_and_released_after_process_exit(self):
        path = self.root / "exclusive.lock"
        command = [sys.executable, "-c", "from rag.ops.state import exclusive; import sys; "
                   "with_lock=exclusive(sys.argv[1]); with_lock.__enter__()", str(path)]
        with exclusive(path):
            result = subprocess.run(command, cwd=ROOT, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(subprocess.run(command, cwd=ROOT, capture_output=True).returncode, 0)

    def test_backup_schedule_uses_beijing_date_and_catches_up_after_login(self):
        with patch("rag.ops.supervisor.identity", return_value=identity(self.settings)):
            self.assertFalse(backup_due(self.root, timestamp=datetime(2026,10,14,18,tzinfo=timezone.utc)))
            self.assertTrue(backup_due(self.root, timestamp=self.clock))
            atomic_json(self.root / "backup.json", {"identity":identity(self.settings), "last_success_at":None, "status":"running"})
            self.assertTrue(backup_due(self.root, timestamp=self.clock))
            atomic_json(self.root / "backup.json", {"identity":identity(self.settings), "last_success_at":self.clock.isoformat()})
            self.assertFalse(backup_due(self.root, timestamp=self.clock+timedelta(hours=1)))
            self.assertTrue(backup_due(self.root, timestamp=self.clock+timedelta(days=1)))

    def test_published_backup_ignores_staging_and_verifies_raw_and_snapshot(self):
        write_corpus(self.root / "corpus")
        snapshot = load_snapshot(self.root / "corpus")
        snapshot.publish(self.settings.storage_root)
        raw = self.root / "input"
        raw.mkdir()
        (raw / "fixture.pdf").write_bytes(b"fixture pdf")
        digest=hashlib.sha256(b"fixture pdf").hexdigest()
        publish_raw(raw,"fixture.pdf",digest,self.settings.storage_root)
        (self.settings.storage_root / "raw/.raw-incomplete").mkdir()
        (self.settings.storage_root / "snapshots/.snapshot-incomplete").mkdir()
        copy_published(self.settings.storage_root,self.root / "copy")
        self.assertEqual(list((self.root / "copy/raw").iterdir()), [self.root / "copy/raw" / digest])
        self.assertEqual(load_snapshot(self.root / "copy/snapshots" / snapshot.fingerprint).fingerprint,snapshot.fingerprint)
        (self.settings.storage_root / "raw" / digest).write_bytes(b"corruption")
        with self.assertRaisesRegex(ValueError,"checksum"):
            copy_published(self.settings.storage_root,self.root / "bad-copy")

    def test_retention_only_removes_owned_successful_backups(self):
        from rag.ops.__main__ import rotate_backups
        backups = self.root / "backups"
        backups.mkdir()
        for i in range(10):
            folder = backups / f"rag-20261015T010000Z-{i:08x}"
            atomic_json(folder / "managed.json", {"identity":identity(self.settings), "completed_at":f"2026-10-15T01:00:{i:02}Z"})
            atomic_json(folder / "backup.json", {"files_sha256":{"database.sql":"hash"}})
        unrelated = backups / "operator-backup"
        unrelated.mkdir()
        with patch("rag.ops.__main__.identity", return_value=identity(self.settings)):
            rotate_backups(backups,7)
        self.assertEqual(len(list(backups.iterdir())),8)
        self.assertTrue(unrelated.is_dir())

    def test_failed_scheduled_backup_keeps_last_success_and_never_rotates(self):
        from rag.ops.__main__ import scheduled_backup
        previous = {"identity":identity(self.settings),"last_success_at":self.clock.isoformat(),"status":"succeeded"}
        atomic_json(self.root / "backup.json",previous)
        with patch.dict(os.environ,{"RAG_RUNTIME_ROOT":str(self.root)}), \
             patch("rag.ops.__main__.identity",return_value=identity(self.settings)), \
             patch("scripts.backup_knowledge_base._backup",side_effect=OSError("private-secret")), \
             patch("rag.ops.__main__.event"), \
             patch("rag.ops.__main__.rotate_backups") as rotate:
            with self.assertRaises(OSError):
                scheduled_backup(destination=self.root / "backups")
        value=json.loads((self.root / "backup.json").read_text(encoding="utf-8"))
        self.assertEqual(value["status"],"failed")
        self.assertEqual(value["last_success_at"],self.clock.isoformat())
        self.assertNotIn("private-secret",json.dumps(value))
        rotate.assert_not_called()

    @unittest.skipUnless(os.name=="nt", "Windows Job Objects")
    def test_job_termination_reaps_native_style_descendant(self):
        import ctypes
        from ctypes import wintypes
        from rag.ops.process import Child
        pidfile=self.root / "descendant.pid"
        code="import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(120)"
        child=Child([sys.executable,"-c",code,str(pidfile)],cwd=ROOT)
        self.addCleanup(child.stop)
        deadline=time.monotonic()+10
        while not pidfile.exists() and time.monotonic()<deadline:
            time.sleep(.05)
        pid=int(pidfile.read_text())
        kernel=ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x00100000,False,pid)
        self.assertTrue(handle)
        try:
            child.stop()
            self.assertEqual(kernel.WaitForSingleObject(handle,5000),0)
        finally:
            kernel.CloseHandle(handle)
