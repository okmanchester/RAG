"""Error boundaries must remain safe even when third-party exceptions carry secrets."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.exc import OperationalError, SQLAlchemyError

from rag.management.errors import ManagementError, public_error, stored_error

SECRET = "password=secret-token mysql://user:secret-token@host SQL_PARAMETER D:/private/secret-token/file"


def exceptions():
    return [ValueError(SECRET), RuntimeError(SECRET), SQLAlchemyError(SECRET),
            OperationalError("SELECT " + SECRET, {"password": SECRET}, Exception(SECRET)),
            OperationalError("SELECT " + SECRET, {"password": SECRET}, Exception(1045, SECRET)),
            PermissionError(13, SECRET, "D:/private/secret-token/file"),
            json.JSONDecodeError(SECRET, SECRET, 0),
            UnicodeDecodeError("utf-8", SECRET.encode(), 0, 1, SECRET)]


class ErrorBoundaryTests(unittest.TestCase):
    def assert_safe(self, value):
        encoded = json.dumps(value)
        for token in ("secret-token", "mysql://", "SQL_PARAMETER", "D:/private"):
            self.assertNotIn(token, encoded)

    def test_external_exception_messages_are_never_echoed(self):
        for exception in exceptions():
            with self.subTest(exception=type(exception).__name__):
                error = public_error(exception)
                self.assertIn("error_code", error)
                self.assert_safe(error)
        database = public_error(OperationalError("hidden", {}, Exception(1045, SECRET)))
        self.assertEqual(database, {"error_code": "DATABASE_ERROR", "error": "database operation failed (code=1045)"})

    def test_controlled_and_stored_messages_use_only_the_catalogue(self):
        self.assert_safe(public_error(ManagementError(SECRET)))
        self.assertEqual(public_error(ManagementError("RAW_CHECKSUM"))["error"], "raw file checksum mismatch")
        for code in (None, "LEGACY_ERROR", "RAW_CHECKSUM", "DATABASE_ERROR", SECRET):
            self.assert_safe(stored_error(code, SECRET))
        self.assertEqual(stored_error("DATABASE_ERROR", "database operation failed (code=1045)")["error"],
                         "database operation failed (code=1045)")

    def test_cli_stderr_hides_external_exception_text(self):
        from rag.manage import main
        for exception in exceptions():
            errors = io.StringIO()
            with self.subTest(exception=type(exception).__name__), \
                    patch("rag.manage.Settings.from_env", side_effect=exception), \
                    contextlib.redirect_stderr(errors):
                self.assertEqual(main(["db", "check"]), 1)
            self.assert_safe(errors.getvalue())
            self.assertIn("error_code", json.loads(errors.getvalue()))

    def test_backup_stderr_and_unsafe_path_validation_do_not_echo_input(self):
        from scripts.backup_knowledge_base import main
        from rag.management.storage import relative_path
        with self.assertRaises(ManagementError) as caught:
            relative_path("D:/private/secret-token/file")
        self.assert_safe(public_error(caught.exception))
        for exception in exceptions():
            errors = io.StringIO()
            with patch("sys.argv", ["backup", "backup", "--output", "unused"]), \
                    patch("scripts.backup_knowledge_base.backup", side_effect=exception), \
                    contextlib.redirect_stderr(errors):
                self.assertEqual(main(), 1)
            self.assert_safe(errors.getvalue())

    def test_backup_manifest_records_the_unenforced_consistency_contract(self):
        from rag.management.config import Settings
        from scripts.backup_knowledge_base import backup
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings("localhost", 3306, "test", "test", "not-real", root / "storage")
            settings.storage_root.mkdir()
            import hashlib
            digest=hashlib.sha256(b"preserved").hexdigest()
            (settings.storage_root / "raw").mkdir()
            (settings.storage_root / "raw" / digest).write_bytes(b"preserved")

            def dump(_arguments, *, stdout, **_kwargs):
                stdout.write(b"-- SQL fixture")
                return type("Result", (), {"returncode": 0})()

            with patch.dict(os.environ, {"RAG_RUNTIME_ROOT": str(root / "runtime")}), \
                    patch("scripts.backup_knowledge_base.Settings.from_env", return_value=settings), \
                    patch("scripts.backup_knowledge_base.subprocess.run", side_effect=dump):
                backup(root / "unused.env", root / "backup")
            contract = json.loads((root / "backup/backup.json").read_text())["consistency_contract"]
            self.assertFalse(contract["coordination_enforced"])
            self.assertEqual(contract["published_storage"], "append_only_no_overwrite_or_delete")
            self.assertEqual(contract["database_dml"], "online_single_transaction_snapshot")
            self.assertEqual(contract["ddl_and_destructive_storage_mutations"], "prohibited_by_operator")
            self.assertEqual((root / "backup/storage/raw" / digest).read_bytes(), b"preserved")

    def test_invalid_resources_and_pages_fail_before_database_access(self):
        from rag.management.service import ManagementService
        service = ManagementService(None, Path("unused"))
        for kind in (None, [], {}, 1, "unknown"):
            with self.subTest(kind=kind), self.assertRaises(ManagementError) as caught:
                service.list_records(kind)
            self.assertEqual(caught.exception.code, "INVALID_RESOURCE")
        for limit, offset in ((0, 0), (True, 0), (1001, 0), (1, -1), (1, False)):
            with self.subTest(limit=limit, offset=offset), self.assertRaises(ManagementError) as caught:
                service.list_records("kb", limit=limit, offset=offset)
            self.assertEqual(caught.exception.code, "INVALID_PAGE")


if __name__ == "__main__":
    unittest.main()
