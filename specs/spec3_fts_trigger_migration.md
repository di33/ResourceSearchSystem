# Spec 3: FTS 触发器 + 数据迁移 SQL

## 目标
自动维护 search_vector 列，历史数据回填。

## 交付物
1. `Server/sql/fts_setup.sql` — 扩展创建 + 触发器函数 + 触发器 + ALTER TABLE + CREATE INDEX + 回填 UPDATE
2. `Server/app/main.py` — lifespan 中执行 fts_setup.sql

## 详细规格

### 3.1 `Server/sql/fts_setup.sql`

```sql
-- FTS setup: pg_jieba extension, tsvector columns, triggers, backfill
-- Idempotent — safe to run on every startup

-- 1. Ensure pg_jieba extension
CREATE EXTENSION IF NOT EXISTS pg_jieba;

-- 2. Add tsvector columns (idempotent via IF NOT EXISTS in ALTER)
ALTER TABLE resource_task ADD COLUMN IF NOT EXISTS search_vector tsvector;
ALTER TABLE resource_description ADD COLUMN IF NOT EXISTS search_vector tsvector;

-- 3. Create GIN indexes (IF NOT EXISTS)
CREATE INDEX IF NOT EXISTS ix_resource_task_search_vector
    ON resource_task USING gin (search_vector);
CREATE INDEX IF NOT EXISTS ix_resource_description_search_vector
    ON resource_description USING gin (search_vector);

-- 4. Trigger function for resource_task
CREATE OR REPLACE FUNCTION resource_task_search_vector_trigger() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('jieba', COALESCE(NEW.title, '')), 'A') ||
        setweight(to_tsvector('jieba', COALESCE(NEW.pack_name, '')), 'A') ||
        setweight(to_tsvector('jieba', COALESCE(NEW.source_description, '')), 'B') ||
        setweight(to_tsvector('jieba', COALESCE(NEW.category, '')), 'C') ||
        setweight(to_tsvector('jieba', COALESCE(NEW.source, '')), 'D');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 5. Trigger function for resource_description
CREATE OR REPLACE FUNCTION resource_description_search_vector_trigger() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('jieba', COALESCE(NEW.main_content, '')), 'A') ||
        setweight(to_tsvector('jieba', COALESCE(NEW.detail_content, '')), 'B') ||
        setweight(to_tsvector('jieba', COALESCE(NEW.full_description, '')), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 6. Create triggers (drop first for idempotency)
DROP TRIGGER IF EXISTS tsvector_update_resource_task ON resource_task;
CREATE TRIGGER tsvector_update_resource_task
    BEFORE INSERT OR UPDATE ON resource_task
    FOR EACH ROW EXECUTE FUNCTION resource_task_search_vector_trigger();

DROP TRIGGER IF EXISTS tsvector_update_resource_description ON resource_description;
CREATE TRIGGER tsvector_update_resource_description
    BEFORE INSERT OR UPDATE ON resource_description
    FOR EACH ROW EXECUTE FUNCTION resource_description_search_vector_trigger();

-- 7. Backfill existing data
UPDATE resource_task SET search_vector =
    setweight(to_tsvector('jieba', COALESCE(title, '')), 'A') ||
    setweight(to_tsvector('jieba', COALESCE(pack_name, '')), 'A') ||
    setweight(to_tsvector('jieba', COALESCE(source_description, '')), 'B') ||
    setweight(to_tsvector('jieba', COALESCE(category, '')), 'C') ||
    setweight(to_tsvector('jieba', COALESCE(source, '')), 'D')
WHERE search_vector IS NULL;

UPDATE resource_description SET search_vector =
    setweight(to_tsvector('jieba', COALESCE(main_content, '')), 'A') ||
    setweight(to_tsvector('jieba', COALESCE(detail_content, '')), 'B') ||
    setweight(to_tsvector('jieba', COALESCE(full_description, '')), 'C')
WHERE search_vector IS NULL;
```

### 3.2 `Server/app/main.py` 变更

在 lifespan 的数据库初始化部分，`create_all` 之后执行 fts_setup.sql：

```python
import os
from pathlib import Path

_FTS_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "fts_setup.sql"

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    try:
        logger.info("Creating database tables …")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables ready.")
    except Exception as exc:
        logger.warning("Database init deferred (will retry on first request): %s", exc)

    # Execute FTS setup SQL
    try:
        if _FTS_SQL_PATH.exists():
            logger.info("Executing FTS setup SQL …")
            fts_sql = _FTS_SQL_PATH.read_text(encoding="utf-8")
            async with engine.begin() as conn:
                await conn.execute(text(fts_sql))
            logger.info("FTS setup complete.")
        else:
            logger.warning("FTS SQL not found at %s — skipping", _FTS_SQL_PATH)
    except Exception as exc:
        logger.warning("FTS setup deferred (will retry on first request): %s", exc)

    try:
        logger.info("Ensuring Milvus collection …")
        ensure_collection(get_milvus())
    except Exception as exc:
        logger.warning("Milvus init deferred (will retry on first request): %s", exc)

    logger.info("Server ready — %s", "DEBUG mode" if settings.debug else "production mode")
    yield

    # --- shutdown ---
    close_milvus()
    await engine.dispose()
    logger.info("Connections closed.")
```

新增 import:
```python
from sqlalchemy import text
```

## 单元测试
文件: `Server/Test/sql/test_fts_setup.py`

测试触发器函数逻辑（纯 Python 模拟）：

```python
"""Test FTS trigger logic (Python simulation of the SQL trigger functions)."""
import unittest


def simulate_task_search_vector(title="", pack_name="", source_description="", category="", source=""):
    """Simulate resource_task_search_vector_trigger in Python."""
    weights = []
    # A weight: title, pack_name
    if title:
        weights.append(("A", title))
    if pack_name:
        weights.append(("A", pack_name))
    # B weight: source_description
    if source_description:
        weights.append(("B", source_description))
    # C weight: category
    if category:
        weights.append(("C", category))
    # D weight: source
    if source:
        weights.append(("D", source))
    return weights


def simulate_description_search_vector(main_content="", detail_content="", full_description=""):
    """Simulate resource_description_search_vector_trigger in Python."""
    weights = []
    if main_content:
        weights.append(("A", main_content))
    if detail_content:
        weights.append(("B", detail_content))
    if full_description:
        weights.append(("C", full_description))
    return weights


class TestTaskSearchVectorTrigger(unittest.TestCase):

    def test_title_gets_weight_a(self):
        result = simulate_task_search_vector(title="卡通角色素材")
        self.assertEqual(result[0], ("A", "卡通角色素材"))

    def test_pack_name_gets_weight_a(self):
        result = simulate_task_search_vector(pack_name="冒险岛素材包")
        self.assertEqual(result[0], ("A", "冒险岛素材包"))

    def test_source_description_gets_weight_b(self):
        result = simulate_task_search_vector(source_description="一个卡通角色")
        self.assertEqual(result[0], ("B", "一个卡通角色"))

    def test_category_gets_weight_c(self):
        result = simulate_task_search_vector(category="角色")
        self.assertEqual(result[0], ("C", "角色"))

    def test_source_gets_weight_d(self):
        result = simulate_task_search_vector(source="kenny")
        self.assertEqual(result[0], ("D", "kenny"))

    def test_all_fields_produce_correct_weights(self):
        result = simulate_task_search_vector(
            title="卡通", pack_name="素材包", source_description="描述", category="角色", source="kenny"
        )
        weights = [w for w, _ in result]
        self.assertEqual(weights, ["A", "A", "B", "C", "D"])

    def test_empty_fields_produce_empty_result(self):
        result = simulate_task_search_vector()
        self.assertEqual(result, [])


class TestDescriptionSearchVectorTrigger(unittest.TestCase):

    def test_main_content_gets_weight_a(self):
        result = simulate_description_search_vector(main_content="主要内容")
        self.assertEqual(result[0], ("A", "主要内容"))

    def test_detail_content_gets_weight_b(self):
        result = simulate_description_search_vector(detail_content="详细内容")
        self.assertEqual(result[0], ("B", "详细内容"))

    def test_full_description_gets_weight_c(self):
        result = simulate_description_search_vector(full_description="完整描述")
        self.assertEqual(result[0], ("C", "完整描述"))

    def test_all_fields_produce_correct_weights(self):
        result = simulate_description_search_vector(
            main_content="主要", detail_content="详细", full_description="完整"
        )
        weights = [w for w, _ in result]
        self.assertEqual(weights, ["A", "B", "C"])


if __name__ == "__main__":
    unittest.main()
```

## 验证方式
```bash
python -m pytest Server/Test/sql/test_fts_setup.py -v
# Docker 端到端: 启动后检查 search_vector 列是否被自动填充
```
