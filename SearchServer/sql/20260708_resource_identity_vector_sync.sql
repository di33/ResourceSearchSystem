ALTER TABLE resource_task
    ADD COLUMN IF NOT EXISTS vector_state VARCHAR(32) NOT NULL DEFAULT '';

ALTER TABLE resource_task
    ADD COLUMN IF NOT EXISTS vector_error TEXT NOT NULL DEFAULT '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_resource_task_source_resource
    ON resource_task (source, source_resource_id);

CREATE TABLE IF NOT EXISTS vector_sync_job (
    id SERIAL PRIMARY KEY,
    resource_id VARCHAR(64) NOT NULL,
    action VARCHAR(16) NOT NULL,
    resource_type VARCHAR(32) NOT NULL DEFAULT '',
    vector_json TEXT NOT NULL DEFAULT '',
    state VARCHAR(32) NOT NULL DEFAULT 'pending',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_vector_sync_job_resource_id
    ON vector_sync_job (resource_id);

CREATE INDEX IF NOT EXISTS ix_vector_sync_job_state
    ON vector_sync_job (state);
