"""Online backup under concurrent committed publication, using real MySQL snapshots."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
import threading
import unittest
from unittest.mock import patch
import uuid

from sqlalchemy import select
from rag.management.config import Settings
from rag.management.models import Document,DocumentVersion,DocumentBuild
from rag.management.service import ManagementService
from rag.management.storage import file_hash,contained
from scripts import backup_knowledge_base as backups
from test_http_api_integration import HTTPFixture
from test_management_storage import write_corpus


@unittest.skipUnless(os.environ.get("RAG_RUN_MYSQL_TESTS")=="1","enable disposable MySQL backup tests")
class OnlineBackupTests(HTTPFixture,unittest.TestCase):
    def test_snapshot_keeps_old_active_build_while_new_files_and_versions_publish(self):
        corpus,raw=self.root / "corpus",self.root / "raw"
        docs,chunks=write_corpus(corpus)
        (raw / "policy").mkdir(parents=True)
        (raw / "policy/sample.pdf").write_bytes(b"fixture pdf")
        first=self.service.import_corpus(self.kb,corpus,raw)["results_json"][0]
        self.service.activate_document(first["document_id"],first["build_id"])
        dumped,proceed=threading.Event(),threading.Event()
        original=backups.copy_published
        def barrier(source,target):
            dumped.set()
            if not proceed.wait(20):raise AssertionError("publication barrier timed out")
            return original(source,target)
        target="rag_test_online_"+uuid.uuid4().hex[:12]
        try:
            with patch.dict(os.environ,{"MYSQL_DATABASE":self.database,"RAG_STORAGE_ROOT":str(self.service.storage_root),
                                        "RAG_RUNTIME_ROOT":str(self.root / "runtime")}):
                with ThreadPoolExecutor(max_workers=1) as pool,patch.object(backups,"copy_published",side_effect=barrier):
                    future=pool.submit(backups.backup,self.env_file,self.root / "backup")
                    try:
                        self.assertTrue(dumped.wait(20))
                        # Keep staging incomplete throughout copy; it must never be traversed.
                        (self.service.storage_root / "snapshots/.snapshot-paused").mkdir()
                        chunks[0]["text"]+=" 新发布"
                        write_corpus(corpus,documents=docs,chunks=chunks)
                        second=self.service.import_corpus(self.kb,corpus,raw)["results_json"][0]
                        self.service.activate_document(second["document_id"],second["build_id"])
                        # Register another source after the database snapshot; it is absent in restored DB.
                        self.upload(path="policy/new.pdf",data=b"new source")
                    finally:proceed.set()
                    future.result(timeout=30)
                backups.restore(self.env_file,self.root / "backup",target,self.root / "restored")
            engine=replace(Settings.from_env(self.env_file),database=target).engine()
            try:
                restored=ManagementService(engine,self.root / "restored")
                self.assertEqual(restored.show("document",first["document_id"])["active_build_id"],first["build_id"])
                self.assertEqual(restored.list_records("document",kb_id=self.kb)["total"],1)
                self.assertEqual(len(list((self.root / "backup/storage/snapshots").iterdir())),2)
                with engine.connect() as connection:
                    for row in connection.execute(select(DocumentVersion)).mappings():
                        from pathlib import Path
                        self.assertEqual(file_hash(Path(row["storage_uri"])),row["source_sha256"])
                    for row in connection.execute(select(DocumentBuild)).mappings():
                        from pathlib import Path
                        manifest=row["artifact_manifest_json"]
                        if isinstance(manifest,str):manifest=json.loads(manifest)
                        for name,digest in manifest["files_sha256"].items():
                            self.assertEqual(file_hash(contained(Path(manifest["snapshot_uri"]),name)),digest)
            finally:engine.dispose()
            self.assertEqual(self.service.show("document",first["document_id"])["active_build_id"],second["build_id"])
        finally:
            backups.container_sql(self.env_file,f"DROP DATABASE IF EXISTS `{target}`;")
