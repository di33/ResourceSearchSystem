import asyncio
import os
import unittest

from ResourceProcessor.cloud_client import (
    BaseCloudClient,
    CommitRequest,
    CommitResponse,
    MockCloudClient,
    RegisterRequest,
    RegisterResponse,
    UploadResult,
)
from ResourceProcessor.local_cache import LocalCacheStore
from ResourceProcessor.preview_metadata import ProcessState, ResourceProcessingEntity
from ResourceProcessor.upload_orchestrator import UploadOrchestrator, UploadOutcome, UploadTask


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def _make_entity() -> ResourceProcessingEntity:
    return ResourceProcessingEntity(
        content_md5="d41d8cd98f00b204e9800998ecf8427e",
        resource_type="model",
        source_path="/tmp/cube.fbx",
        source_name="cube.fbx",
        source_size=2048,
        source_format="fbx",
        process_state=ProcessState.PACKAGE_READY,
    )


def _make_task(task_id: int) -> UploadTask:
    return UploadTask(
        task_id=task_id,
        content_md5="d41d8cd98f00b204e9800998ecf8427e",
        resource_type="model",
        file_name="cube.fbx",
        file_size=2048,
        file_path="/tmp/cube.fbx",
        preview_path="/tmp/preview.png",
        preview_format="png",
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
    """upload_file returns success=False."""

    async def upload_file(self, resource_id: str, file_path: str, file_size: int) -> UploadResult:
        self.upload_file_calls.append((resource_id, file_path, file_size))
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
        self.assertEqual(task.file_name, "cube.fbx")
        self.assertEqual(task.file_size, 2048)
        self.assertEqual(task.file_path, "/tmp/cube.fbx")
        self.assertEqual(task.preview_path, "/tmp/preview.png")
        self.assertEqual(task.preview_format, "png")
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
            self.assertEqual(req.file_name, self.task.file_name)
            self.assertEqual(req.file_size, self.task.file_size)
            self.assertEqual(req.preview_format, self.task.preview_format)
        _run_async(run())

    def test_execute_uploads_files(self):
        async def run():
            await self.orchestrator.execute(self.task)
            self.assertEqual(len(self.client.upload_file_calls), 1)
            self.assertEqual(len(self.client.upload_preview_calls), 1)
            rid, fpath, fsize = self.client.upload_file_calls[0]
            self.assertEqual(fpath, self.task.file_path)
            self.assertEqual(fsize, self.task.file_size)
            rid2, ppath = self.client.upload_preview_calls[0]
            self.assertEqual(ppath, self.task.preview_path)
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
            self.assertEqual(len(self.client.upload_file_calls), 0)
            self.assertEqual(len(self.client.upload_preview_calls), 0)
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


if __name__ == "__main__":
    unittest.main()
