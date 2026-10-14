"""Append per-document import results; keep the old JSON as a compatibility archive.

Backfill is online and bounded by run/item batches. Definitions below are frozen
and deliberately do not import application models or error catalogues.
"""
from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0002_import_run_items"
down_revision = "0001_kb_management"
branch_labels = None
depends_on = None


def _tables():
    runs = sa.table("import_runs", sa.column("id", mysql.BIGINT(unsigned=True)),
                    sa.column("results_json", mysql.JSON()), sa.column("started_at", mysql.DATETIME(fsp=6)),
                    sa.column("error_message", mysql.LONGTEXT()), sa.column("error_code", sa.String(64)),
                    sa.column("result_storage", sa.String(16)))
    items = sa.table("import_run_items", sa.column("id", mysql.BIGINT(unsigned=True)),
                     sa.column("import_run_id", mysql.BIGINT(unsigned=True)),
                     sa.column("ordinal", mysql.INTEGER(unsigned=True)), sa.column("status", sa.String(16)),
                     sa.column("document_id", mysql.BIGINT(unsigned=True)), sa.column("build_id", mysql.BIGINT(unsigned=True)),
                     sa.column("payload_json", mysql.JSON()), sa.column("created_at", mysql.DATETIME(fsp=6)))
    return runs, items


def backfill(connection):
    """Idempotent, useful when an interrupted MySQL DDL migration is retried."""
    runs, items = _tables()
    after = 0
    while True:
        batch = connection.execute(sa.select(runs.c.id, runs.c.results_json, runs.c.started_at)
                                   .where(runs.c.id > after, runs.c.result_storage == "legacy")
                                   .order_by(runs.c.id).limit(20)).mappings().all()
        if not batch:
            break
        for run in batch:
            archived = run["results_json"]
            if not isinstance(archived, list):
                raise RuntimeError("Invalid historical import result format; restore or repair before retrying")
            for start in range(0, len(archived), 100):
                rows = []
                for ordinal, entry in enumerate(archived[start:start + 100], start + 1):
                    if not isinstance(entry, dict) or entry.get("status") not in ("imported", "reused", "failed"):
                        raise RuntimeError("Invalid historical import item; restore or repair before retrying")
                    status = entry["status"]
                    if status == "failed":
                        payload = {key: entry[key] for key in ("legacy_doc_id", "source_path", "status") if key in entry}
                        payload.update(error_code="LEGACY_ERROR", error="historical error details are withheld")
                    else:
                        payload = {key: value for key, value in entry.items() if key not in ("error", "error_message", "error_code")}
                        if type(entry.get("document_id")) is not int or type(entry.get("build_id")) is not int:
                            raise RuntimeError("Historical success mapping is incomplete; restore or repair before retrying")
                    rows.append({"import_run_id": run["id"], "ordinal": ordinal, "status": status,
                                 "document_id": payload.get("document_id"), "build_id": payload.get("build_id"),
                                 "payload_json": payload, "created_at": run["started_at"]})
                insert = mysql.insert(items)
                connection.execute(insert.on_duplicate_key_update(id=items.c.id), rows)
            after = run["id"]
    connection.execute(runs.update().where(runs.c.error_message.is_not(None), runs.c.error_code.is_(None))
                       .values(error_code="LEGACY_ERROR"))


def upgrade():
    offline = context.is_offline_mode()
    connection = None if offline else op.get_bind()
    inspector = None if offline else sa.inspect(connection)
    columns = set() if offline else {column["name"] for column in inspector.get_columns("import_runs")}
    if "error_code" not in columns:
        op.add_column("import_runs", sa.Column("error_code", sa.String(64), nullable=True))
    if "result_storage" not in columns:
        op.add_column("import_runs", sa.Column("result_storage", sa.String(16), server_default="legacy", nullable=False))
    checks = set() if offline else {c["name"] for c in sa.inspect(connection).get_check_constraints("import_runs")}
    if "ck_import_result_storage" not in checks:
        op.create_check_constraint("ck_import_result_storage", "import_runs", "result_storage IN ('legacy','items')")
    if offline or "import_run_items" not in inspector.get_table_names():
        op.create_table("import_run_items",
            sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
            sa.Column("import_run_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("ordinal", mysql.INTEGER(unsigned=True), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("document_id", mysql.BIGINT(unsigned=True), nullable=True),
            sa.Column("build_id", mysql.BIGINT(unsigned=True), nullable=True),
            sa.Column("payload_json", mysql.JSON(), nullable=False),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.CheckConstraint("status IN ('imported','reused','failed')", name="ck_import_item_status"),
            sa.ForeignKeyConstraint(["import_run_id"], ["import_runs.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["build_id"], ["document_builds.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("import_run_id", "ordinal", name="uq_import_item_ordinal"),
            mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_ai_ci", mysql_engine="InnoDB")
    indexes = set() if offline else {i["name"] for i in sa.inspect(connection).get_indexes("import_run_items")}
    if "ix_import_item_run_status_ordinal" not in indexes:
        op.create_index("ix_import_item_run_status_ordinal", "import_run_items", ["import_run_id", "status", "ordinal"])
    if offline:
        op.execute("-- Historical JSON backfill requires an online Alembic upgrade; this SQL describes schema only.")
    else:
        backfill(connection)


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Downgrade requires an online check for new import records")
    runs, _items = _tables()
    if op.get_bind().scalar(sa.select(sa.func.count()).select_from(runs).where(runs.c.result_storage == "items")):
        raise RuntimeError("New import records exist; restore the pre-upgrade backup instead of dropping import details")
    op.drop_table("import_run_items")
    op.drop_constraint("ck_import_result_storage", "import_runs", type_="check")
    op.drop_column("import_runs", "result_storage")
    op.drop_column("import_runs", "error_code")
