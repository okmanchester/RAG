"""Durable coalesced index synchronization; schema frozen independently of models."""
from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0004_index_sync"
down_revision = "0003_processing_tasks"
branch_labels = None
depends_on = None
OPTIONS = dict(mysql_charset="utf8mb4", mysql_collate="utf8mb4_0900_ai_ci", mysql_engine="InnoDB")


def upgrade():
    offline = context.is_offline_mode()
    existing = set() if offline else set(sa.inspect(op.get_bind()).get_table_names())
    if "index_sync_states" not in existing:
        op.create_table("index_sync_states",
            sa.Column("knowledge_base_id", mysql.BIGINT(unsigned=True), primary_key=True),
            sa.Column("desired_revision", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("applied_revision", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("paused", sa.Boolean(), nullable=False),
            sa.Column("scheduled_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("attempt_count", mysql.INTEGER(unsigned=True), nullable=False),
            sa.Column("cycle_attempts", mysql.INTEGER(unsigned=True), nullable=False),
            sa.Column("execution_token", sa.String(32), nullable=True),
            sa.Column("lease_expires_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("generation", sa.String(32), nullable=True),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("updated_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], ondelete="RESTRICT"),
            sa.CheckConstraint("status IN ('pending','running','succeeded','failed')", name="ck_index_sync_status"),
            sa.CheckConstraint("applied_revision <= desired_revision", name="ck_index_sync_revision"), **OPTIONS)
    indexes = set() if offline else {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("index_sync_states")}
    for name, columns in (("ix_index_sync_schedule", ["paused", "status", "scheduled_at", "knowledge_base_id"]),
                          ("ix_index_sync_lease", ["status", "lease_expires_at"])):
        if name not in indexes:
            op.create_index(name, "index_sync_states", columns)
    if "index_sync_attempts" not in existing:
        op.create_table("index_sync_attempts",
            sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
            sa.Column("knowledge_base_id", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("ordinal", mysql.INTEGER(unsigned=True), nullable=False),
            sa.Column("target_revision", mysql.BIGINT(unsigned=True), nullable=False),
            sa.Column("execution_token", sa.String(32), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("manual", sa.Boolean(), nullable=False),
            sa.Column("generation", sa.String(32), nullable=True),
            sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=False),
            sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.ForeignKeyConstraint(["knowledge_base_id"], ["index_sync_states.knowledge_base_id"], ondelete="RESTRICT"),
            sa.UniqueConstraint("knowledge_base_id", "ordinal", name="uq_index_sync_attempt"),
            sa.CheckConstraint("status IN ('running','succeeded','failed','interrupted','superseded','deferred')",
                               name="ck_index_sync_attempt_status"), **OPTIONS)
    # Also safe to rerun after MySQL's implicit DDL commits interrupted an upgrade.
    op.execute(sa.text("""INSERT INTO index_sync_states
        (knowledge_base_id, desired_revision, applied_revision, status, paused, scheduled_at,
         attempt_count, cycle_attempts, created_at, updated_at)
        SELECT id, 1, 0, 'pending', 0, UTC_TIMESTAMP(6), 0, 0, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6)
        FROM knowledge_bases WHERE id NOT IN (SELECT knowledge_base_id FROM index_sync_states)"""))


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Downgrade requires an online index synchronization history check")
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT COUNT(*) FROM index_sync_attempts")):
        raise RuntimeError("Index attempts exist; restore the pre-upgrade backup instead of dropping history")
    destination = context.get_revision_argument()
    if destination not in (down_revision, "-1"):
        if connection.scalar(sa.text("SELECT COUNT(*) FROM processing_tasks")) or (
                destination != "0002_import_run_items" and connection.scalar(sa.text(
                    "SELECT COUNT(*) FROM import_runs WHERE result_storage='items'"))):
            raise RuntimeError("Historical tasks/imports exist; restore the pre-upgrade backup instead of downgrading")
    op.drop_table("index_sync_attempts")
    op.drop_table("index_sync_states")
