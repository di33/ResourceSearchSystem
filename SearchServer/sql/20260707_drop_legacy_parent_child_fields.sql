ALTER TABLE resource_task
    DROP COLUMN IF EXISTS parent_resource_id,
    DROP COLUMN IF EXISTS parent_source_resource_id,
    DROP COLUMN IF EXISTS child_resource_ids_json,
    DROP COLUMN IF EXISTS child_source_resource_ids_json,
    DROP COLUMN IF EXISTS child_resource_count,
    DROP COLUMN IF EXISTS contains_resource_types_json;
