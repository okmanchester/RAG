import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag.chunk import process_document, run
from rag.manage import parser
from rag.management.errors import ManagementError, public_error
from rag.management.processing import native_workspace, processing_config, temporary_error
from rag.management.storage import fingerprint
from test_chunking import fake_parse, middle, node


def config_fixture(root):
    model, native = root / "tokenizer", root / "native"
    model.mkdir()
    native.mkdir()
    (model / "tokenizer.json").write_text("{}", encoding="utf-8")
    (native / "weights.bin").write_bytes(b"model fixture")
    config = root / "mineru.yaml"
    config.write_text(json.dumps({"model": {"base_dir": str(native)},
        "llm_aided": {"api_key": "secret-token", "features": {}}, "table_processing": {"llm_enabled": False}}), encoding="utf-8")
    return {"model_path": model, "config_path": config}


class ProcessingUnitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.options = config_fixture(self.root)

    def test_configuration_identity_excludes_credentials_and_tracks_changes(self):
        config = processing_config(**self.options)
        self.assertNotIn("secret-token", json.dumps(config))
        path = self.options["config_path"]
        data = json.loads(path.read_text())
        data["llm_aided"]["api_key"] = "different-secret"
        path.write_text(json.dumps(data))
        self.assertEqual(config, processing_config(**self.options))
        (self.options["model_path"] / "tokenizer.json").write_text('{"changed":true}')
        self.assertNotEqual(fingerprint(config), fingerprint(processing_config(**self.options)))
        changed = processing_config(**self.options, maximum=500)
        self.assertNotEqual(config["max_tokens"], changed["max_tokens"])

    def test_invalid_config_and_models_return_fixed_errors(self):
        for maximum, overlap in ((0, 0), (400, 400), (400, -1), (True, 0)):
            with self.assertRaises(ManagementError):
                processing_config(**self.options, maximum=maximum, overlap=overlap)
        with self.assertRaises(ManagementError) as caught:
            processing_config(**{**self.options, "model_path": self.root / "missing"})
        self.assertEqual(caught.exception.code, "PROCESSING_MODELS_MISSING")
        data = json.loads(self.options["config_path"].read_text())
        data["llm_aided"]["base_url"] = "https://user:secret-token@example.com"
        self.options["config_path"].write_text(json.dumps(data))
        with self.assertRaises(ManagementError) as caught:
            processing_config(**self.options)
        self.assertNotIn("secret-token", str(caught.exception))

    def test_identity_tracks_orchestration_validation_and_persistence(self):
        from rag.management.storage import file_hash
        baseline = processing_config(**self.options)
        self.assertEqual(baseline["format_version"], 2)
        for name in ("processing", "service", "storage", "repository", "models", "config", "errors"):
            relative = f"management/{name}.py"
            self.assertIn(relative, baseline["implementation_sha256"])
            def changed(path):
                return "f" * 64 if Path(path).as_posix().endswith(relative) else file_hash(path)
            with self.subTest(module=relative), patch("rag.management.processing.file_hash", side_effect=changed):
                self.assertNotEqual(fingerprint(baseline), fingerprint(processing_config(**self.options)))

    def test_native_temporary_files_stay_in_workspace_and_restore_on_failure(self):
        import os
        previous_dir = tempfile.tempdir
        previous_env = {name: os.environ.get(name) for name in ("TEMP", "TMP", "TMPDIR")}
        with self.assertRaises(RuntimeError):
            with native_workspace(self.root):
                with tempfile.TemporaryDirectory() as directory:
                    self.assertTrue(Path(directory).is_relative_to(self.root))
                self.assertEqual(os.environ["TEMP"], str(self.root))
                raise RuntimeError()
        self.assertEqual(tempfile.tempdir, previous_dir)
        self.assertEqual(previous_env, {name: os.environ.get(name) for name in previous_env})

    def test_single_document_matches_batch_payload_and_provenance(self):
        raw = self.root / "raw"
        (raw / "technical").mkdir(parents=True)
        source = raw / "technical/sample.pdf"
        source.write_bytes(b"fixture PDF")
        document = middle([node("doc_title", "标题", level=1), node("text", "这是可以检索的正文。"),
                           node("table", [node("table_body", "<table><tr><th>名称</th><th>说明</th></tr><tr><td>项目</td><td>内容</td></tr></table>")])])
        direct = self.root / "direct"
        direct.mkdir()
        stages = []
        with patch("rag.chunk.parse", side_effect=fake_parse(document)), patch("rag.chunk.TokenCounter", return_value=len):
            payload, chunks = process_document(source, "technical/sample.pdf", direct, len,
                                              config_path=self.options["config_path"], stage=stages.append)
            self.assertEqual(run(raw, self.root / "batch", model_path=self.options["model_path"],
                                 config_path=self.options["config_path"]), 0)
        batch_doc = json.loads((self.root / "batch/documents.jsonl").read_text(encoding="utf-8"))
        batch_chunks = [json.loads(line) for line in (self.root / "batch/chunks.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(payload, batch_doc)
        self.assertEqual(chunks, batch_chunks)
        self.assertEqual(stages, ["parsing", "chunking"])
        self.assertTrue(all(c["token_count"] <= 400 and c["sources"] for c in chunks))

    def test_pipeline_propagates_failures_for_safe_task_boundary(self):
        source = self.root / "sample.pdf"
        source.write_bytes(b"input")
        with patch("rag.chunk.parse", side_effect=RuntimeError("secret-token")):
            with self.assertRaises(RuntimeError):
                process_document(source, "sample.pdf", self.root / "output", len, config_path=self.options["config_path"])
        self.assertNotIn("secret-token", json.dumps(public_error(RuntimeError("secret-token"))))

    def test_temporary_errors_are_classified_by_type_and_status(self):
        import urllib.error
        import httpx
        from sqlalchemy.exc import OperationalError
        self.assertTrue(temporary_error(TimeoutError("secret-token")))
        self.assertTrue(temporary_error(OperationalError(None, None, Exception(2013, "secret-token"))))
        self.assertFalse(temporary_error(OperationalError(None, None, Exception(1045, "secret-token"))))
        self.assertTrue(temporary_error(urllib.error.HTTPError("https://example.com", 429, "rate", {}, None)))
        self.assertFalse(temporary_error(urllib.error.HTTPError("https://example.com", 401, "auth", {}, None)))
        self.assertTrue(temporary_error(httpx.ConnectError("secret-token")))
        self.assertFalse(temporary_error(ValueError("timeout or rate limit")))

    def test_cli_interfaces(self):
        cli = parser()
        for arguments in (
            ["document", "register", "--kb-id", "1", "--raw-root", "data/raw", "--source-path", "policy/a.pdf"],
            ["document", "process", "--version-id", "1", "--max-tokens", "500"],
            ["task", "list", "--kb-id", "1", "--status", "failed"],
            ["task", "show", "--task-id", "1", "--limit", "10"],
            ["task", "retry", "--task-id", "1"], ["worker", "run", "--once"]):
            with self.subTest(arguments=arguments):
                cli.parse_args(arguments)


if __name__ == "__main__":
    unittest.main()
