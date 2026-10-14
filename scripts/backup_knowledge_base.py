"""Binary-safe MySQL + local storage backup/restore through Docker Compose.

Run from the project root; restore only into a different database and new storage root.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag.management.config import Settings
from rag.management.errors import ManagementError, public_error
from rag.management.storage import file_hash


def database_name(name):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
        raise ManagementError("INVALID_DATABASE_NAME")
    return name


def docker_command(env_file, executable, arguments=()):
    # Password expansion happens inside the container, never in the host command line/output.
    return ["docker", "compose", "--env-file", str(Path(env_file).resolve()), "exec", "-T", "mysql",
            "sh", "-c", 'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec "$@"', "sh", executable,
            "-u", "root", *arguments]


def container_sql(env_file, sql, database=None):
    args = ["--batch", "--skip-column-names"] + ([database_name(database)] if database else [])
    result = subprocess.run(docker_command(env_file, "mysql", args), input=sql.encode(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
    if result.returncode:
        raise ManagementError("MYSQL_COMMAND_FAILED")
    return result.stdout.decode("utf-8").strip()


def backup(env_file, output: Path):
    settings = Settings.from_env(env_file)
    database_name(settings.database)
    output = output.resolve()
    if output.exists():
        raise ManagementError("BACKUP_EXISTS")
    if output.is_relative_to(settings.storage_root.resolve()):
        raise ManagementError("BACKUP_INSIDE_STORAGE")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Caller must pause DB writes/DDL, storage cleanup and manual file mutations.
    # Published storage is append-only. This contract is not an enforced lock;
    # deletion/GC must join a shared backup coordination protocol before introduction.
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=output.parent) as temporary:
        staged = Path(temporary)
        with (staged / "database.sql").open("wb") as stream:
            result = subprocess.run(docker_command(env_file, "mysqldump", ["--single-transaction",
                "--set-gtid-purged=OFF", "--no-tablespaces", "--hex-blob", settings.database]),
                stdout=stream, stderr=subprocess.PIPE, cwd=ROOT)
        if result.returncode:
            raise ManagementError("BACKUP_DUMP_FAILED")
        if settings.storage_root.is_dir():
            shutil.copytree(settings.storage_root, staged / "storage")
        else:
            (staged / "storage").mkdir()
        hashes = {p.relative_to(staged).as_posix(): file_hash(p) for p in sorted(staged.rglob("*")) if p.is_file()}
        (staged / "backup.json").write_text(json.dumps({"database": settings.database,
            "storage_root": settings.storage_root.resolve().as_posix(), "files_sha256": hashes,
            "consistency_contract": {"database_and_storage_mutations": "paused_by_operator",
                                     "published_storage": "append_only_no_overwrite_or_delete",
                                     "coordination_enforced": False,
                                     "storage_may_include_unreferenced_files": True}}, indent=2), encoding="utf-8")
        staged.rename(output)
    return {"backup": output.as_posix(), "files": len(hashes)}


def restore(env_file, source: Path, database: str, storage_root: Path):
    from sqlalchemy import create_engine, text
    settings = Settings.from_env(env_file)
    database_name(database)
    source, storage_root = source.resolve(), storage_root.resolve()
    if database == settings.database or storage_root == settings.storage_root.resolve() or storage_root.exists():
        raise ManagementError("RESTORE_NOT_ISOLATED")
    manifest = json.loads((source / "backup.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files_sha256"].items():
        from rag.management.storage import contained
        if file_hash(contained(source, name)) != digest:
            raise ManagementError("BACKUP_CHECKSUM")
    if container_sql(env_file, "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='" + database + "';"):
        raise ManagementError("RESTORE_DATABASE_EXISTS")
    database_name(settings.user)
    container_sql(env_file, f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;\n"
                           f"GRANT ALL PRIVILEGES ON `{database}`.* TO '{settings.user}'@'%';")
    with (source / "database.sql").open("rb") as stream:
        result = subprocess.run(docker_command(env_file, "mysql", [database]), stdin=stream,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=ROOT)
    if result.returncode:
        raise ManagementError("RESTORE_FAILED")
    shutil.copytree(source / "storage", storage_root)
    # Rebind only the restored database's managed file references to its isolated storage copy.
    engine = create_engine(settings.url.set(database=database), hide_parameters=True)
    old_root = Path(manifest["storage_root"])

    def rebase(uri):
        old = Path(uri)
        if not old.is_relative_to(old_root):
            raise ManagementError("RESTORE_PATH_ESCAPE")
        return (storage_root / old.relative_to(old_root)).as_posix()

    try:
        with engine.begin() as connection:
            for row in connection.execute(text("SELECT id, storage_uri FROM document_versions")).mappings().all():
                connection.execute(text("UPDATE document_versions SET storage_uri=:uri WHERE id=:id"),
                                   {"id": row["id"], "uri": rebase(row["storage_uri"])})
            for row in connection.execute(text("SELECT id, artifact_manifest_json FROM document_builds")).mappings().all():
                artifacts = json.loads(row["artifact_manifest_json"])
                for key in ("snapshot_uri", "raw_uri"):
                    if key in artifacts:
                        artifacts[key] = rebase(artifacts[key])
                connection.execute(text("UPDATE document_builds SET artifact_manifest_json=:value WHERE id=:id"),
                                   {"id": row["id"], "value": json.dumps(artifacts, ensure_ascii=False)})
            # Restored workers must not inherit ownership from the source database.
            from sqlalchemy import inspect
            if "processing_tasks" in inspect(connection).get_table_names():
                connection.execute(text("UPDATE processing_attempts SET status='interrupted', "
                    "error_code='TASK_INTERRUPTED', finished_at=UTC_TIMESTAMP(6) WHERE status='running'"))
                connection.execute(text("UPDATE processing_tasks SET status='pending', stage='validation', "
                    "error_code='TASK_INTERRUPTED', scheduled_at=UTC_TIMESTAMP(6), finished_at=NULL "
                    "WHERE status='running'"))
                connection.execute(text("UPDATE processing_tasks SET execution_token=NULL, lease_expires_at=NULL"))
    finally:
        engine.dispose()
    return {"database": database, "storage_root": storage_root.as_posix(), "status": "restored"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.mysql")
    commands = parser.add_subparsers(dest="action", required=True)
    command = commands.add_parser("backup")
    command.add_argument("--output", type=Path, required=True)
    command = commands.add_parser("restore")
    command.add_argument("--input", type=Path, required=True)
    command.add_argument("--database", required=True)
    command.add_argument("--storage-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = backup(args.env_file, args.output) if args.action == "backup" else restore(
            args.env_file, args.input, args.database, args.storage_root)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps(public_error(exc), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
