# Spec 2: ORM Schema 变更 — tsvector 列 + GIN 索引

## 目标
ResourceTask 和 ResourceDescription 增加 search_vector 列和 GIN 索引。

## 交付物
1. `Server/app/models/tables.py` — 增加 TSVECTOR 列 + GIN Index 定义

## 详细规格

### 2.1 `tables.py` 变更

#### 新增 import
```python
from sqlalchemy import TSVECTOR
from sqlalchemy.dialects.postgresql import TSVECTOR as PG_TSVECTOR
```

#### ResourceTask 新增列
```python
search_vector: Mapped[str | None] = mapped_column(
    TSVECTOR, nullable=True, index=False
)
```

#### ResourceTask 新增 GIN 索引
在类定义末尾添加 `__table_args__`:
```python
__table_args__ = (
    Index("ix_resource_task_search_vector", "search_vector", postgresql_using="gin"),
)
```

#### ResourceDescription 新增列
```python
search_vector: Mapped[str | None] = mapped_column(
    TSVECTOR, nullable=True, index=False
)
```

#### ResourceDescription 新增 GIN 索引
```python
__table_args__ = (
    Index("ix_resource_description_search_vector", "search_vector", postgresql_using="gin"),
)
```

### 2.2 完整修改后关键片段

```python
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TSVECTOR,
    func,
)

class ResourceTask(Base):
    __tablename__ = "resource_task"
    __table_args__ = (
        Index("ix_resource_task_search_vector", "search_vector", postgresql_using="gin"),
    )
    # ... 所有现有列不变 ...
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)

class ResourceDescription(Base):
    __tablename__ = "resource_description"
    __table_args__ = (
        Index("ix_resource_description_search_vector", "search_vector", postgresql_using="gin"),
    )
    # ... 所有现有列不变 ...
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
```

## 单元测试
文件: `Server/Test/models/test_tables_fts.py`

```python
"""Test that ORM models define search_vector columns and GIN indexes."""
import unittest

from app.models.tables import Base, ResourceTask, ResourceDescription


class TestSearchVectorColumns(unittest.TestCase):

    def test_resource_task_has_search_vector_column(self):
        columns = {c.name for c in ResourceTask.__table__.columns}
        self.assertIn("search_vector", columns)

    def test_resource_description_has_search_vector_column(self):
        columns = {c.name for c in ResourceDescription.__table__.columns}
        self.assertIn("search_vector", columns)

    def test_resource_task_has_gin_index(self):
        indexes = ResourceTask.__table__.indexes
        gin_indexes = [idx for idx in indexes if idx.name == "ix_resource_task_search_vector"]
        self.assertEqual(len(gin_indexes), 1)
        idx = gin_indexes[0]
        self.assertIn("gin", str(idx.kwargs.get("postgresql_using", "")))

    def test_resource_description_has_gin_index(self):
        indexes = ResourceDescription.__table__.indexes
        gin_indexes = [idx for idx in indexes if idx.name == "ix_resource_description_search_vector"]
        self.assertEqual(len(gin_indexes), 1)
        idx = gin_indexes[0]
        self.assertIn("gin", str(idx.kwargs.get("postgresql_using", "")))


if __name__ == "__main__":
    unittest.main()
```

## 验证方式
```bash
cd G:/ResourceUpload
python -m pytest Server/Test/models/test_tables_fts.py -v
```
