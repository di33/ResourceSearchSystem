ALTER TABLE resource_task
    ADD COLUMN IF NOT EXISTS package_storage_profile_id TEXT NOT NULL DEFAULT '';

ALTER TABLE resource_task
    ADD COLUMN IF NOT EXISTS package_object_key TEXT NOT NULL DEFAULT '';

ALTER TABLE resource_task
    DROP COLUMN IF EXISTS download_object_key,
    DROP COLUMN IF EXISTS download_file_name,
    DROP COLUMN IF EXISTS download_content_type,
    DROP COLUMN IF EXISTS download_file_size;
