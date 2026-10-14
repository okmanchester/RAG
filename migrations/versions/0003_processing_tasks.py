"""Durable processing queue; definitions frozen independently of application models."""
from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0003_processing_tasks"
down_revision = "0002_import_run_items"
branch_labels = None
depends_on = None

OPTIONS = dict(mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_ai_ci", mysql_engine="InnoDB")


def upgrade():
    offline = context.is_offline_mode()
    existing = set() if offline else set(sa.inspect(op.get_bind()).get_table_names())
    if "processing_tasks" not in existing:
        op.create_table("processing_tasks",
            sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
            sa.Column("document_version_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("config_sha256", mysql.CHAR(64, charset="ascii", collation="ascii_bin"), nullable=False),
            sa.Column("config_json", mysql.JSON(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("stage", sa.String(16), nullable=False),
            sa.Column("build_id", mysql.BIGINT(unsigned=True), nullable=True),
            sa.Column("attempt_count", mysql.INTEGER(unsigned=True), nullable=False),
            sa.Column("cycle_attempts", mysql.INTEGER(unsigned=True), nullable=False),
            sa.Column("scheduled_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("execution_token", sa.String(32), nullable=True),
            sa.Column("lease_expires_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.ForeignKeyConstraint(["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["build_id"], ["document_builds.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint("document_version_id", "config_sha256", name="uq_task_version_config"),
            sa.CheckConstraint("status IN ('pending','running','succeeded','failed')", name="ck_task_status"),
            sa.CheckConstraint("stage IN ('validation','parsing','chunking','publishing','persisting')", name="ck_task_stage"),
            **OPTIONS)
    indexes = set() if offline else {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("processing_tasks")}
    for name, columns in (("ix_task_schedule", ["status", "scheduled_at", "id"]),
                          ("ix_task_lease", ["status", "lease_expires_at"])):
        if name not in indexes:
            op.create_index(name, "processing_tasks", columns)
    if "processing_attempts" not in existing:
        op.create_table("processing_attempts",
            sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
            sa.Column("task_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("ordinal", mysql.INTEGER(unsigned=True), nullable=False),
            sa.Column("execution_token", sa.String(32), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("stage", sa.String(16), nullable=False),
            sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.ForeignKeyConstraint(["task_id"], ["processing_tasks.id"], ondelete="RESTRICT"),
            sa.UniqueConstraint("task_id", "ordinal", name="uq_attempt_ordinal"),
            sa.CheckConstraint("status IN ('running','succeeded','failed','interrupted')", name="ck_attempt_status"),
            sa.CheckConstraint("stage IN ('validation','parsing','chunking','publishing','persisting')", name="ck_attempt_stage"),
            **OPTIONS)


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Downgrade requires an online processing task check")
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM processing_tasks")):
        raise RuntimeError("Processing tasks exist; restore the pre-upgrade backup instead of dropping task history")
    # MySQL DDL commits implicitly. Refuse the older migration's known data-loss
    # condition before dropping these tables, so a rejected multi-step downgrade
    # cannot leave the version marker pointing at tables already removed.
    destination = context.get_revision_argument()
    if destination not in (down_revision, "-1") and op.get_bind().scalar(sa.text(
            "SELECT COUNT(*) FROM import_runs WHERE result_storage='items'")):
        raise RuntimeError("New import records exist; restore the pre-upgrade backup instead of downgrading import details")
    op.drop_table("processing_attempts")
    op.drop_table("processing_tasks")
