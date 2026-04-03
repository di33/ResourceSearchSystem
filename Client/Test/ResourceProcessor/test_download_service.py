import asyncio
import unittest

from ResourceProcessor.search_client import (
    AgentDownloadToolInput,
    BaseSearchClient,
    DownloadLinkRequest,
    DownloadLinkResponse,
    MockSearchClient,
    SearchRequest,
    SearchResponse,
)
from ResourceProcessor.download_service import (
    AgentDownloadToolAdapter,
    DownloadConfig,
    DownloadErrorCode,
    DownloadService,
)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


class ErrorSearchClient(BaseSearchClient):
    async def search(self, request: SearchRequest) -> SearchResponse:
        raise RuntimeError("not implemented")

    async def get_download_link(self, request: DownloadLinkRequest) -> DownloadLinkResponse:
        raise RuntimeError("connection failed")


# ---- 1: error code constants ------------------------------------------------

class TestDownloadErrorCodeValues(unittest.TestCase):

    def test_download_error_code_values(self):
        self.assertEqual(DownloadErrorCode.RESOURCE_NOT_FOUND, "RESOURCE_NOT_FOUND")
        self.assertEqual(DownloadErrorCode.RESOURCE_NOT_AVAILABLE, "RESOURCE_NOT_AVAILABLE")
        self.assertEqual(DownloadErrorCode.PERMISSION_DENIED, "PERMISSION_DENIED")
        self.assertEqual(DownloadErrorCode.LINK_GENERATE_FAILED, "LINK_GENERATE_FAILED")
        self.assertEqual(DownloadErrorCode.INVALID_REQUEST, "INVALID_REQUEST")


# ---- 2: config defaults -----------------------------------------------------

class TestDownloadConfigDefaults(unittest.TestCase):

    def test_download_config_defaults(self):
        cfg = DownloadConfig()
        self.assertEqual(cfg.max_expire_seconds, 86400)
        self.assertEqual(cfg.default_expire_seconds, 3600)
        self.assertEqual(cfg.base64_size_threshold, 5 * 1024 * 1024)


# ---- 3–6: validate_request --------------------------------------------------

class TestValidateEmptyResourceId(unittest.TestCase):

    def test_validate_empty_resource_id(self):
        svc = DownloadService(MockSearchClient())
        result = svc.validate_request("", 3600)
        self.assertIsNotNone(result)
        self.assertEqual(result.error_code, DownloadErrorCode.INVALID_REQUEST)
        self.assertIn("resource_id", result.error_message)


class TestValidateNegativeExpire(unittest.TestCase):

    def test_validate_negative_expire(self):
        svc = DownloadService(MockSearchClient())
        result = svc.validate_request("r1", -1)
        self.assertIsNotNone(result)
        self.assertEqual(result.error_code, DownloadErrorCode.INVALID_REQUEST)
        self.assertIn("expire_seconds", result.error_message)


class TestValidateExceedsMaxExpire(unittest.TestCase):

    def test_validate_exceeds_max_expire(self):
        cfg = DownloadConfig(max_expire_seconds=3600)
        svc = DownloadService(MockSearchClient(), config=cfg)
        result = svc.validate_request("r1", 7200)
        self.assertIsNotNone(result)
        self.assertEqual(result.error_code, DownloadErrorCode.INVALID_REQUEST)
        self.assertIn("expire_seconds", result.error_message)


class TestValidateOk(unittest.TestCase):

    def test_validate_ok(self):
        svc = DownloadService(MockSearchClient())
        result = svc.validate_request("r1", 3600)
        self.assertIsNone(result)


# ---- 7–10: get_download_link ------------------------------------------------

class TestGetDownloadLinkSuccess(unittest.TestCase):

    def test_get_download_link_success(self):
        client = MockSearchClient()
        svc = DownloadService(client)
        resp = _run_async(svc.get_download_link("r1", expire_seconds=600))
        self.assertTrue(resp.success)
        self.assertIn("r1", resp.download_url)
        self.assertEqual(resp.file_size, 12345)
        self.assertIsNone(resp.base64_content)
        self.assertEqual(len(client.download_calls), 1)
        self.assertEqual(client.download_calls[0].expire_seconds, 600)


class TestGetDownloadLinkWithBase64(unittest.TestCase):

    def test_get_download_link_with_base64(self):
        client = MockSearchClient()
        svc = DownloadService(client)
        resp = _run_async(svc.get_download_link("r1", return_base64=True))
        self.assertTrue(resp.success)
        self.assertIsNotNone(resp.base64_content)


class TestGetDownloadLinkUsesDefaultExpire(unittest.TestCase):

    def test_get_download_link_uses_default_expire(self):
        client = MockSearchClient()
        cfg = DownloadConfig(default_expire_seconds=1800)
        svc = DownloadService(client, config=cfg)
        _run_async(svc.get_download_link("r1"))
        self.assertEqual(len(client.download_calls), 1)
        self.assertEqual(client.download_calls[0].expire_seconds, 1800)


class TestGetDownloadLinkHandlesException(unittest.TestCase):

    def test_get_download_link_handles_exception(self):
        svc = DownloadService(ErrorSearchClient())
        resp = _run_async(svc.get_download_link("r1"))
        self.assertFalse(resp.success)
        self.assertEqual(resp.error_code, DownloadErrorCode.LINK_GENERATE_FAILED)
        self.assertIn("connection failed", resp.error_message)


# ---- 11–13: should_return_base64 --------------------------------------------

class TestShouldReturnBase64SmallFile(unittest.TestCase):

    def test_should_return_base64_small_file(self):
        svc = DownloadService(MockSearchClient())
        self.assertTrue(svc.should_return_base64(1024, explicit_request=True))


class TestShouldReturnBase64LargeFile(unittest.TestCase):

    def test_should_return_base64_large_file(self):
        svc = DownloadService(MockSearchClient())
        self.assertFalse(svc.should_return_base64(10 * 1024 * 1024, explicit_request=True))


class TestShouldReturnBase64NotRequested(unittest.TestCase):

    def test_should_return_base64_not_requested(self):
        svc = DownloadService(MockSearchClient())
        self.assertFalse(svc.should_return_base64(1024, explicit_request=False))


# ---- 14–15: agent adapter ----------------------------------------------------

class TestAgentAdapterSuccess(unittest.TestCase):

    def test_agent_adapter_success(self):
        client = MockSearchClient()
        svc = DownloadService(client)
        adapter = AgentDownloadToolAdapter(svc)
        tool_input = AgentDownloadToolInput(resource_id="r1", expire_seconds=600)
        output = _run_async(adapter.execute(tool_input))
        self.assertIn("r1", output.download_url)
        self.assertEqual(output.file_name, "r1.png")
        self.assertEqual(output.file_size, 12345)
        self.assertEqual(output.error_code, "")


class TestAgentAdapterError(unittest.TestCase):

    def test_agent_adapter_error(self):
        svc = DownloadService(ErrorSearchClient())
        adapter = AgentDownloadToolAdapter(svc)
        tool_input = AgentDownloadToolInput(resource_id="r1")
        output = _run_async(adapter.execute(tool_input))
        self.assertEqual(output.error_code, DownloadErrorCode.LINK_GENERATE_FAILED)
        self.assertIn("connection failed", output.error_message)
        self.assertEqual(output.download_url, "")


if __name__ == "__main__":
    unittest.main()
