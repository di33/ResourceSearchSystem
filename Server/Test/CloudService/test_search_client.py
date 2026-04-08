import asyncio
import unittest

from CloudService.search_client import (
    AgentDownloadToolInput,
    AgentDownloadToolOutput,
    AgentSearchToolInput,
    AgentSearchToolOutput,
    DownloadLinkRequest,
    DownloadLinkResponse,
    MockSearchClient,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    SearchSuggestion,
)


def _run_async(coro):
    """Run a coroutine without destroying the default event loop for later tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _make_result(resource_id="r1", resource_type="texture", score=0.9, **kw):
    defaults = dict(
        primary_preview_url="https://preview.example.com/r1",
        other_preview_urls=[],
        file_download_url="https://download.example.com/r1",
        description_summary="A test resource",
        file_format="png",
        file_size=2048,
        status="ready",
        preview_available=True,
    )
    defaults.update(kw)
    return SearchResultItem(
        resource_id=resource_id,
        resource_type=resource_type,
        score=score,
        **defaults,
    )


# ---- 1–3: data-class basics ------------------------------------------------

class TestSearchRequestDefaults(unittest.TestCase):

    def test_search_request_defaults(self):
        req = SearchRequest(query_text="cat")
        self.assertEqual(req.query_text, "cat")
        self.assertIsNone(req.resource_type)
        self.assertIsNone(req.format_filter)
        self.assertEqual(req.top_k, 10)
        self.assertAlmostEqual(req.similarity_threshold, 0.5)


class TestSearchResultItemToDict(unittest.TestCase):

    def test_search_result_item_to_dict(self):
        item = _make_result()
        d = item.to_dict()
        self.assertIsInstance(d, dict)
        self.assertEqual(d["resource_id"], "r1")
        self.assertEqual(d["score"], 0.9)
        self.assertEqual(d["file_format"], "png")
        self.assertIn("preview_available", d)


class TestSearchResponseToDict(unittest.TestCase):

    def test_search_response_to_dict(self):
        resp = SearchResponse(
            results=[_make_result()],
            total_count=1,
        )
        d = resp.to_dict()
        self.assertIn("results", d)
        self.assertIn("total_count", d)
        self.assertEqual(d["total_count"], 1)
        self.assertEqual(len(d["results"]), 1)


# ---- 4: suggestion on empty results ----------------------------------------

class TestSearchSuggestionEmptyResults(unittest.TestCase):

    def test_search_suggestion_empty_results(self):
        client = MockSearchClient()
        req = SearchRequest(query_text="nonexistent", similarity_threshold=0.99)
        resp = _run_async(client.search(req))
        self.assertEqual(resp.total_count, 0)
        self.assertIsNotNone(resp.suggestion)
        self.assertGreater(len(resp.suggestion.rewrite_queries), 0)
        self.assertGreater(len(resp.suggestion.relaxable_filters), 0)
        self.assertIsNotNone(resp.suggestion.suggested_threshold)
        self.assertLess(resp.suggestion.suggested_threshold, 0.99)
        self.assertTrue(resp.suggestion.try_cross_type)


# ---- 5–7: download link data classes ----------------------------------------

class TestDownloadLinkRequestDefaults(unittest.TestCase):

    def test_download_link_request_defaults(self):
        req = DownloadLinkRequest(resource_id="r1")
        self.assertEqual(req.resource_id, "r1")
        self.assertEqual(req.expire_seconds, 3600)
        self.assertFalse(req.return_base64)


class TestDownloadLinkResponseSuccess(unittest.TestCase):

    def test_download_link_response_success(self):
        resp = DownloadLinkResponse(
            download_url="https://example.com/dl",
            expires_at="2026-12-31T23:59:59Z",
            file_name="file.png",
            file_size=100,
            content_type="image/png",
        )
        self.assertTrue(resp.success)


class TestDownloadLinkResponseError(unittest.TestCase):

    def test_download_link_response_error(self):
        resp = DownloadLinkResponse(
            download_url="",
            expires_at="",
            file_name="",
            file_size=0,
            content_type="",
            error_code="RESOURCE_NOT_FOUND",
            error_message="Not found",
        )
        self.assertFalse(resp.success)


# ---- 8–12: mock search client behaviour ------------------------------------

class TestMockSearchReturnsResults(unittest.TestCase):

    def test_mock_search_returns_results(self):
        client = MockSearchClient()
        client.set_mock_results([_make_result()])
        req = SearchRequest(query_text="test")
        resp = _run_async(client.search(req))
        self.assertEqual(resp.total_count, 1)
        self.assertEqual(resp.results[0].resource_id, "r1")


class TestMockSearchFiltersByType(unittest.TestCase):

    def test_mock_search_filters_by_type(self):
        client = MockSearchClient()
        client.set_mock_results([
            _make_result(resource_id="r1", resource_type="texture"),
            _make_result(resource_id="r2", resource_type="model"),
        ])
        req = SearchRequest(query_text="test", resource_type="model")
        resp = _run_async(client.search(req))
        self.assertEqual(resp.total_count, 1)
        self.assertEqual(resp.results[0].resource_id, "r2")


class TestMockSearchFiltersByThreshold(unittest.TestCase):

    def test_mock_search_filters_by_threshold(self):
        client = MockSearchClient()
        client.set_mock_results([
            _make_result(resource_id="r1", score=0.8),
            _make_result(resource_id="r2", score=0.3),
        ])
        req = SearchRequest(query_text="test", similarity_threshold=0.5)
        resp = _run_async(client.search(req))
        self.assertEqual(resp.total_count, 1)
        self.assertEqual(resp.results[0].resource_id, "r1")


class TestMockSearchRespectsTopK(unittest.TestCase):

    def test_mock_search_respects_top_k(self):
        client = MockSearchClient()
        client.set_mock_results([
            _make_result(resource_id=f"r{i}", score=0.9 - i * 0.01)
            for i in range(10)
        ])
        req = SearchRequest(query_text="test", top_k=3)
        resp = _run_async(client.search(req))
        self.assertEqual(len(resp.results), 3)
        self.assertEqual(resp.total_count, 3)


class TestMockSearchEmptySuggestion(unittest.TestCase):

    def test_mock_search_empty_suggestion(self):
        client = MockSearchClient()
        req = SearchRequest(query_text="nothing", similarity_threshold=0.99)
        resp = _run_async(client.search(req))
        self.assertEqual(resp.total_count, 0)
        self.assertIsNotNone(resp.suggestion)
        self.assertIn("nothing 高清", resp.suggestion.rewrite_queries)
        self.assertIn("nothing 素材", resp.suggestion.rewrite_queries)


# ---- 13–14: mock download link ----------------------------------------------

class TestMockDownloadLink(unittest.TestCase):

    def test_mock_download_link(self):
        client = MockSearchClient()
        req = DownloadLinkRequest(resource_id="r1")
        resp = _run_async(client.get_download_link(req))
        self.assertTrue(resp.success)
        self.assertIn("r1", resp.download_url)
        self.assertEqual(resp.file_size, 12345)
        self.assertIsNone(resp.base64_content)


class TestMockDownloadWithBase64(unittest.TestCase):

    def test_mock_download_with_base64(self):
        client = MockSearchClient()
        req = DownloadLinkRequest(resource_id="r1", return_base64=True)
        resp = _run_async(client.get_download_link(req))
        self.assertTrue(resp.success)
        self.assertIsNotNone(resp.base64_content)


# ---- 15–16: agent tool contracts --------------------------------------------

class TestAgentSearchToolInputDefaults(unittest.TestCase):

    def test_agent_search_tool_input_defaults(self):
        inp = AgentSearchToolInput(query_text="test")
        self.assertEqual(inp.query_text, "test")
        self.assertIsNone(inp.resource_type)
        self.assertIsNone(inp.format_filter)
        self.assertEqual(inp.top_k, 5)
        self.assertAlmostEqual(inp.similarity_threshold, 0.6)


class TestAgentDownloadToolInputDefaults(unittest.TestCase):

    def test_agent_download_tool_input_defaults(self):
        inp = AgentDownloadToolInput(resource_id="r1")
        self.assertEqual(inp.resource_id, "r1")
        self.assertEqual(inp.expire_seconds, 3600)
        self.assertFalse(inp.return_base64)


if __name__ == "__main__":
    unittest.main()
