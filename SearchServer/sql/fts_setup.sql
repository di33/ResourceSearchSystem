-- FTS setup: pg_jieba extension, tsvector column, trigger, backfill
-- Idempotent — safe to run on every startup

-- 1. Ensure pg_jieba extension
CREATE EXTENSION IF NOT EXISTS pg_jieba;

-- 1b. Create stop-word dictionary for jiebacfg (only if not already present)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_dict WHERE dictname = 'jieba_stop') THEN
        CREATE TEXT SEARCH DICTIONARY jieba_stop (
            TEMPLATE = simple,
            StopWords = chinese_stopwords
        );
    END IF;
END
$$;
COMMENT ON TEXT SEARCH DICTIONARY jieba_stop IS 'Chinese stop words for pg_jieba';

-- 1c. Add stop-word filtering to jiebacfg token mapping
-- pg_jieba produces Chinese POS types (n, nz, v, uj, m, x, …), not standard PG types.
-- jieba_stop first: stop words → filtered (empty), others → passed through to jieba_stem.
-- Only runs if jieba_stop is not yet in any mapping (first setup or after volume wipe).
DO $$
DECLARE
    t text;
    types text[] := ARRAY[
        'eng','nz','n','m','i','l','d','s','t','mq','nr','j','a','r','b','f',
        'nrt','v','z','ns','q','vn','c','nt','u','o','zg','nrfg','df','p','g',
        'y','ad','vg','ng','x','ul','k','ag','dg','rr','rg','an','vq','e','uv',
        'tg','mg','ud','vi','vd','uj','uz','h','ug','rz'
    ];
BEGIN
    -- Skip if jieba_stop is already mapped for any token type
    IF EXISTS (
        SELECT 1 FROM pg_ts_config_map m
        JOIN pg_ts_config c ON m.mapcfg = c.oid
        JOIN pg_ts_dict d ON d.oid = m.mapdict
        WHERE c.cfgname = 'jiebacfg' AND d.dictname = 'jieba_stop'
    ) THEN
        RETURN;
    END IF;
    FOREACH t IN ARRAY types LOOP
        EXECUTE format('ALTER TEXT SEARCH CONFIGURATION jiebacfg DROP MAPPING IF EXISTS FOR %I', t);
        EXECUTE format('ALTER TEXT SEARCH CONFIGURATION jiebacfg ADD MAPPING FOR %I WITH jieba_stop, jieba_stem', t);
    END LOOP;
END
$$;

-- 2. Add tsvector column to resource_description (idempotent via IF NOT EXISTS in ALTER)
ALTER TABLE resource_description ADD COLUMN IF NOT EXISTS usage_space varchar(16) NOT NULL DEFAULT '';
ALTER TABLE resource_description ADD COLUMN IF NOT EXISTS usage_category varchar(64) NOT NULL DEFAULT '';
ALTER TABLE resource_description ADD COLUMN IF NOT EXISTS usage_subcategories_json text NOT NULL DEFAULT '[]';
ALTER TABLE resource_description ADD COLUMN IF NOT EXISTS usage_classification_reason text NOT NULL DEFAULT '';
ALTER TABLE resource_description ADD COLUMN IF NOT EXISTS usage_classification_suggestion_json text NOT NULL DEFAULT '{}';
ALTER TABLE resource_description ADD COLUMN IF NOT EXISTS usage_classification_version varchar(64) NOT NULL DEFAULT '';
ALTER TABLE resource_description ADD COLUMN IF NOT EXISTS search_vector tsvector;

-- 3. Create GIN index (IF NOT EXISTS)
CREATE INDEX IF NOT EXISTS ix_resource_description_search_vector
    ON resource_description USING gin (search_vector);

-- 4. Trigger function retained for manual compatibility, but synchronous
-- triggers are disabled for high-throughput ingestion. The API backfills
-- search_vector in a background worker instead.
CREATE OR REPLACE FUNCTION resource_description_search_vector_trigger() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('jiebacfg', COALESCE(NEW.full_description, '')), 'A');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 5. Drop the legacy synchronous trigger if it exists.
DROP TRIGGER IF EXISTS tsvector_update_resource_description ON resource_description;

-- Backfill: run manually via `python SearchServer/Scripts/backfill_fts.py` from the repo root,
-- or `python Scripts/backfill_fts.py` from the server folder, when needed.
