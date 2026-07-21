"""Test updated search data contracts with search_mode, bm25_weight, scores."""
import sys
import unittest
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parents[2]
_ROOT = _SERVER_DIR.parent
for _p in (str(_ROOT), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

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
        self.assertEqual(item.package_download_url, "")

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
        self.assertIn("package_download_url", d)
        self.assertIn("file_structure", d)


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
            file_download_url="",
            description_summary="",
            file_format="png",
            file_size=100,
            status="ready",
            preview_available=True,
        )
        self.assertEqual(result.vector_score, 0.0)
        self.assertEqual(result.bm25_score, 0.0)
        self.assertEqual(result.rrf_score, 0.0)
        self.assertEqual(result.package_download_url, "")


class TestConfigBM25Settings(unittest.TestCase):

    def test_bm25_default_weight(self):
        from app.config import Settings
        s = Settings()
        self.assertAlmostEqual(s.bm25_default_weight, 0.5)

    def test_search_text_config(self):
        from app.config import Settings
        s = Settings()
        self.assertEqual(s.search_text_config, "jiebacfg")


if __name__ == "__main__":
    unittest.main()
