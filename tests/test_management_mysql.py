"""Opt-in integration tests against a disposable real MySQL database.

RAG_RUN_MYSQL_TESTS=1 enables these tests; Docker Compose mysql must be healthy.
"""
import copy
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from sqlalchemy import delete, event, func, inspect, select, update
from sqlalchemy.exc import DBAPIError

from rag.management.config import ROOT, Settings, migration_config
from rag.management.models import Base, Chunk, Document, DocumentBuild, DocumentVersion, ImportRun, ImportRunItem, KnowledgeBase
from rag.management.service import ManagementService
from rag.management.storage import file_hash, load_snapshot
from scripts.backup_knowledge_base import container_sql, database_name
from test_management_storage import write_corpus


@unittest.skipUnless(os.environ.get("RAG_RUN_MYSQL_TESTS") == "1", "set RAG_RUN_MYSQL_TESTS=1 for isolated MySQL tests")
class MySQLManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env_file = Path(os.environ.get("RAG_MYSQL_ENV_FILE", str(ROOT / ".env.mysql")))
        settings = Settings.from_env(cls.env_file)
        cls.database = "rag_test_" + uuid.uuid4().hex[:12]
        database_name(settings.user)
        container_sql(cls.env_file, f"CREATE DATABASE `{cls.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;\n"
                      f"GRANT ALL PRIVILEGES ON `{cls.database}`.* TO '{settings.user}'@'%';")
        cls.engine = replace(settings, database=cls.database).engine()
        cls.service = ManagementService(cls.engine, ROOT / "tmp/unused_test_storage")
        try:
            cls.service.db_upgrade()
        except Exception:
            cls.engine.dispose()
            container_sql(cls.env_file, f"DROP DATABASE `{cls.database}`;")
            raise

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        if not cls.database.startswith("rag_test_"):
            raise RuntimeError("refusing to drop a non-test database")
        container_sql(cls.env_file, f"DROP DATABASE `{cls.database}`;")

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(update(Document).values(active_build_id=None))
            for model in (ImportRunItem, Chunk, DocumentBuild, DocumentVersion, ImportRun, Document, KnowledgeBase):
                connection.execute(delete(model))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = ManagementService(self.engine, self.root / "storage")
        self.corpus, self.raw = self.root / "corpus", self.root / "raw"
        self.docs, self.chunks = write_corpus(self.corpus)
        (self.raw / "policy").mkdir(parents=True)
        (self.raw / "policy/sample.pdf").write_bytes(b"fixture pdf")
        self.kb = self.service.kb_create("default", "默认知识库")["id"]

    def import_fixture(self):
        return self.service.import_corpus(self.kb, self.corpus, self.raw)

    def counts(self):
        with self.engine.connect() as connection:
            return {model.__tablename__: connection.scalar(select(func.count()).select_from(model))
                    for model in (Document, DocumentVersion, DocumentBuild, Chunk)}

    def add_second_document(self):
        doc, chunk = copy.deepcopy(self.docs[0]), copy.deepcopy(self.chunks[0])
        doc.update(doc_id="second_doc", source_path="policy/second.pdf")
        chunk.update(chunk_id="second_chunk", doc_id="second_doc", source_path="policy/second.pdf")
        self.docs.append(doc)
        self.chunks.append(chunk)
        (self.raw / "policy/second.pdf").write_bytes(b"fixture pdf")
        write_corpus(self.corpus, documents=self.docs, chunks=self.chunks)

    def test_full_corpus_payloads_idempotence_and_explicit_activation(self):
        source = ROOT / "data/benchmark/gold150_hard_v2"
        snapshot = load_snapshot(source)
        original_hashes = dict(snapshot.hashes)
        first = self.service.import_corpus(self.kb, source)
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first["summary_json"], {"documents": 33, "imported": 33, "reused": 0,
                                               "failed": 0, "chunks": 1390, "eligible_chunks": 1308})
        self.assertEqual(self.counts(), {"documents": 33, "document_versions": 33, "document_builds": 33, "chunks": 1390})
        with self.service.sessions() as session:
            self.assertTrue(all(d.active_build_id is None for d in session.scalars(select(Document))))
            builds = {b.legacy_doc_id: b for b in session.scalars(select(DocumentBuild))}
            stored = {c.legacy_chunk_id: c.payload_json for c in session.scalars(select(Chunk))}
            for doc in snapshot.documents:
                self.assertEqual(builds[doc["doc_id"]].document_payload_json, doc)
            for chunks in snapshot.chunks_by_doc.values():
                for chunk in chunks:
                    self.assertEqual(stored[chunk["chunk_id"]], chunk)
        second = self.service.import_corpus(self.kb, source)
        self.assertEqual(second["summary_json"]["reused"], 33)
        self.assertEqual(self.counts()["chunks"], 1390)
        activated = self.service.activate_import(first["id"])
        self.assertEqual(activated["activated"], 33)
        self.assertEqual({n: file_hash(source / n) for n in original_hashes}, original_hashes)
        with self.service.sessions() as session:
            categories = dict(session.execute(select(Document.category, func.count()).group_by(Document.category)).all())
            self.assertEqual(categories, {"policy": 19, "technical": 14})
            self.assertEqual(session.scalar(select(func.count()).select_from(Chunk).where(Chunk.retrieval_enabled.is_(True))), 1308)

    def test_source_and_processing_versions_rollback_and_scoped_chunk_ids(self):
        initial = self.import_fixture()
        first = initial["results_json"][0]
        self.service.activate_import(initial["id"])
        self.docs[0]["warnings"] = ["new chunking settings"]
        self.chunks[0]["text"] += "\n新的标题上下文"
        # Same original ID may appear in two different builds.
        write_corpus(self.corpus, documents=self.docs, chunks=self.chunks)
        changed = self.import_fixture()["results_json"][0]
        self.assertEqual(changed["version_id"], first["version_id"])
        self.assertNotEqual(changed["build_id"], first["build_id"])
        self.assertEqual(self.service.show("document", first["document_id"])["active_build_id"], first["build_id"])
        self.service.activate_document(first["document_id"], changed["build_id"])
        self.service.activate_document(first["document_id"], first["build_id"])
        self.docs[0]["sha256"] = hashlib.sha256(b"changed pdf").hexdigest()
        self.docs[0]["doc_id"] = "new_legacy_doc"
        self.chunks[0]["doc_id"] = "new_legacy_doc"
        (self.raw / "policy/sample.pdf").write_bytes(b"changed pdf")
        write_corpus(self.corpus, documents=self.docs, chunks=self.chunks)
        updated = self.import_fixture()["results_json"][0]
        self.assertEqual(updated["document_id"], first["document_id"])
        self.assertNotEqual(updated["version_id"], first["version_id"])
        self.assertEqual(self.counts(), {"documents": 1, "document_versions": 2, "document_builds": 3, "chunks": 3})

    def test_activation_ownership_nonempty_and_batch_atomicity(self):
        self.add_second_document()
        imported = self.import_fixture()
        a, b = imported["results_json"]
        with self.assertRaisesRegex(ValueError, "belong"):
            self.service.activate_document(a["document_id"], b["build_id"])
        for status in ("failed", "empty"):
            with self.engine.begin() as connection:
                connection.execute(update(DocumentBuild).where(DocumentBuild.id == b["build_id"]).values(status=status))
            with self.assertRaisesRegex(ValueError, "ready"):
                self.service.activate_import(imported["id"])
            self.assertIsNone(self.service.show("document", a["document_id"])["active_build_id"])
        with self.engine.begin() as connection:
            connection.execute(update(DocumentBuild).where(DocumentBuild.id == b["build_id"]).values(status="ready"))
            connection.execute(delete(Chunk).where(Chunk.build_id == b["build_id"]))
        with self.assertRaisesRegex(ValueError, "nonempty"):
            self.service.activate_document(b["document_id"], b["build_id"])

    def test_partial_raw_failure_and_retry(self):
        self.add_second_document()
        (self.raw / "policy/second.pdf").write_bytes(b"wrong")
        imported = self.import_fixture()
        self.assertEqual(imported["status"], "partial_failed")
        self.assertEqual(self.counts()["documents"], 1)
        self.assertIn("checksum", imported["results_json"][1]["error"])
        with self.assertRaisesRegex(ValueError, "fully succeeded"):
            self.service.activate_import(imported["id"])
        (self.raw / "policy/second.pdf").write_bytes(b"fixture pdf")
        retry = self.import_fixture()
        self.assertEqual(retry["summary_json"]["imported"], 1)
        self.assertEqual(retry["summary_json"]["reused"], 1)

    def test_database_failure_rolls_back_entire_document(self):
        def fail_chunks(_connection, _cursor, statement, _parameters, _context, _many):
            if statement.startswith("INSERT INTO chunks"):
                raise RuntimeError("injected chunk write failure")
        event.listen(self.engine, "before_cursor_execute", fail_chunks)
        try:
            run = self.import_fixture()
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_chunks)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.counts(), {"documents": 0, "document_versions": 0, "document_builds": 0, "chunks": 0})
        self.assertEqual(self.import_fixture()["status"], "succeeded")

    def test_validation_failure_is_recorded_without_database_content(self):
        (self.corpus / "chunks.jsonl").write_bytes(b"broken")
        run = self.import_fixture()
        self.assertEqual(run["status"], "failed")
        self.assertIn("checksum", run["error_message"])
        self.assertEqual(self.counts()["documents"], 0)

    def test_status_gates_current_reads_but_preserves_history(self):
        run = self.import_fixture()
        entry = run["results_json"][0]
        self.service.activate_import(run["id"])
        self.service.kb_update(self.kb, name="修改后的知识库", description="说明")
        for kind, identifier in (("document", entry["document_id"]), ("kb", self.kb)):
            self.service.set_status(kind, identifier, "disabled")
            with self.assertRaisesRegex(ValueError, "disabled"):
                self.service.list_records("chunk", document_id=entry["document_id"])
            self.assertEqual(self.service.list_records("chunk", build_id=entry["build_id"])["total"], 1)
            self.service.set_status(kind, identifier, "active")
        with self.engine.begin() as connection:
            with self.assertRaises(DBAPIError) as caught:
                connection.execute(update(KnowledgeBase).where(KnowledgeBase.id == self.kb).values(status="invalid"))
            self.assertEqual(caught.exception.orig.args[0], 3819)

    def test_cli_commands_and_failure_exit_codes(self):
        import contextlib
        import io
        from unittest.mock import patch
        from rag.manage import main

        def call(*arguments, expected=0):
            output, errors = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {"MYSQL_DATABASE": self.database, "RAG_STORAGE_ROOT": str(self.root / "storage")}), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = main(["--env-file", str(self.env_file), *arguments])
            self.assertEqual(result, expected, errors.getvalue())
            return json.loads(output.getvalue() or errors.getvalue())

        self.assertEqual(call("db", "check")["database"], self.database)
        other = call("kb", "create", "--code", "cli", "--name", "CLI 知识库")["id"]
        self.assertEqual(call("kb", "list")["total"], 2)
        self.assertEqual(call("kb", "update", "--kb-id", str(other), "--description", "新说明")["description"], "新说明")
        self.assertEqual(call("kb", "show", "--kb-id", str(other))["code"], "cli")
        run = call("corpus", "import", "--kb-id", str(other), "--source", str(self.corpus), "--raw-root", str(self.raw))
        entry = run["results_json"][0]
        self.assertEqual(call("import", "show", "--import-id", str(run["id"]))["status"], "succeeded")
        self.assertEqual(call("import", "show", "--import-id", str(run["id"]), "--limit", "1", "--offset", "1")["results_json"], [])
        self.assertEqual(call("import", "items", "--import-id", str(run["id"]), "--status", "imported")["total"], 1)
        self.assertEqual(call("import", "activate", "--import-id", str(run["id"]))["activated"], 1)
        document_id, build_id = str(entry["document_id"]), str(entry["build_id"])
        self.assertEqual(call("document", "list", "--kb-id", str(other))["total"], 1)
        self.assertEqual(call("document", "show", "--document-id", document_id)["active_chunk_count"], 1)
        for action in ("versions", "builds"):
            self.assertEqual(call("document", action, "--document-id", document_id)["total"], 1)
        values = call("chunk", "list", "--document-id", document_id, "--eligible-only")
        chunk_id = str(values["items"][0]["id"])
        self.assertEqual(call("chunk", "show", "--chunk-id", chunk_id)["payload_json"], self.chunks[0])
        call("document", "activate", "--document-id", document_id, "--build-id", build_id)
        call("document", "status", "--document-id", document_id, "--status", "archived")
        self.assertIn("error", call("chunk", "list", "--document-id", document_id, expected=1))
        self.assertEqual(call("chunk", "list", "--build-id", build_id)["total"], 1)
        call("kb", "status", "--kb-id", str(other), "--status", "disabled")
        (self.corpus / "chunks.jsonl").write_bytes(b"broken")
        self.assertEqual(call("corpus", "import", "--kb-id", str(other), "--source", str(self.corpus),
                              "--raw-root", str(self.raw), expected=1)["status"], "failed")

    def test_concurrent_multidocument_imports_are_idempotent(self):
        from concurrent.futures import ThreadPoolExecutor
        self.add_second_document()
        with ThreadPoolExecutor(max_workers=2) as executor:
            runs = list(executor.map(lambda _: self.import_fixture(), range(2)))
        self.assertTrue(all(r["status"] == "succeeded" for r in runs), runs)
        self.assertEqual(self.counts(), {"documents": 2, "document_versions": 2, "document_builds": 2, "chunks": 2})
        with self.service.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(ImportRunItem)), 4)
        for run in runs:
            self.assertEqual(run["results_page"]["total"], 2)
            self.assertEqual(run["summary_json"]["imported"] + run["summary_json"]["reused"], 2)

    def test_result_insert_failure_rolls_back_document_and_counters(self):
        injected = False

        def fail_first_item(_connection, _cursor, statement, _parameters, _context, _many):
            nonlocal injected
            if statement.startswith("INSERT INTO import_run_items") and not injected:
                injected = True
                raise RuntimeError("sensitive /private/path password=secret")

        event.listen(self.engine, "before_cursor_execute", fail_first_item)
        try:
            run = self.import_fixture()
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_first_item)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.counts(), {"documents": 0, "document_versions": 0, "document_builds": 0, "chunks": 0})
        self.assertEqual(run["summary_json"]["imported"], 0)
        self.assertEqual(run["summary_json"]["failed"], 1)
        self.assertEqual(run["results_page"]["total"], 1)
        self.assertEqual(run["results_json"][0]["error_code"], "INTERNAL_ERROR")
        self.assertNotIn("secret", json.dumps(run, default=str))

    def test_interruption_leaves_committed_result_and_progress(self):
        from unittest.mock import patch
        from rag.management.storage import publish_raw
        self.add_second_document()

        def interrupt_second(raw_root, source_path, expected, storage_root):
            if source_path == "policy/second.pdf":
                raise KeyboardInterrupt()
            return publish_raw(raw_root, source_path, expected, storage_root)

        with patch("rag.management.service.publish_raw", side_effect=interrupt_second), self.assertRaises(KeyboardInterrupt):
            self.import_fixture()
        with self.service.sessions() as session:
            run_id = session.scalar(select(ImportRun.id))
        run = self.service.show_import(run_id)
        self.assertEqual(run["status"], "running")
        self.assertEqual(run["summary_json"]["documents"], 2)
        self.assertEqual(run["summary_json"]["imported"], 1)
        self.assertEqual(run["results_page"]["total"], 1)
        self.assertEqual(self.counts()["documents"], 1)

    def test_thousand_document_log_writes_pagination_and_activation(self):
        documents, chunks = [], []
        for index in range(1000):
            doc, chunk = copy.deepcopy(self.docs[0]), copy.deepcopy(self.chunks[0])
            path, doc_id = f"policy/{index:04d}.pdf", f"doc_{index}"
            doc.update(doc_id=doc_id, source_path=path)
            chunk.update(chunk_id=f"chunk_{index}", doc_id=doc_id, source_path=path)
            documents.append(doc)
            chunks.append(chunk)
            (self.raw / path).write_bytes(b"fixture pdf")
        write_corpus(self.corpus, documents=documents, chunks=chunks)
        item_writes, summary_sizes = [], []

        def inspect_writes(_connection, _cursor, statement, parameters, _context, _many):
            if statement.startswith("INSERT INTO import_run_items"):
                item_writes.append(1)
            if statement.startswith("UPDATE import_runs SET"):
                self.assertNotIn("results_json", statement)
                if "summary_json" in parameters:
                    summary_sizes.append(len(parameters["summary_json"]))

        event.listen(self.engine, "before_cursor_execute", inspect_writes)
        try:
            run = self.import_fixture()
        finally:
            event.remove(self.engine, "before_cursor_execute", inspect_writes)
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(len(item_writes), 1000)
        self.assertEqual(len(summary_sizes), 1001)
        self.assertLess(max(summary_sizes), 256)
        self.assertEqual(run["results_page"], {"total": 1000, "limit": 100, "offset": 0})
        self.assertEqual(len(run["results_json"]), 100)
        page = self.service.show_import(run["id"], limit=10, offset=995)
        self.assertEqual([item["legacy_doc_id"] for item in page["results_json"]], [f"doc_{i}" for i in range(995, 1000)])
        self.assertEqual(self.service.import_items(run["id"], status="imported")["total"], 1000)
        self.assertEqual(self.service.import_items(run["id"], status="failed")["total"], 0)
        with self.service.sessions() as session:
            self.assertEqual(session.get(ImportRun, run["id"]).results_json, [])
        self.assertEqual(self.service.activate_import(run["id"])["activated"], 1000)
        with self.service.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Document).where(Document.active_build_id.is_not(None))), 1000)

    def test_list_status_validation_for_every_resource(self):
        run = self.import_fixture()
        entry = run["results_json"][0]
        params = {"kb_id": self.kb, "document_id": entry["document_id"], "build_id": entry["build_id"]}
        from rag.management.errors import ManagementError
        from rag.management.service import STATUS_FILTERS
        for kind, statuses in STATUS_FILTERS.items():
            for status in statuses:
                with self.subTest(kind=kind, status=status):
                    self.service.list_records(kind, status=status, **params)
            with self.assertRaises(ManagementError) as caught:
                self.service.list_records(kind, status="wrong", **params)
            self.assertEqual(caught.exception.code, "INVALID_STATUS_FILTER")
        for kind in ("version", "chunk"):
            with self.assertRaises(ManagementError) as caught:
                self.service.list_records(kind, status="active", **params)
            self.assertEqual(caught.exception.code, "UNSUPPORTED_STATUS_FILTER")
        with self.assertRaises(ManagementError) as caught:
            self.service.list_records("unknown")
        self.assertEqual(caught.exception.code, "INVALID_RESOURCE")

    def test_new_import_blocks_destructive_downgrade(self):
        from alembic import command
        run = self.import_fixture()
        with self.assertRaisesRegex(RuntimeError, "pre-upgrade backup"):
            with self.engine.begin() as connection:
                command.downgrade(migration_config(connection), "0001_kb_management")
        self.assertEqual(self.service.show_import(run["id"])["results_page"]["total"], 1)
        # The new migration preflights the older guard before MySQL commits any DDL.
        self.service.db_upgrade()
        self.assertEqual(self.service.db_check()["migration_heads"], ["0003_processing_tasks"])

    def test_legacy_success_failure_running_migration_and_idempotent_backfill(self):
        import importlib.util
        from sqlalchemy import text
        from alembic import command
        from rag.management.models import utcnow
        entry = self.import_fixture()["results_json"][0]
        with self.engine.begin() as connection:
            connection.execute(delete(ImportRunItem))
            connection.execute(delete(ImportRun))
            command.downgrade(migration_config(connection), "0001_kb_management")
        secret = "password=secret mysql://user:secret@host /private/file SQL_PARAMETER"
        failed = {"legacy_doc_id": "bad_doc", "source_path": "policy/bad.pdf", "status": "failed", "error": secret}
        archives = [("succeeded", [entry], {"documents": 1, "imported": 1}),
                    ("partial_failed", [entry, failed], {"documents": 2, "imported": 1, "failed": 1}),
                    ("running", [entry], {"documents": 3, "imported": 1}),
                    ("failed", [], {"completed_documents": 0})]
        identifiers = []
        with self.engine.begin() as connection:
            for status, results, summary in archives:
                statement = text("INSERT INTO import_runs (knowledge_base_id, source_uri, status, summary_json, results_json, "
                                 "error_message, started_at) VALUES (:kb, 'legacy', :status, :summary, :results, :error, :started)")
                result = connection.execute(statement, {"kb": self.kb, "status": status, "summary": json.dumps(summary),
                    "results": json.dumps(results), "error": secret if status == "failed" else None, "started": utcnow()})
                identifiers.append(result.lastrowid)
        self.assertEqual(self.service.db_upgrade()["business_tables"], 9)
        spec = importlib.util.spec_from_file_location("frozen_import_items", ROOT / "migrations/versions/0002_import_run_items.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with self.engine.begin() as connection:
            module.backfill(connection)
            module.backfill(connection)
        with self.service.sessions() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(ImportRunItem)), 4)
            for run_id, (status, archived, summary) in zip(identifiers, archives):
                stored = session.get(ImportRun, run_id)
                self.assertEqual(stored.results_json, archived)
                self.assertEqual(stored.summary_json, summary)
                self.assertEqual(stored.result_storage, "legacy")
                public = self.service.show_import(run_id)
                self.assertEqual(public["status"], status)
                self.assertNotIn("secret", json.dumps(public, default=str))
                self.assertNotIn("SQL_PARAMETER", json.dumps(public, default=str))
        self.assertEqual(self.service.activate_import(identifiers[0])["activated"], 1)
        with self.engine.begin() as connection:
            command.downgrade(migration_config(connection), "0001_kb_management")
        self.service.db_upgrade()
        self.assertEqual(self.service.import_items(identifiers[1], status="failed")["items"][0]["error_code"], "LEGACY_ERROR")

    def test_external_errors_are_safe_in_import_rows_and_queries(self):
        from unittest.mock import patch
        from test_management_errors import exceptions
        for exception in exceptions():
            for boundary in ("load_snapshot", "publish_raw"):
                with self.subTest(exception=type(exception).__name__, boundary=boundary), \
                        patch("rag.management.service." + boundary, side_effect=exception):
                    run = self.import_fixture()
                self.assertEqual(run["status"], "failed")
                if boundary == "load_snapshot":
                    self.assertIsNotNone(run["error_code"])
                    with self.service.sessions() as session:
                        stored = session.get(ImportRun, run["id"])
                        self.assertNotIn("secret-token", stored.error_message)
                else:
                    self.assertIn("error_code", run["results_json"][0])
                    with self.service.sessions() as session:
                        payload = session.scalar(select(ImportRunItem.payload_json).where(ImportRunItem.import_run_id == run["id"]))
                        self.assertNotIn("secret-token", json.dumps(payload))
                rendered = json.dumps(self.service.show_import(run["id"]), default=str)
                for token in ("secret-token", "mysql://", "SQL_PARAMETER", "D:/private"):
                    self.assertNotIn(token, rendered)

    def test_z_migration_roundtrip_and_metadata_match(self):
        from alembic import command
        from alembic.autogenerate import compare_metadata
        with self.engine.connect() as connection:
            from alembic.migration import MigrationContext
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
            self.assertEqual(differences, [])
        with self.engine.begin() as connection:
            command.downgrade(migration_config(connection), "base")
        with self.engine.connect() as connection:
            self.assertNotIn("knowledge_bases", inspect(connection).get_table_names())
        self.assertEqual(self.service.db_upgrade()["business_tables"], 9)


if __name__ == "__main__":
    unittest.main()
