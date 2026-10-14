"""Initial knowledge-base management schema. Frozen; independent of current models."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0001_kb_management"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('knowledge_bases',
    sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
    sa.Column('code', sa.String(length=128, collation='utf8mb4_bin'), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', mysql.LONGTEXT(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.Column('updated_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.CheckConstraint("status IN ('active','disabled','archived')", name='ck_kb_status'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code', name='uq_kb_code'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_table('documents',
    sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
    sa.Column('knowledge_base_id', mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column('source_key', mysql.CHAR(charset='ascii', collation='ascii_bin', length=64), nullable=False),
    sa.Column('source_path', sa.String(length=1024), nullable=False),
    sa.Column('title', mysql.LONGTEXT(), nullable=False),
    sa.Column('category', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('active_build_id', mysql.BIGINT(unsigned=True), nullable=True),
    sa.Column('created_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.Column('updated_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.CheckConstraint("status IN ('active','disabled','archived')", name='ck_document_status'),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('knowledge_base_id', 'source_key', name='uq_document_source'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_index('ix_document_kb_status_id', 'documents', ['knowledge_base_id', 'status', 'id'], unique=False)
    op.create_table('import_runs',
    sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
    sa.Column('knowledge_base_id', mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column('source_uri', sa.String(length=2048), nullable=False),
    sa.Column('snapshot_fingerprint', mysql.CHAR(charset='ascii', collation='ascii_bin', length=64), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('summary_json', mysql.JSON(), nullable=False),
    sa.Column('results_json', mysql.JSON(), nullable=False),
    sa.Column('error_message', mysql.LONGTEXT(), nullable=True),
    sa.Column('started_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.Column('finished_at', mysql.DATETIME(fsp=6), nullable=True),
    sa.CheckConstraint("status IN ('running','succeeded','partial_failed','failed')", name='ck_import_status'),
    sa.ForeignKeyConstraint(['knowledge_base_id'], ['knowledge_bases.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_table('document_versions',
    sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
    sa.Column('document_id', mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column('source_sha256', mysql.CHAR(charset='ascii', collation='ascii_bin', length=64), nullable=False),
    sa.Column('original_filename', sa.String(length=255), nullable=False),
    sa.Column('format', sa.String(length=16), nullable=False),
    sa.Column('file_size', mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column('storage_uri', sa.String(length=2048), nullable=False),
    sa.Column('created_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('document_id', 'source_sha256', name='uq_version_source'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_table('document_builds',
    sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
    sa.Column('document_version_id', mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column('legacy_doc_id', sa.String(length=128, collation='utf8mb4_bin'), nullable=False),
    sa.Column('output_fingerprint', mysql.CHAR(charset='ascii', collation='ascii_bin', length=64), nullable=False),
    sa.Column('pipeline_config_json', mysql.JSON(), nullable=False),
    sa.Column('document_payload_json', mysql.JSON(), nullable=False),
    sa.Column('artifact_manifest_json', mysql.JSON(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.CheckConstraint("status IN ('ready','failed','empty')", name='ck_build_status'),
    sa.ForeignKeyConstraint(['document_version_id'], ['document_versions.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('document_version_id', 'output_fingerprint', name='uq_build_output'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_table('chunks',
    sa.Column('id', mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
    sa.Column('build_id', mysql.BIGINT(unsigned=True), nullable=False),
    sa.Column('legacy_chunk_id', sa.String(length=128, collation='utf8mb4_bin'), nullable=False),
    sa.Column('chunk_index', mysql.INTEGER(unsigned=True), nullable=False),
    sa.Column('body', mysql.LONGTEXT(), nullable=False),
    sa.Column('text', mysql.LONGTEXT(), nullable=False),
    sa.Column('token_count', mysql.INTEGER(unsigned=True), nullable=False),
    sa.Column('structure_type', sa.String(length=64), nullable=False),
    sa.Column('retrieval_enabled', sa.Boolean(), nullable=False),
    sa.Column('payload_json', mysql.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['build_id'], ['document_builds.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('build_id', 'chunk_index', name='uq_chunk_position'),
    sa.UniqueConstraint('build_id', 'legacy_chunk_id', name='uq_chunk_legacy'),
    mysql_charset='utf8mb4',
    mysql_collate='utf8mb4_0900_ai_ci',
    mysql_engine='InnoDB'
    )
    op.create_index('ix_chunk_build_retrieval_position', 'chunks', ['build_id', 'retrieval_enabled', 'chunk_index'], unique=False)
    op.create_foreign_key('fk_document_active_build', 'documents', 'document_builds', ['active_build_id'], ['id'], ondelete='RESTRICT', use_alter=True)

def downgrade():
    op.drop_constraint('fk_document_active_build', 'documents', type_='foreignkey')
    op.drop_table('chunks')
    op.drop_table('document_builds')
    op.drop_table('document_versions')
    op.drop_table('import_runs')
    op.drop_table('documents')
    op.drop_table('knowledge_bases')
