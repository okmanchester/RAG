CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001_kb_management

CREATE TABLE knowledge_bases (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, 
    code VARCHAR(128) COLLATE utf8mb4_bin NOT NULL, 
    name VARCHAR(255) NOT NULL, 
    description LONGTEXT NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    created_at DATETIME(6) NOT NULL, 
    updated_at DATETIME(6) NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_kb_status CHECK (status IN ('active','disabled','archived')), 
    CONSTRAINT uq_kb_code UNIQUE (code)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE documents (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, 
    knowledge_base_id BIGINT UNSIGNED NOT NULL, 
    source_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, 
    source_path VARCHAR(1024) NOT NULL, 
    title LONGTEXT NOT NULL, 
    category VARCHAR(128) NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    active_build_id BIGINT UNSIGNED, 
    created_at DATETIME(6) NOT NULL, 
    updated_at DATETIME(6) NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_document_status CHECK (status IN ('active','disabled','archived')), 
    FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_document_source UNIQUE (knowledge_base_id, source_key)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE INDEX ix_document_kb_status_id ON documents (knowledge_base_id, status, id);

CREATE TABLE import_runs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, 
    knowledge_base_id BIGINT UNSIGNED NOT NULL, 
    source_uri VARCHAR(2048) NOT NULL, 
    snapshot_fingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin, 
    status VARCHAR(16) NOT NULL, 
    summary_json JSON NOT NULL, 
    results_json JSON NOT NULL, 
    error_message LONGTEXT, 
    started_at DATETIME(6) NOT NULL, 
    finished_at DATETIME(6), 
    PRIMARY KEY (id), 
    CONSTRAINT ck_import_status CHECK (status IN ('running','succeeded','partial_failed','failed')), 
    FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases (id) ON DELETE RESTRICT
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE document_versions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, 
    document_id BIGINT UNSIGNED NOT NULL, 
    source_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, 
    original_filename VARCHAR(255) NOT NULL, 
    format VARCHAR(16) NOT NULL, 
    file_size BIGINT UNSIGNED NOT NULL, 
    storage_uri VARCHAR(2048) NOT NULL, 
    created_at DATETIME(6) NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_version_source UNIQUE (document_id, source_sha256)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE document_builds (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, 
    document_version_id BIGINT UNSIGNED NOT NULL, 
    legacy_doc_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL, 
    output_fingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, 
    pipeline_config_json JSON NOT NULL, 
    document_payload_json JSON NOT NULL, 
    artifact_manifest_json JSON NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    created_at DATETIME(6) NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_build_status CHECK (status IN ('ready','failed','empty')), 
    FOREIGN KEY(document_version_id) REFERENCES document_versions (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_build_output UNIQUE (document_version_id, output_fingerprint)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE TABLE chunks (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, 
    build_id BIGINT UNSIGNED NOT NULL, 
    legacy_chunk_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL, 
    chunk_index INTEGER UNSIGNED NOT NULL, 
    body LONGTEXT NOT NULL, 
    text LONGTEXT NOT NULL, 
    token_count INTEGER UNSIGNED NOT NULL, 
    structure_type VARCHAR(64) NOT NULL, 
    retrieval_enabled BOOL NOT NULL, 
    payload_json JSON NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(build_id) REFERENCES document_builds (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_chunk_position UNIQUE (build_id, chunk_index), 
    CONSTRAINT uq_chunk_legacy UNIQUE (build_id, legacy_chunk_id)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE INDEX ix_chunk_build_retrieval_position ON chunks (build_id, retrieval_enabled, chunk_index);

ALTER TABLE documents ADD CONSTRAINT fk_document_active_build FOREIGN KEY(active_build_id) REFERENCES document_builds (id) ON DELETE RESTRICT;

INSERT INTO alembic_version (version_num) VALUES ('0001_kb_management');

-- Running upgrade 0001_kb_management -> 0002_import_run_items

ALTER TABLE import_runs ADD COLUMN error_code VARCHAR(64);

ALTER TABLE import_runs ADD COLUMN result_storage VARCHAR(16) NOT NULL DEFAULT 'legacy';

ALTER TABLE import_runs ADD CONSTRAINT ck_import_result_storage CHECK (result_storage IN ('legacy','items'));

CREATE TABLE import_run_items (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT, 
    import_run_id BIGINT UNSIGNED NOT NULL, 
    ordinal INTEGER UNSIGNED NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    document_id BIGINT UNSIGNED, 
    build_id BIGINT UNSIGNED, 
    payload_json JSON NOT NULL, 
    created_at DATETIME(6) NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT ck_import_item_status CHECK (status IN ('imported','reused','failed')), 
    FOREIGN KEY(import_run_id) REFERENCES import_runs (id) ON DELETE RESTRICT, 
    FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE RESTRICT, 
    FOREIGN KEY(build_id) REFERENCES document_builds (id) ON DELETE RESTRICT, 
    CONSTRAINT uq_import_item_ordinal UNIQUE (import_run_id, ordinal)
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE INDEX ix_import_item_run_status_ordinal ON import_run_items (import_run_id, status, ordinal);

-- Historical JSON backfill requires an online Alembic upgrade; this SQL describes schema only.;

UPDATE alembic_version SET version_num='0002_import_run_items' WHERE alembic_version.version_num = '0001_kb_management';

-- Running upgrade 0002_import_run_items -> 0003_processing_tasks

CREATE TABLE processing_tasks (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    document_version_id BIGINT UNSIGNED NOT NULL,
    config_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    config_json JSON NOT NULL,
    status VARCHAR(16) NOT NULL,
    stage VARCHAR(16) NOT NULL,
    build_id BIGINT UNSIGNED,
    attempt_count INTEGER UNSIGNED NOT NULL,
    cycle_attempts INTEGER UNSIGNED NOT NULL,
    scheduled_at DATETIME(6) NOT NULL,
    execution_token VARCHAR(32),
    lease_expires_at DATETIME(6),
    error_code VARCHAR(64),
    finished_at DATETIME(6),
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(document_version_id) REFERENCES document_versions (id) ON DELETE RESTRICT,
    FOREIGN KEY(build_id) REFERENCES document_builds (id) ON DELETE RESTRICT,
    CONSTRAINT uq_task_version_config UNIQUE (document_version_id, config_sha256),
    CONSTRAINT ck_task_status CHECK (status IN ('pending','running','succeeded','failed')),
    CONSTRAINT ck_task_stage CHECK (stage IN ('validation','parsing','chunking','publishing','persisting'))
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

CREATE INDEX ix_task_schedule ON processing_tasks (status, scheduled_at, id);

CREATE INDEX ix_task_lease ON processing_tasks (status, lease_expires_at);

CREATE TABLE processing_attempts (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    task_id BIGINT UNSIGNED NOT NULL,
    ordinal INTEGER UNSIGNED NOT NULL,
    execution_token VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL,
    stage VARCHAR(16) NOT NULL,
    started_at DATETIME(6) NOT NULL,
    finished_at DATETIME(6),
    error_code VARCHAR(64),
    PRIMARY KEY (id),
    FOREIGN KEY(task_id) REFERENCES processing_tasks (id) ON DELETE RESTRICT,
    CONSTRAINT uq_attempt_ordinal UNIQUE (task_id, ordinal),
    CONSTRAINT ck_attempt_status CHECK (status IN ('running','succeeded','failed','interrupted')),
    CONSTRAINT ck_attempt_stage CHECK (stage IN ('validation','parsing','chunking','publishing','persisting'))
)CHARSET=utf8mb4 ENGINE=InnoDB COLLATE utf8mb4_0900_ai_ci;

UPDATE alembic_version SET version_num='0003_processing_tasks' WHERE alembic_version.version_num = '0002_import_run_items';

