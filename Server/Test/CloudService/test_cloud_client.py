import asyncio
import unittest

from CloudService.cloud_client import (
    BaseCloudClient,
    CommitRequest,
    CommitResponse,
    FileInfo,
    MockCloudClient,
    MULTIPART_THRESHOLD,
    PreviewFileInfo,
    RegisterRequest,
    RegisterResponse,
    UploadResult,
)


def _run_async(coro):
    """Run a coroutine without destroying the default event loop for later tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _make_file_info(name="tex.png", size=1024, fmt="png", role="main", primary=True):
    return FileInfo(
        file_path=f"/tmp/{name}",
        file_name=name,
        file_size=size,
        file_format=fmt,
        content_md5="abc123",
        file_role=role,
        is_primary=primary,
    )


class TestRegisterRequestIdempotencyKey(unittest.TestCase):

    def test_register_request_auto_idempotency_key(self):
        req = RegisterRequest(
            content_md5="abc123",
            resource_type="texture",
            files=[_make_file_info()],
        )
        self.assertTrue(req.idempotency_key.startswith("register-"))
        self.assertGreater(len(req.idempotency_key), len("register-"))

    def test_register_request_custom_idempotency_key(self):
        req = RegisterRequest(
            content_md5="abc123",
            resource_type="texture",
            files=[_make_file_info()],
            idempotency_key="my-custom-key",
        )
        self.assertEqual(req.idempotency_key, "my-custom-key")


class TestCommitRequestIdempotencyKey(unittest.TestCase):

    def test_commit_request_auto_idempotency_key(self):
        req = CommitRequest(
            resource_id="res-001",
            resource_type="model",
            description_main="A cube",
            description_detail="Simple cube mesh",
            description_full="A simple cube mesh for testing",
            embedding_dimension=3,
            embedding_vector_data=[0.1, 0.2, 0.3],
        )
        self.assertTrue(req.idempotency_key.startswith("commit-"))
        self.assertGreater(len(req.idempotency_key), len("commit-"))


class TestDetermineUploadMode(unittest.TestCase):

    def setUp(self):
        self.client = MockCloudClient()

    def test_determine_upload_mode_direct(self):
        self.assertEqual(self.client.determine_upload_mode(50 * 1024 * 1024), "direct")

    def test_determine_upload_mode_multipart(self):
        self.assertEqual(
            self.client.determine_upload_mode(200 * 1024 * 1024), "multipart"
        )


class TestMockCloudClient(unittest.TestCase):

    def setUp(self):
        self.client = MockCloudClient()

    def _make_register_request(self, file_size: int = 1024) -> RegisterRequest:
        return RegisterRequest(
            content_md5="d41d8cd98f00b204e9800998ecf8427e",
            resource_type="model",
            files=[_make_file_info("cube.fbx", file_size, "fbx", "model", True)],
        )

    def _make_commit_request(self, resource_id: str) -> CommitRequest:
        return CommitRequest(
            resource_id=resource_id,
            resource_type="model",
            description_main="A cube",
            description_detail="Simple cube mesh",
            description_full="A simple cube mesh for testing",
            embedding_dimension=3,
            embedding_vector_data=[0.1, 0.2, 0.3],
        )

    def test_mock_register_returns_resource_id(self):
        async def run():
            resp = await self.client.register(self._make_register_request())
            self.assertTrue(resp.resource_id.startswith("res-"))
            self.assertEqual(resp.state, "registered")
        _run_async(run())

    def test_mock_register_records_call(self):
        async def run():
            req = self._make_register_request()
            await self.client.register(req)
            self.assertEqual(len(self.client.register_calls), 1)
            self.assertIs(self.client.register_calls[0], req)
        _run_async(run())

    def test_mock_upload_files_success(self):
        async def run():
            files = [_make_file_info("cube.fbx", 2048, "fbx", "model", True)]
            result = await self.client.upload_files("res-001", files)
            self.assertTrue(result.success)
            self.assertEqual(result.uploaded_bytes, 2048)
        _run_async(run())

    def test_mock_upload_previews_success(self):
        async def run():
            previews = [PreviewFileInfo(
                file_path="/tmp/preview.png",
                file_name="preview.png",
                content_type="image/png",
                role="primary",
            )]
            result = await self.client.upload_previews("res-001", previews)
            self.assertTrue(result.success)
            self.assertEqual(result.uploaded_bytes, 1024)
        _run_async(run())

    def test_mock_commit_returns_committed(self):
        async def run():
            req = self._make_commit_request("res-001")
            resp = await self.client.commit(req)
            self.assertEqual(resp.state, "committed")
            self.assertEqual(resp.resource_id, "res-001")
        _run_async(run())

    def test_mock_full_workflow(self):
        async def run():
            reg_req = self._make_register_request(file_size=512)
            reg_resp = await self.client.register(reg_req)
            self.assertEqual(reg_resp.state, "registered")
            self.assertEqual(reg_resp.upload_mode, "direct")

            rid = reg_resp.resource_id
            files = [_make_file_info("cube.fbx", 512, "fbx", "model", True)]
            upload_result = await self.client.upload_files(rid, files)
            self.assertTrue(upload_result.success)

            previews = [PreviewFileInfo(
                file_path="/tmp/preview.png",
                file_name="preview.png",
                content_type="image/png",
                role="primary",
            )]
            preview_result = await self.client.upload_previews(rid, previews)
            self.assertTrue(preview_result.success)

            commit_req = self._make_commit_request(rid)
            commit_resp = await self.client.commit(commit_req)
            self.assertEqual(commit_resp.state, "committed")
            self.assertEqual(commit_resp.resource_id, rid)

            self.assertEqual(len(self.client.register_calls), 1)
            self.assertEqual(len(self.client.upload_files_calls), 1)
            self.assertEqual(len(self.client.upload_previews_calls), 1)
            self.assertEqual(len(self.client.commit_calls), 1)
        _run_async(run())


class TestDataclassDefaults(unittest.TestCase):

    def test_register_response_fields(self):
        resp = RegisterResponse(
            resource_id="res-abc",
            exists=True,
            upload_mode="multipart",
            multipart_chunk_size=10485760,
            state="registered",
        )
        self.assertEqual(resp.resource_id, "res-abc")
        self.assertTrue(resp.exists)
        self.assertEqual(resp.upload_mode, "multipart")
        self.assertEqual(resp.multipart_chunk_size, 10485760)
        self.assertEqual(resp.state, "registered")

    def test_upload_result_default_values(self):
        result = UploadResult(success=False)
        self.assertFalse(result.success)
        self.assertEqual(result.uploaded_bytes, 0)
        self.assertEqual(result.error_message, "")

    def test_multipart_threshold_value(self):
        self.assertEqual(MULTIPART_THRESHOLD, 104857600)


# ---------------------------------------------------------------------------
# Multi-file / multi-preview tests
# ---------------------------------------------------------------------------


class TestRegisterRequestMultiFile(unittest.TestCase):

    def test_total_size_aggregates_all_files(self):
        files = [
            _make_file_info("a.fbx", 1000, "fbx", "model", True),
            _make_file_info("b.png", 500, "png", "texture", False),
            _make_file_info("c.png", 300, "png", "texture", False),
        ]
        req = RegisterRequest(content_md5="composite", resource_type="3d_model", files=files)
        self.assertEqual(req.total_size, 1800)

    def test_primary_file_returns_marked_primary(self):
        files = [
            _make_file_info("tex.png", 500, "png", "texture", False),
            _make_file_info("model.fbx", 1000, "fbx", "model", True),
        ]
        req = RegisterRequest(content_md5="md5", resource_type="3d_model", files=files)
        self.assertEqual(req.primary_file.file_name, "model.fbx")

    def test_primary_file_fallback_to_first(self):
        files = [
            _make_file_info("a.png", 100, "png", "main", False),
            _make_file_info("b.png", 200, "png", "main", False),
        ]
        req = RegisterRequest(content_md5="md5", resource_type="image", files=files)
        self.assertEqual(req.primary_file.file_name, "a.png")

    def test_empty_files_total_size_zero(self):
        req = RegisterRequest(content_md5="md5", resource_type="other", files=[])
        self.assertEqual(req.total_size, 0)
        self.assertIsNone(req.primary_file)


class TestMockCloudClientMultiFile(unittest.TestCase):

    def setUp(self):
        self.client = MockCloudClient()

    def test_upload_files_sums_bytes(self):
        async def run():
            files = [
                _make_file_info("a.fbx", 1000, "fbx", "model", True),
                _make_file_info("b.png", 500, "png", "texture", False),
            ]
            result = await self.client.upload_files("res-001", files)
            self.assertTrue(result.success)
            self.assertEqual(result.uploaded_bytes, 1500)
        _run_async(run())

    def test_upload_previews_counts_previews(self):
        async def run():
            previews = [
                PreviewFileInfo("/p/a.gif", "a.gif", "image/gif", "primary"),
                PreviewFileInfo("/p/b.webp", "b.webp", "image/webp", "gallery"),
            ]
            result = await self.client.upload_previews("res-001", previews)
            self.assertTrue(result.success)
            self.assertEqual(result.uploaded_bytes, 2 * 1024)
        _run_async(run())

    def test_register_multipart_for_large_multi_file(self):
        async def run():
            files = [
                _make_file_info("big.fbx", 60 * 1024 * 1024, "fbx", "model", True),
                _make_file_info("big_tex.png", 50 * 1024 * 1024, "png", "texture", False),
            ]
            req = RegisterRequest(content_md5="big", resource_type="model", files=files)
            resp = await self.client.register(req)
            self.assertEqual(resp.upload_mode, "multipart")
        _run_async(run())


if __name__ == "__main__":
    unittest.main()
