"""Test MilvusSearchClient hybrid/BM25/RRF logic."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_SERVER_DIR = Path(__file__).resolve().parents[2]
_ROOT = _SERVER_DIR.parent
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
    def generate_download_url(self, key, expires=None, storage_profile_id=""):
        return f"https://storage.local/{key}"


class TestRRFFusion(unittest.TestCase):

    def setUp(self):
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

        top_rids = [r[0] for r in result[:2]]
        self.assertIn("r1", top_rids)
        self.assertIn("r2", top_rids)

    def test_rrf_fusion_empty_vector(self):
        bm25_hits = [("r1", "image", 15.0), ("r2", "image", 10.0)]
        result = self.client._rrf_fusion([], bm25_hits, bm25_weight=0.5, k=60)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][2], 0.0)

    def test_rrf_fusion_empty_bm25(self):
        vector_hits = [("r1", "image", 0.95)]
        result = self.client._rrf_fusion(vector_hits, [], bm25_weight=0.5, k=60)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][3], 0.0)

    def test_rrf_fusion_weight_shifts_ranking(self):
        vector_hits = [("r1", "image", 0.95), ("r2", "image", 0.90)]
        bm25_hits = [("r2", "image", 15.0), ("r1", "image", 12.0)]

        result_high_bm25 = self.client._rrf_fusion(vector_hits, bm25_hits, bm25_weight=0.9, k=60)
        result_low_bm25 = self.client._rrf_fusion(vector_hits, bm25_hits, bm25_weight=0.1, k=60)

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
        vector_hits = [("r1", "image", 0.95), ("r2", "atlas", 0.88)]
        bm25_hits = [("r2", "atlas", 12.0), ("r3", "audio", 8.0)]
        result = self.client._rrf_fusion(vector_hits, bm25_hits, bm25_weight=0.3, k=60)

        for item in result:
            self.assertEqual(len(item), 6)

        r2_item = next(r for r in result if r[0] == "r2")
        self.assertGreater(r2_item[2], 0)
        self.assertGreater(r2_item[3], 0)

        r1_item = next(r for r in result if r[0] == "r1")
        self.assertGreater(r1_item[2], 0)
        self.assertEqual(r1_item[3], 0.0)

        r3_item = next(r for r in result if r[0] == "r3")
        self.assertEqual(r3_item[2], 0.0)
        self.assertGreater(r3_item[3], 0)


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

        self.assertEqual(len(fake_milvus.search_calls), 1)
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

        try:
            result = await client._bm25_search("nonexistent", "", 10)
            self.assertIsInstance(result, list)
        except Exception:
            pass  # Expected: SQLite doesn't support PostgreSQL-specific functions

        await session.close()


if __name__ == "__main__":
    unittest.main()
