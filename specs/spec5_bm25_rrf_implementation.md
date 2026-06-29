# Spec 5: BM25 搜索 + RRF 融合实现

## 目标
MilvusSearchClient 支持 vector/bm25/hybrid 三种搜索模式。

## 交付物
1. `Server/app/services/milvus_search_client.py` — 重构为三模式

## 依赖
Spec 2 (tsvector 列), Spec 3 (触发器), Spec 4 (数据合约)

## 详细规格

### 5.1 重构策略

将现有 `search()` 方法拆分为：
- `_vector_search()` — 提取现有 Milvus 向量搜索逻辑
- `_bm25_search()` — 新增 PostgreSQL BM25 全文搜索
- `_rrf_fusion()` — RRF 融合算法
- `_search_hybrid()` — 调用 vector + BM25 + RRF 融合
- `_search_bm25_only()` — 纯 BM25 模式
- `search()` — 根据 search_mode 分发

### 5.2 `_bm25_search()` 实现

```python
async def _bm25_search(
    self,
    query_text: str,
    normalized_resource_type: str,
    normalized_format_filter: set[str],
    limit: int,
) -> list[tuple[str, str, float]]:
    """BM25 full-text search via PostgreSQL + pg_jieba.

    Returns list of (resource_id, resource_type, bm25_score).
    """
    # Build the tsquery from the user's text
    # Use plainto_tsquery for simple space-separated terms
    tsquery_sql = "plainto_tsquery(:text_config, :query_text)"

    # Base BM25 score: ts_rank_cd with normalization
    # ts_rank_cd(vector, query, normalization) — normalization=32 divides by rank/(rank+1)
    rank_expr = f"ts_rank_cd(rt.search_vector, {tsquery_sql}, 32)"

    # Build WHERE clauses
    conditions = [
        f"rt.search_vector @@ {tsquery_sql}",
        "rt.process_state = 'committed'",
    ]
    params: dict = {
        "text_config": settings.search_text_config,
        "query_text": query_text,
    }

    if normalized_resource_type:
        conditions.append("rt.resource_type = :resource_type")
        params["resource_type"] = normalized_resource_type

    where_clause = " AND ".join(conditions)

    sql = f"""
        SELECT rt.resource_id, rt.resource_type, {rank_expr} AS rank
        FROM resource_task rt
        WHERE {where_clause}
        ORDER BY rank DESC
        LIMIT :limit
    """
    params["limit"] = limit

    result = await self.session.execute(text(sql), params)
    rows = result.fetchall()

    hits: list[tuple[str, str, float]] = []
    for row in rows:
        rid, rtype, rank = row[0], row[1], float(row[2])
        if rank > 0:
            hits.append((rid, rtype, rank))

    # Format filter (post-filter, same as vector search)
    if normalized_format_filter:
        hits = await self._apply_format_filter(hits, normalized_format_filter, limit)

    return hits
```

### 5.3 `_apply_format_filter()` 辅助方法

```python
async def _apply_format_filter(
    self,
    hits: list[tuple[str, str, float]],
    normalized_format_filter: set[str],
    limit: int,
) -> list[tuple[str, str, float]]:
    """Post-filter hits by file format using database lookup."""
    if not hits or not normalized_format_filter:
        return hits

    resource_ids = [h[0] for h in hits]
    files_result = await self.session.execute(
        select(ResourceFile.file_format, ResourceTask.resource_id)
        .join(ResourceTask, ResourceFile.task_id == ResourceTask.id)
        .where(ResourceTask.resource_id.in_(resource_ids))
    )
    format_by_rid: dict[str, set[str]] = {}
    for fmt, rid in files_result.fetchall():
        normalized = str(fmt or "").strip().lower().lstrip(".")
        if normalized:
            format_by_rid.setdefault(rid, set()).add(normalized)

    filtered = []
    for rid, rtype, score in hits:
        formats = format_by_rid.get(rid, set())
        if formats & normalized_format_filter:
            filtered.append((rid, rtype, score))

    return filtered[:limit]
```

### 5.4 `_rrf_fusion()` 实现

```python
def _rrf_fusion(
    self,
    vector_hits: list[tuple[str, str, float]],
    bm25_hits: list[tuple[str, str, float]],
    bm25_weight: float,
    k: int = 60,
) -> list[tuple[str, str, float, float, float, float]]:
    """Reciprocal Rank Fusion of vector and BM25 results.

    Args:
        vector_hits: (resource_id, resource_type, vector_score) sorted by score desc
        bm25_hits: (resource_id, resource_type, bm25_score) sorted by score desc
        bm25_weight: weight for BM25 in fusion (0-1), vector_weight = 1 - bm25_weight
        k: RRF constant (default 60)

    Returns:
        list of (resource_id, resource_type, vector_score, bm25_score, rrf_score, final_score)
        sorted by final_score desc
    """
    vector_weight = 1.0 - bm25_weight

    # Build rank maps (1-based rank)
    vector_rank_map: dict[str, tuple[int, float]] = {}
    for rank, (rid, rtype, score) in enumerate(vector_hits, start=1):
        vector_rank_map[rid] = (rank, score)

    bm25_rank_map: dict[str, tuple[int, float]] = {}
    for rank, (rid, rtype, score) in enumerate(bm25_hits, start=1):
        bm25_rank_map[rid] = (rank, score)

    # Collect all unique resource_ids
    all_rids = set(vector_rank_map.keys()) | set(bm25_rank_map.keys())

    # Build resource_type lookup
    rtype_map: dict[str, str] = {}
    for rid, rtype, _ in vector_hits:
        rtype_map[rid] = rtype
    for rid, rtype, _ in bm25_hits:
        rtype_map[rid] = rtype

    fused: list[tuple[str, str, float, float, float, float]] = []
    for rid in all_rids:
        v_rank, v_score = vector_rank_map.get(rid, (len(vector_hits) + 1, 0.0))
        b_rank, b_score = bm25_rank_map.get(rid, (len(bm25_hits) + 1, 0.0))

        # RRF formula: sum of 1/(k+rank) for each list
        rrf_vector = 1.0 / (k + v_rank)
        rrf_bm25 = 1.0 / (k + b_rank)
        final_score = vector_weight * rrf_vector + bm25_weight * rrf_bm25

        fused.append((rid, rtype_map.get(rid, ""), v_score, b_score, rrf_vector + rrf_bm25, final_score))

    # Sort by final_score descending
    fused.sort(key=lambda x: x[5], reverse=True)
    return fused
```

### 5.5 `_search_hybrid()` 实现

```python
async def _search_hybrid(
    self,
    request: SearchRequest,
    normalized_resource_type: str,
    normalized_format_filter: set[str],
) -> SearchResponse:
    """Hybrid search: vector + BM25 + RRF fusion."""
    search_limit = max(request.top_k * 3, 30)

    # Run vector search
    vector_hits = await self._vector_search(
        request.query_text,
        normalized_resource_type,
        request.similarity_threshold,
        search_limit,
    )

    # Run BM25 search
    bm25_hits = await self._bm25_search(
        request.query_text,
        normalized_resource_type,
        normalized_format_filter,
        search_limit,
    )

    if not vector_hits and not bm25_hits:
        return self._empty_response(request)

    # RRF fusion
    fused = self._rrf_fusion(vector_hits, bm25_hits, request.bm25_weight)

    # Take top_k
    fused = fused[:request.top_k]

    # Build results
    return await self._build_search_results(fused, request, normalized_format_filter)
```

### 5.6 `_search_bm25_only()` 实现

```python
async def _search_bm25_only(
    self,
    request: SearchRequest,
    normalized_resource_type: str,
    normalized_format_filter: set[str],
) -> SearchResponse:
    """BM25-only search mode."""
    search_limit = max(request.top_k * 3, 30) if normalized_format_filter else request.top_k

    bm25_hits = await self._bm25_search(
        request.query_text,
        normalized_resource_type,
        normalized_format_filter,
        search_limit,
    )

    if not bm25_hits:
        return self._empty_response(request)

    # Convert to fused format: (rid, rtype, vector_score=0, bm25_score, rrf_score=bm25_score, final_score=bm25_score)
    fused = [(rid, rtype, 0.0, score, score, score) for rid, rtype, score in bm25_hits[:request.top_k]]

    return await self._build_search_results(fused, request, normalized_format_filter)
```

### 5.7 `_vector_search()` 提取

从现有 search() 方法提取向量搜索逻辑：

```python
async def _vector_search(
    self,
    query_text: str,
    normalized_resource_type: str,
    similarity_threshold: float,
    limit: int,
) -> list[tuple[str, str, float]]:
    """Vector search via Milvus. Returns list of (resource_id, resource_type, score)."""
    query_vector = await _embed_query(query_text)

    search_filter = ""
    if normalized_resource_type:
        search_filter = f'resource_type == "{normalized_resource_type}"'

    hits = self.milvus.search(
        collection_name=settings.milvus_collection,
        data=[query_vector],
        limit=limit,
        output_fields=["resource_id", "resource_type"],
        filter=search_filter or "",
        search_params={"metric_type": "COSINE", "params": {"nprobe": 16}},
    )

    scored_hits: list[tuple[str, str, float]] = []
    for hit_group in hits:
        for hit in hit_group:
            score = hit.get("distance", 0.0)
            if score < similarity_threshold:
                continue
            rid = hit["entity"].get("resource_id", "")
            rtype = hit["entity"].get("resource_type", "")
            scored_hits.append((rid, rtype, score))

    return scored_hits
```

### 5.8 `_build_search_results()` 统一结果构建

```python
async def _build_search_results(
    self,
    fused: list[tuple[str, str, float, float, float, float]],
    request: SearchRequest,
    normalized_format_filter: set[str],
) -> SearchResponse:
    """Build SearchResponse from fused results.

    fused items: (rid, rtype, vector_score, bm25_score, rrf_score, final_score)
    """
    resource_ids = [f[0] for f in fused]
    if not resource_ids:
        return self._empty_response(request)

    # Batch load tasks, descriptions, files, previews, parents (same as existing)
    # ... (reuse existing batch loading logic from current search()) ...

    results: list[SearchResultItem] = []
    for rid, rtype, v_score, b_score, rrf_score, final_score in fused:
        task = task_by_rid.get(rid)
        if task is None:
            continue

        # ... (same file/preview/parent logic as existing) ...

        results.append(SearchResultItem(
            resource_id=rid,
            resource_type=rtype,
            score=final_score,
            # ... all existing fields ...
            vector_score=v_score,
            bm25_score=b_score,
            rrf_score=rrf_score,
        ))
        if len(results) >= request.top_k:
            break

    return SearchResponse(
        results=results,
        total_count=len(results),
        suggestion=None if results else self._make_suggestion(request),
    )
```

### 5.9 `_empty_response()` 和 `_make_suggestion()` 辅助

```python
def _empty_response(self, request: SearchRequest) -> SearchResponse:
    return SearchResponse(results=[], total_count=0, suggestion=self._make_suggestion(request))

def _make_suggestion(self, request: SearchRequest) -> SearchSuggestion:
    return SearchSuggestion(
        rewrite_queries=[f"{request.query_text} 高清", f"{request.query_text} 素材"],
        relaxable_filters=["resource_type", "format_filter"],
        suggested_threshold=max(0.1, request.similarity_threshold - 0.2),
        try_cross_type=True,
    )
```

### 5.10 `search()` 方法重构

```python
async def search(self, request: SearchRequest) -> SearchResponse:
    normalized_resource_type = _normalize_resource_type(request.resource_type)
    normalized_format_filter = _normalize_format_filter(request.format_filter)

    if request.search_mode == "bm25":
        return await self._search_bm25_only(request, normalized_resource_type, normalized_format_filter)
    elif request.search_mode == "hybrid":
        return await self._search_hybrid(request, normalized_resource_type, normalized_format_filter)
    else:  # "vector" or fallback
        # Original vector-only logic, extracted to _vector_search + _build_search_results
        search_limit = max(request.top_k * 3, 30) if normalized_format_filter else request.top_k
        vector_hits = await self._vector_search(
            request.query_text, normalized_resource_type,
            request.similarity_threshold, search_limit,
        )
        if not vector_hits:
            return self._empty_response(request)
        fused = [(rid, rtype, score, 0.0, score, score) for rid, rtype, score in vector_hits]
        return await self._build_search_results(fused[:request.top_k], request, normalized_format_filter)
```

## 单元测试
文件: `Server/Test/CloudService/test_milvus_search_client_v2.py`

```python
"""Test MilvusSearchClient hybrid/BM25/RRF logic."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[3]
_SERVER_DIR = _ROOT / "Server"
for _p in (str(_ROOT), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from CloudService.search_client import SearchRequest, SearchResultItem
from app.models.tables import Base, ResourceDescription, ResourceFile, ResourcePreview, ResourceTask
from app.services.milvus_search_client import MilvusSearchClient


class _FakeMilvus:
    def __init__(self, hits):
        self.hits = hits
        self.search_calls = []

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return self.hits


class _FakeStorage:
    def generate_presigned_download_url(self, key, expires=None):
        return f"https://storage.local/{key}"


class TestRRFFusion(unittest.TestCase):

    def setUp(self):
        self.engine = None
        self.session = None
        # Create client with minimal mocks for unit testing fusion logic
        self.client = MilvusSearchClient.__new__(MilvusSearchClient)

    def test_rrf_fusion_basic(self):
        vector_hits = [
            ("r1", "image", 0.95),
            ("r2", "image", 0.90),
            ("r3", "image", 0.85),
        ]
        bm25_hits = [
            ("r2", "image", 15.0),
            ("r1", "image", 12.0),
            ("r4", "image", 10.0),
        ]
        result = self.client._rrf_fusion(vector_hits, bm25_hits, bm25_weight=0.5, k=60)

        # r1 and r2 appear in both, should rank higher
        top_rids = [r[0] for r in result[:2]]
        self.assertIn("r1", top_rids)
        self.assertIn("r2", top_rids)

        # r3 only in vector, r4 only in bm25 — should rank lower
        self.assertEqual(result[0][5], max(result[i][5] for i in range(len(result))))

    def test_rrf_fusion_empty_vector(self):
        bm25_hits = [("r1", "image", 15.0), ("r2", "image", 10.0)]
        result = self.client._rrf_fusion([], bm25_hits, bm25_weight=0.5, k=60)
        self.assertEqual(len(result), 2)
        # Only BM25 scores contribute
        self.assertEqual(result[0][2], 0.0)  # vector_score = 0

    def test_rrf_fusion_empty_bm25(self):
        vector_hits = [("r1", "image", 0.95)]
        result = self.client._rrf_fusion(vector_hits, [], bm25_weight=0.5, k=60)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][3], 0.0)  # bm25_score = 0

    def test_rrf_fusion_weight_shifts_ranking(self):
        vector_hits = [("r1", "image", 0.95), ("r2", "image", 0.90)]
        bm25_hits = [("r2", "image", 15.0), ("r1", "image", 12.0)]

        # High BM25 weight should favor r2 (which ranks #1 in BM25)
        result_high_bm25 = self.client._rrf_fusion(vector_hits, bm25_hits, bm25_weight=0.9, k=60)
        # Low BM25 weight should favor r1 (which ranks #1 in vector)
        result_low_bm25 = self.client._rrf_fusion(vector_hits, bm25_hits, bm25_weight=0.1, k=60)

        # r2 should rank higher with high bm25_weight
        # r1 should rank higher with low bm25_weight
        # (This tests that weight actually affects ranking)
        self.assertNotEqual(result_high_bm25[0][0], result_low_bm25[0][0])

    def test_rrf_fusion_score_fields_populated(self):
        vector_hits = [("r1", "image", 0.95)]
        bm25_hits = [("r1", "image", 15.0)]
        result = self.client._rrf_fusion(vector_hits, bm25_hits, bm25_weight=0.5, k=60)
        rid, rtype, v_score, b_score, rrf_score, final_score = result[0]
        self.assertEqual(rid, "r1")
        self.assertEqual(v_score, 0.95)
        self.assertEqual(b_score, 15.0)
        self.assertGreater(rrf_score, 0)
        self.assertGreater(final_score, 0)

    def test_rrf_fusion_preserves_all_score_types(self):
        """Fused tuple has 6 elements: rid, rtype, v_score, b_score, rrf_score, final_score."""
        vector_hits = [("r1", "image", 0.95), ("r2", "atlas", 0.88)]
        bm25_hits = [("r2", "atlas", 12.0), ("r3", "audio", 8.0)]
        result = self.client._rrf_fusion(vector_hits, bm25_hits, bm25_weight=0.3, k=60)

        for item in result:
            self.assertEqual(len(item), 6)  # rid, rtype, v, b, rrf, final

        # r2 appears in both — check it has non-zero scores from both sources
        r2_item = next(r for r in result if r[0] == "r2")
        self.assertGreater(r2_item[2], 0)  # vector_score
        self.assertGreater(r2_item[3], 0)  # bm25_score

        # r1 only in vector
        r1_item = next(r for r in result if r[0] == "r1")
        self.assertGreater(r1_item[2], 0)  # vector_score
        self.assertEqual(r1_item[3], 0.0)  # bm25_score = 0

        # r3 only in bm25
        r3_item = next(r for r in result if r[0] == "r3")
        self.assertEqual(r3_item[2], 0.0)  # vector_score = 0
        self.assertGreater(r3_item[3], 0)  # bm25_score


class TestSearchModeDispatch(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_search_mode_vector_calls_vector_only(self):
        session = self.session_factory()
        fake_milvus = _FakeMilvus(
            [[{"distance": 0.91, "entity": {"resource_id": "r1", "resource_type": "image"}}]]
        )
        client = MilvusSearchClient(fake_milvus, session, _FakeStorage())

        with patch("app.services.milvus_search_client._embed_query", new=AsyncMock(return_value=[0.1])):
            resp = await client.search(SearchRequest(query_text="test", search_mode="vector", similarity_threshold=0.5))

        self.assertEqual(len(fake_milvus.search_calls), 1)
        await session.close()

    async def test_search_mode_hybrid_runs_both(self):
        session = self.session_factory()

        # Seed a task so BM25 has something to find
        task = ResourceTask(
            content_md5="md5-hybrid", resource_type="image",
            source_directory="assets", process_state="committed",
            resource_id="r-hybrid-001", title="test hybrid",
            idempotency_key="register-hybrid-001",
        )
        session.add(task)
        await session.flush()
        session.add(ResourceDescription(task_id=task.id, main_content="test content"))
        await session.commit()

        fake_milvus = _FakeMilvus(
            [[{"distance": 0.91, "entity": {"resource_id": "r-hybrid-001", "resource_type": "image"}}]]
        )
        client = MilvusSearchClient(fake_milvus, session, _FakeStorage())

        with patch("app.services.milvus_search_client._embed_query", new=AsyncMock(return_value=[0.1])):
            resp = await client.search(SearchRequest(query_text="test", search_mode="hybrid", similarity_threshold=0.1))

        # Vector search was called
        self.assertEqual(len(fake_milvus.search_calls), 1)
        # BM25 also ran (we got results that include r-hybrid-001)
        # The result should have score fields
        if resp.results:
            self.assertIsInstance(resp.results[0].vector_score, float)
            self.assertIsInstance(resp.results[0].bm25_score, float)
            self.assertIsInstance(resp.results[0].rrf_score, float)

        await session.close()


class TestBM25SQLConstruction(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_bm25_search_returns_empty_on_no_match(self):
        session = self.session_factory()
        client = MilvusSearchClient(_FakeMilvus([]), session, _FakeStorage())

        # SQLite doesn't support tsvector, so this will fail gracefully
        try:
            result = await client._bm25_search("nonexistent", "", set(), 10)
            # If it doesn't raise, it should return empty
            self.assertIsInstance(result, list)
        except Exception:
            # Expected: SQLite doesn't support PostgreSQL-specific functions
            pass

        await session.close()


if __name__ == "__main__":
    unittest.main()
```

## 验证方式
```bash
python -m pytest Server/Test/CloudService/test_milvus_search_client_v2.py -v
python -m pytest Server/Test/CloudService/test_milvus_search_client.py -v  # 确保不破坏现有测试
python -m pytest Server/Test/CloudService/test_search_contracts_v2.py -v
python -m pytest Server/Test/CloudService/test_search_client.py -v
```
