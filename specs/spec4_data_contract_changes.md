# Spec 4: 数据合约变更 — SearchRequest/SearchResultItem/SearchBody

## 目标
搜索接口增加 search_mode、bm25_weight、score 字段。

## 交付物
1. `Server/Scripts/CloudService/search_client.py` — SearchRequest 增加 search_mode/bm25_weight，SearchResultItem 增加 vector_score/bm25_score/rrf_score
2. `Server/app/routers/search.py` — SearchBody/SearchResultOut 增加对应字段
3. `Server/app/config.py` — 增加 bm25_default_weight/search_text_config 配置

## 详细规格

### 4.1 `Server/Scripts/CloudService/search_client.py` 变更

#### SearchRequest 新增字段
```python
@dataclass
class SearchRequest:
    query_text: str
    resource_type: Optional[str] = None
    format_filter: Optional[List[str]] = None
    top_k: int = 10
    similarity_threshold: float = 0.5
    # --- NEW ---
    search_mode: str = "hybrid"  # "vector" | "bm25" | "hybrid"
    bm25_weight: float = 0.5     # RRF 中 BM25 权重 (0-1)
```

#### SearchResultItem 新增字段
```python
@dataclass
class SearchResultItem:
    resource_id: str
    resource_type: str
    score: float  # 最终融合分数 (RRF score 或单一模式分数)
    primary_preview_url: str
    description_summary: str
    file_format: str
    file_size: int
    status: str
    preview_available: bool
    file_download_url: str = ""
    file_count: int = 0
    other_preview_urls: List[str] = field(default_factory=list)
    title: str = ""
    source_resource_id: str = ""
    parent_resource_id: str = ""
    parent_title: str = ""
    parent_preview_url: str = ""
    parent_download_url: str = ""
    child_resource_count: int = 0
    contains_resource_types: List[str] = field(default_factory=list)
    # --- NEW ---
    vector_score: float = 0.0
    bm25_score: float = 0.0
    rrf_score: float = 0.0
```

#### MockSearchClient 同步更新
MockSearchClient.search 方法需要处理 search_mode 字段（可先忽略 bm25，返回原逻辑）。

### 4.2 `Server/app/routers/search.py` 变更

#### SearchBody 新增字段
```python
class SearchBody(BaseModel):
    query_text: str
    resource_type: Optional[str] = None
    format_filter: Optional[List[str]] = None
    top_k: int = 10
    similarity_threshold: float = 0.5
    # --- NEW ---
    search_mode: str = Field(default="hybrid", pattern="^(vector|bm25|hybrid)$")
    bm25_weight: float = Field(default=0.5, ge=0.0, le=1.0)
```

#### SearchResultOut 新增字段
```python
class SearchResultOut(BaseModel):
    resource_id: str
    resource_type: str
    score: float
    # ... 所有现有字段不变 ...
    # --- NEW ---
    vector_score: float = 0.0
    bm25_score: float = 0.0
    rrf_score: float = 0.0
```

#### 端点映射更新
在 `search_resources` 中传递新字段：
```python
req = SearchRequest(
    query_text=body.query_text,
    resource_type=body.resource_type,
    format_filter=body.format_filter,
    top_k=body.top_k,
    similarity_threshold=body.similarity_threshold,
    search_mode=body.search_mode,
    bm25_weight=body.bm25_weight,
)
```

结果映射增加新字段：
```python
SearchResultOut(
    # ... 所有现有字段 ...
    vector_score=r.vector_score,
    bm25_score=r.bm25_score,
    rrf_score=r.rrf_score,
)
```

### 4.3 `Server/app/config.py` 变更

```python
class Settings(BaseSettings):
    # ... 所有现有配置不变 ...

    # BM25 / FTS
    bm25_default_weight: float = 0.5
    search_text_config: str = "jieba"  # PostgreSQL text search config name
```

## 单元测试
文件: `Server/Test/CloudService/test_search_contracts_v2.py`

```python
"""Test updated search data contracts with search_mode, bm25_weight, scores."""
import unittest

from CloudService.search_client import SearchRequest, SearchResultItem, SearchResponse


class TestSearchRequestV2(unittest.TestCase):

    def test_search_mode_default_is_hybrid(self):
        req = SearchRequest(query_text="test")
        self.assertEqual(req.search_mode, "hybrid")

    def test_bm25_weight_default(self):
        req = SearchRequest(query_text="test")
        self.assertAlmostEqual(req.bm25_weight, 0.5)

    def test_search_mode_vector(self):
        req = SearchRequest(query_text="test", search_mode="vector")
        self.assertEqual(req.search_mode, "vector")

    def test_search_mode_bm25(self):
        req = SearchRequest(query_text="test", search_mode="bm25")
        self.assertEqual(req.search_mode, "bm25")


class TestSearchResultItemV2(unittest.TestCase):

    def test_score_fields_default_zero(self):
        item = SearchResultItem(
            resource_id="r1",
            resource_type="texture",
            score=0.9,
            primary_preview_url="",
            description_summary="",
            file_format="png",
            file_size=100,
            status="ready",
            preview_available=True,
        )
        self.assertEqual(item.vector_score, 0.0)
        self.assertEqual(item.bm25_score, 0.0)
        self.assertEqual(item.rrf_score, 0.0)

    def test_score_fields_can_be_set(self):
        item = SearchResultItem(
            resource_id="r1",
            resource_type="texture",
            score=0.9,
            primary_preview_url="",
            description_summary="",
            file_format="png",
            file_size=100,
            status="ready",
            preview_available=True,
            vector_score=0.85,
            bm25_score=12.5,
            rrf_score=0.045,
        )
        self.assertEqual(item.vector_score, 0.85)
        self.assertEqual(item.bm25_score, 12.5)
        self.assertEqual(item.rrf_score, 0.045)

    def test_to_dict_includes_score_fields(self):
        item = SearchResultItem(
            resource_id="r1",
            resource_type="texture",
            score=0.9,
            primary_preview_url="",
            description_summary="",
            file_format="png",
            file_size=100,
            status="ready",
            preview_available=True,
            vector_score=0.85,
            bm25_score=12.5,
            rrf_score=0.045,
        )
        d = item.to_dict()
        self.assertIn("vector_score", d)
        self.assertIn("bm25_score", d)
        self.assertIn("rrf_score", d)


class TestSearchPydanticSchemas(unittest.TestCase):

    def test_search_body_defaults(self):
        from app.routers.search import SearchBody
        body = SearchBody(query_text="test")
        self.assertEqual(body.search_mode, "hybrid")
        self.assertAlmostEqual(body.bm25_weight, 0.5)

    def test_search_body_invalid_mode_rejected(self):
        from app.routers.search import SearchBody
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            SearchBody(query_text="test", search_mode="invalid")

    def test_search_body_bm25_weight_clamped(self):
        from app.routers.search import SearchBody
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            SearchBody(query_text="test", bm25_weight=1.5)
        with self.assertRaises(ValidationError):
            SearchBody(query_text="test", bm25_weight=-0.1)

    def test_search_result_out_has_score_fields(self):
        from app.routers.search import SearchResultOut
        result = SearchResultOut(
            resource_id="r1",
            resource_type="texture",
            score=0.9,
            primary_preview_url="",
            description_summary="",
            file_format="png",
            file_size=100,
            status="ready",
            preview_available=True,
        )
        self.assertEqual(result.vector_score, 0.0)
        self.assertEqual(result.bm25_score, 0.0)
        self.assertEqual(result.rrf_score, 0.0)


class TestConfigBM25Settings(unittest.TestCase):

    def test_bm25_default_weight(self):
        from app.config import Settings
        s = Settings()
        self.assertAlmostEqual(s.bm25_default_weight, 0.5)

    def test_search_text_config(self):
        from app.config import Settings
        s = Settings()
        self.assertEqual(s.search_text_config, "jieba")


if __name__ == "__main__":
    unittest.main()
```

## 验证方式
```bash
python -m pytest Server/Test/CloudService/test_search_contracts_v2.py -v
python -m pytest Server/Test/CloudService/test_search_client.py -v  # 确保不破坏现有测试
```
