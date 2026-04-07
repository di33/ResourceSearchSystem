import asyncio
import os
import unittest

from CloudService.cloud_client import (
    BaseCloudClient,
    CommitRequest,
    CommitResponse,
    FileInfo,
    MockCloudClient,
    RegisterRequest,
    RegisterResponse,
    UploadResult,
)
from ResourceProcessor.cache.local_cache import LocalCacheStore
from ResourceProcessor.preview_metadata import (
    FileInfo as ClientFileInfo,
    PreviewInfo,
    PreviewStrategy,
    ProcessState,
    ResourceProcessingEntity,
)
from CloudService.upload_orchestrator import UploadOrchestrator, UploadOutcome, UploadTask


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _make_file_info():
    return FileInfo(
        file_path="/tmp/cube.fbx",
        file_name="cube.fbx",
        file_size=2048,
        file_format="fbx",
        content_md5="d41d8cd98f00b204e9800998ecf8427e",
        file_role="model",
        is_primary=True,
    )


def _make_entity() -> ResourceProcessingEntity:
    return ResourceProcessingEntity(
        content_md5="d41d8cd98f00b204e9800998ecf8427e",
        resource_type="model",
        source_directory="/tmp",
        files=[ClientFileInfo(
            file_path="/tmp/cube.fbx",
            file_name="cube.fbx",
            file_size=2048,
            file_format="fbx",
            content_md5="d41d8cd98f00b204e9800998ecf8427e",
            file_role="model",
            is_primary=True,
        )],
        process_state=ProcessState.PACKAGE_READY,
    )


def _make_preview_info():
    return PreviewInfo(
        strategy=PreviewStrategy.GIF,
        role="primary",
        path="/tmp/preview.png",
        format="png",
        width=256,
        height=256,
        size=5000,
        renderer="blender",
    )


def _make_task(task_id: int) -> UploadTask:
    return UploadTask(
        task_id=task_id,
        content_md5="d41d8cd98f00b204e9800998ecf8427e",
        resource_type="model",
        files=[_make_file_info()],
        previews=[_make_preview_info()],
        description_main="A cube",
        description_detail="Simple cube mesh",
        description_full="A simple cube mesh for testing",
        embedding_dimension=3,
        embedding_vector_data=[0.1, 0.2, 0.3],
    )


# ------------------------------------------------------------------
# Custom failure clients
# ------------------------------------------------------------------

class FailUploadClient(MockCloudClient):
    """upload_files returns success=False."""

    async def upload_files(self, resource_id, files):
        self.upload_files_calls.append((resource_id, files))
        return UploadResult(success=False, error_message="storage unavailable")


class FailCommitClient(MockCloudClient):
    """commit returns state='failed'."""

    async def commit(self, request: CommitRequest) -> CommitResponse:
        self.commit_calls.append(request)
        return CommitResponse(
            resource_id=request.resource_id,
            state="failed",
            error_message="validation error",
        )


class ExistsClient(MockCloudClient):
    """register returns exists=True."""

    async def register(self, request: RegisterRequest) -> RegisterResponse:
        self.register_calls.append(request)
        return RegisterResponse(
            resource_id="res-existing",
            exists=True,
            upload_mode="direct",
            multipart_chunk_size=0,
            state="registered",
        )


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

class TestUploadTaskFields(unittest.TestCase):

    def test_upload_task_fields(self):
        task = _make_task(42)
        self.assertEqual(task.task_id, 42)
        self.assertEqual(task.content_md5, "d41d8cd98f00b204e9800998ecf8427e")
        self.assertEqual(task.resource_type, "model")
        self.assertEqual(len(task.files), 1)
        self.assertEqual(task.files[0].file_name, "cube.fbx")
        self.assertEqual(task.files[0].file_size, 2048)
        self.assertEqual(task.total_size, 2048)
        self.assertEqual(len(task.previews), 1)
        self.assertEqual(task.previews[0].path, "/tmp/preview.png")
        self.assertEqual(task.description_main, "A cube")
        self.assertEqual(task.description_detail, "Simple cube mesh")
        self.assertEqual(task.description_full, "A simple cube mesh for testing")
        self.assertEqual(task.embedding_dimension, 3)
        self.assertEqual(task.embedding_vector_data, [0.1, 0.2, 0.3])


class TestUploadOutcomeDefaults(unittest.TestCase):

    def test_upload_outcome_default_values(self):
        outcome = UploadOutcome(task_id=1, success=False)
        self.assertEqual(outcome.task_id, 1)
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.resource_id, "")
        self.assertEqual(outcome.final_state, "")
        self.assertEqual(outcome.error_message, "")


class TestUploadOrchestratorSuccess(unittest.TestCase):

    def setUp(self):
        self.db_path = os.path.join(os.environ.get("TEMP", "/tmp"), "test_orch.db")
        self.cache = LocalCacheStore(self.db_path)
        self.client = MockCloudClient()
        self.orchestrator = UploadOrchestrator(self.client, self.cache)
        self.task_id = self.cache.insert_task(_make_entity())
        self.task = _make_task(self.task_id)

    def tearDown(self):
        self.cache.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_execute_full_success(self):
        async def run():
            result = await self.orchestrator.execute(self.task)
            self.assertTrue(result.success)
            self.assertEqual(result.final_state, "committed")
            self.assertTrue(result.resource_id.startswith("res-"))
            task_row = self.cache.get_task_by_id(self.task_id)
            self.assertEqual(task_row["process_state"], ProcessState.COMMITTED.value)
        _run_async(run())

    def test_execute_registers_resource(self):
        async def run():
            await self.orchestrator.execute(self.task)
            self.assertEqual(len(self.client.register_calls), 1)
            req = self.client.register_calls[0]
            self.assertEqual(req.content_md5, self.task.content_md5)
            self.assertEqual(req.resource_type, self.task.resource_type)
            self.assertEqual(len(req.files), 1)
            self.assertEqual(req.files[0].file_name, "cube.fbx")
            self.assertEqual(req.total_size, 2048)
        _run_async(run())

    def test_execute_uploads_files(self):
        async def run():
            await self.orchestrator.execute(self.task)
            self.assertEqual(len(self.client.upload_files_calls), 1)
            self.assertEqual(len(self.client.upload_previews_calls), 1)
            rid, files = self.client.upload_files_calls[0]
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].file_name, "cube.fbx")
            rid2, previews = self.client.upload_previews_calls[0]
            self.assertEqual(len(previews), 1)
            self.assertEqual(rid, rid2)
        _run_async(run())

    def test_execute_commits_with_metadata(self):
        async def run():
            await self.orchestrator.execute(self.task)
            self.assertEqual(len(self.client.commit_calls), 1)
            req = self.client.commit_calls[0]
            self.assertEqual(req.resource_type, self.task.resource_type)
            self.assertEqual(req.description_main, self.task.description_main)
            self.assertEqual(req.description_detail, self.task.description_detail)
            self.assertEqual(req.description_full, self.task.description_full)
            self.assertEqual(req.embedding_dimension, self.task.embedding_dimension)
            self.assertEqual(req.embedding_vector_data, self.task.embedding_vector_data)
        _run_async(run())

    def test_execute_updates_cache_states(self):
        """Verify that the cache state walks through REGISTERED → UPLOADED → COMMITTED."""
        observed_states = []
        original_update = self.cache.update_task_state

        def tracking_update(task_id, state, error_code="", error_message=""):
            observed_states.append(state)
            return original_update(task_id, state, error_code, error_message)

        self.cache.update_task_state = tracking_update

        async def run():
            await self.orchestrator.execute(self.task)
            self.assertEqual(
                observed_states,
                [ProcessState.REGISTERED, ProcessState.UPLOADED, ProcessState.COMMITTED],
            )
        _run_async(run())

    def test_execute_logs_events(self):
        async def run():
            await self.orchestrator.execute(self.task)
            logs = self.cache.get_logs(self.task_id)
            events = [lg["event"] for lg in logs]
            self.assertIn("registered", events)
            self.assertIn("uploaded", events)
            self.assertIn("committed", events)
        _run_async(run())


class TestUploadOrchestratorUploadFailure(unittest.TestCase):

    def setUp(self):
        self.db_path = os.path.join(os.environ.get("TEMP", "/tmp"), "test_orch_fail_upload.db")
        self.cache = LocalCacheStore(self.db_path)
        self.client = FailUploadClient()
        self.orchestrator = UploadOrchestrator(self.client, self.cache)
        self.task_id = self.cache.insert_task(_make_entity())
        self.task = _make_task(self.task_id)

    def tearDown(self):
        self.cache.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_execute_handles_upload_failure(self):
        async def run():
            result = await self.orchestrator.execute(self.task)
            self.assertFalse(result.success)
            self.assertIn("文件上传失败", result.error_message)
            task_row = self.cache.get_task_by_id(self.task_id)
            self.assertGreater(task_row["retry_count"], 0)
            logs = self.cache.get_logs(self.task_id)
            error_logs = [lg for lg in logs if lg["event"] == "error"]
            self.assertGreater(len(error_logs), 0)
        _run_async(run())


class TestUploadOrchestratorCommitFailure(unittest.TestCase):

    def setUp(self):
        self.db_path = os.path.join(os.environ.get("TEMP", "/tmp"), "test_orch_fail_commit.db")
        self.cache = LocalCacheStore(self.db_path)
        self.client = FailCommitClient()
        self.orchestrator = UploadOrchestrator(self.client, self.cache)
        self.task_id = self.cache.insert_task(_make_entity())
        self.task = _make_task(self.task_id)

    def tearDown(self):
        self.cache.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_execute_handles_commit_failure(self):
        async def run():
            result = await self.orchestrator.execute(self.task)
            self.assertFalse(result.success)
            self.assertIn("提交失败", result.error_message)
            task_row = self.cache.get_task_by_id(self.task_id)
            self.assertGreater(task_row["retry_count"], 0)
            logs = self.cache.get_logs(self.task_id)
            error_logs = [lg for lg in logs if lg["event"] == "error"]
            self.assertGreater(len(error_logs), 0)
        _run_async(run())


class TestUploadOrchestratorExistsSkip(unittest.TestCase):

    def setUp(self):
        self.db_path = os.path.join(os.environ.get("TEMP", "/tmp"), "test_orch_exists.db")
        self.cache = LocalCacheStore(self.db_path)
        self.client = ExistsClient()
        self.orchestrator = UploadOrchestrator(self.client, self.cache)
        self.task_id = self.cache.insert_task(_make_entity())
        self.task = _make_task(self.task_id)

    def tearDown(self):
        self.cache.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_execute_skips_existing_resource(self):
        async def run():
            result = await self.orchestrator.execute(self.task)
            self.assertTrue(result.success)
            self.assertEqual(result.final_state, "synced")
            self.assertEqual(result.resource_id, "res-existing")
            self.assertEqual(len(self.client.upload_files_calls), 0)
            self.assertEqual(len(self.client.upload_previews_calls), 0)
            self.assertEqual(len(self.client.commit_calls), 0)
            task_row = self.cache.get_task_by_id(self.task_id)
            self.assertEqual(task_row["process_state"], ProcessState.SYNCED.value)
        _run_async(run())


class TestUploadOrchestratorException(unittest.TestCase):

    def setUp(self):
        self.db_path = os.path.join(os.environ.get("TEMP", "/tmp"), "test_orch_exc.db")
        self.cache = LocalCacheStore(self.db_path)
        self.client = MockCloudClient()
        self.orchestrator = UploadOrchestrator(self.client, self.cache)
        self.task_id = self.cache.insert_task(_make_entity())
        self.task = _make_task(self.task_id)

    def tearDown(self):
        self.cache.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_execute_handles_exception(self):
        async def boom(*_args, **_kwargs):
            raise RuntimeError("network down")

        self.client.register = boom

        async def run():
            result = await self.orchestrator.execute(self.task)
            self.assertFalse(result.success)
            self.assertIn("network down", result.error_message)
            task_row = self.cache.get_task_by_id(self.task_id)
            self.assertGreater(task_row["retry_count"], 0)
        _run_async(run())


# ---------------------------------------------------------------------------
# Multi-file / multi-preview tests
# ---------------------------------------------------------------------------


class TestUploadTaskMultiFile(unittest.TestCase):

    def test_total_size_sums_all_files(self):
        task = UploadTask(
            task_id=1,
            content_md5="md5",
            resource_type="3d_model",
            files=[
                FileInfo("/a.fbx", "a.fbx", 1000, "fbx", "m1", "model", True),
                FileInfo("/b.png", "b.png", 500, "png", "m2", "texture", False),
                FileInfo("/c.png", "c.png", 300, "png", "m3", "texture", False),
            ],
        )
        self.assertEqual(task.total_size, 1800)

    def test_empty_files_total_size_zero(self):
        task = UploadTask(task_id=1, content_md5="m", resource_type="other")
        self.assertEqual(task.total_size, 0)

    def test_task_with_no_previews(self):
        task = UploadTask(task_id=1, content_md5="m", resource_type="other",
                          files=[_make_file_info()])
        self.assertEqual(len(task.previews), 0)


class TestUploadOrchestratorMultiFile(unittest.TestCase):

    def setUp(self):
        self.db_path = os.path.join(os.environ.get("TEMP", "/tmp"), "test_orch_multi.db")
        self.cache = LocalCacheStore(self.db_path)
        self.client = MockCloudClient()
        self.orchestrator = UploadOrchestrator(self.client, self.cache)
        self.task_id = self.cache.insert_task(_make_entity())

    def tearDown(self):
        self.cache.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_multi_file_upload(self):
        """Orchestrator uploads all files in a multi-file resource."""
        files = [
            FileInfo("/a.fbx", "a.fbx", 1000, "fbx", "m1", "model", True),
            FileInfo("/b.png", "b.png", 500, "png", "m2", "texture", False),
        ]
        previews = [
            PreviewInfo(strategy=PreviewStrategy.GIF, role="primary",
                        path="/p/model.gif", format="gif", width=512, height=512,
                        size=30000, renderer="blender"),
            PreviewInfo(strategy=PreviewStrategy.STATIC, role="gallery",
                        path="/p/tex.webp", format="webp", width=256, height=256,
                        size=5000, renderer="pillow"),
        ]
        task = UploadTask(
            task_id=self.task_id,
            content_md5="composite",
            resource_type="3d_model",
            files=files,
            previews=previews,
            description_main="A chair",
            description_detail="Wooden chair",
            description_full="A detailed wooden chair model",
            embedding_dimension=3,
            embedding_vector_data=[0.1, 0.2, 0.3],
        )

        async def run():
            result = await self.orchestrator.execute(task)
            self.assertTrue(result.success)
            self.assertEqual(result.final_state, "committed")

            self.assertEqual(len(self.client.upload_files_calls), 1)
            _, uploaded_files = self.client.upload_files_calls[0]
            self.assertEqual(len(uploaded_files), 2)

            self.assertEqual(len(self.client.upload_previews_calls), 1)
            _, uploaded_previews = self.client.upload_previews_calls[0]
            self.assertEqual(len(uploaded_previews), 2)
            roles = {p.role for p in uploaded_previews}
            self.assertEqual(roles, {"primary", "gallery"})
        _run_async(run())

    def test_multi_file_no_previews_skips_preview_upload(self):
        """When task has no previews, upload_previews is not called."""
        task = UploadTask(
            task_id=self.task_id,
            content_md5="md5",
            resource_type="other",
            files=[_make_file_info()],
            previews=[],
            description_main="Test",
            description_detail="",
            description_full="",
        )

        async def run():
            result = await self.orchestrator.execute(task)
            self.assertTrue(result.success)
            self.assertEqual(len(self.client.upload_previews_calls), 0)
        _run_async(run())

    def test_upload_log_contains_file_and_preview_counts(self):
        """Upload log message includes counts of files and previews."""
        task = _make_task(self.task_id)

        async def run():
            await self.orchestrator.execute(task)
            logs = self.cache.get_logs(self.task_id)
            upload_logs = [lg for lg in logs if lg["event"] == "uploaded"]
            self.assertEqual(len(upload_logs), 1)
            self.assertIn("1 个文件", upload_logs[0]["detail"])
            self.assertIn("1 个预览", upload_logs[0]["detail"])
        _run_async(run())


class FailPreviewUploadClient(MockCloudClient):
    """upload_previews returns success=False."""

    async def upload_previews(self, resource_id, previews):
        self.upload_previews_calls.append((resource_id, previews))
        return UploadResult(success=False, error_message="preview storage full")


class TestUploadOrchestratorPreviewFailure(unittest.TestCase):

    def setUp(self):
        self.db_path = os.path.join(os.environ.get("TEMP", "/tmp"), "test_orch_prev_fail.db")
        self.cache = LocalCacheStore(self.db_path)
        self.client = FailPreviewUploadClient()
        self.orchestrator = UploadOrchestrator(self.client, self.cache)
        self.task_id = self.cache.insert_task(_make_entity())

    def tearDown(self):
        self.cache.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_preview_upload_failure(self):
        """When preview upload fails, the outcome reports failure."""
        task = _make_task(self.task_id)

        async def run():
            result = await self.orchestrator.execute(task)
            self.assertFalse(result.success)
            self.assertIn("预览上传失败", result.error_message)
            task_row = self.cache.get_task_by_id(self.task_id)
            self.assertGreater(task_row["retry_count"], 0)
        _run_async(run())


if __name__ == "__main__":
    unittest.main()
