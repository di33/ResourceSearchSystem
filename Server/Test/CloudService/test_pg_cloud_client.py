import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_ROOT = Path(__file__).resolve().parents[3]
_SERVER_DIR = _ROOT / "Server"
for _p in (str(_ROOT), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from CloudService.cloud_client import CommitRequest, FileInfo, RegisterRequest
from app.config import settings
from app.models.tables import Base, ProcessLog, ResourceDescription, ResourceEmbedding, ResourceTask
from app.services.pg_cloud_client import PgCloudClient


class _FakeStorage:
    def upload_file(self, key, file_path):
        return 0

    def upload_fileobj(self, key, fileobj, content_type):
        return 0


class _FakeMilvus:
    def __init__(self):
        self.insert_calls = []

    def insert(self, **kwargs):
        self.insert_calls.append(kwargs)


class TestPgCloudClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _make_client(self, milvus=None):
        session = self.session_factory()
        client = PgCloudClient(session, _FakeStorage(), milvus_client=milvus)
        return session, client

    async def _create_registered_task(self, session, resource_id="res-test-001"):
        task = ResourceTask(
            content_md5="md5-001",
            resource_type="single_image",
            source_directory="assets",
            process_state="registered",
            resource_id=resource_id,
            idempotency_key=f"register-{resource_id}",
        )
        session.add(task)
        await session.flush()
        session.add(
            ProcessLog(task_id=task.id, event="registered", detail=f"resource_id={resource_id}, files=0")
        )
        await session.commit()
        return task

    async def test_register_creates_task_files_and_log(self):
        session, client = await self._make_client()
        request = RegisterRequest(
            content_md5="md5-register",
            resource_type="single_image",
            source_resource_id="src-001",
            title="Stone Floor",
            files=[
                FileInfo(
                    file_path="assets/floor.png",
                    file_name="floor.png",
                    file_size=128,
                    file_format="png",
                    content_md5="file-md5",
                    is_primary=True,
                )
            ],
        )

        response = await client.register(request)
        await session.commit()

        self.assertFalse(response.exists)
        task = (
            await session.execute(
                select(ResourceTask).where(ResourceTask.resource_id == response.resource_id)
            )
        ).scalar_one()
        self.assertEqual(task.title, "Stone Floor")
        self.assertEqual(task.process_state, "registered")
        self.assertEqual(len(task.files), 1)

        logs = (
            await session.execute(
                select(ProcessLog).where(ProcessLog.task_id == task.id)
            )
        ).scalars().all()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].event, "registered")

        await session.close()

    async def test_register_reuses_existing_task_by_source_resource_id(self):
        session, client = await self._make_client()
        first = RegisterRequest(
            content_md5="md5-register-a",
            resource_type="single_image",
            source="kenney",
            source_resource_id="src-dup-001",
            title="Stone Floor",
            files=[
                FileInfo(
                    file_path="assets/floor.png",
                    file_name="floor.png",
                    file_size=128,
                    file_format="png",
                    content_md5="file-md5-a",
                    is_primary=True,
                )
            ],
        )
        first_resp = await client.register(first)
        await session.commit()

        second = RegisterRequest(
            content_md5="md5-register-b",
            resource_type="single_image",
            source="kenney",
            source_resource_id="src-dup-001",
            title="Stone Floor Updated",
            files=[
                FileInfo(
                    file_path="assets/floor_v2.png",
                    file_name="floor_v2.png",
                    file_size=256,
                    file_format="png",
                    content_md5="file-md5-b",
                    is_primary=True,
                )
            ],
        )
        second_resp = await client.register(second)
        await session.commit()

        self.assertTrue(second_resp.exists)
        self.assertEqual(second_resp.resource_id, first_resp.resource_id)

        rows = (await session.execute(select(ResourceTask))).scalars().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_resource_id, "src-dup-001")
        self.assertEqual(rows[0].resource_id, first_resp.resource_id)

        await session.close()

    async def test_commit_rejects_blank_main_description_without_persisting_rows(self):
        session, client = await self._make_client(milvus=_FakeMilvus())
        task = await self._create_registered_task(session)
        resource_id = task.resource_id

        response = await client.commit(
            CommitRequest(
                resource_id=resource_id,
                resource_type=task.resource_type,
                description_main="   ",
                description_detail="detail",
                description_full="full",
            )
        )
        await session.rollback()

        self.assertEqual(response.state, "failed")
        self.assertIn("description_main is required", response.error_message)

        desc_rows = (await session.execute(select(ResourceDescription))).scalars().all()
        emb_rows = (await session.execute(select(ResourceEmbedding))).scalars().all()
        reloaded = (
            await session.execute(
                select(ResourceTask).where(ResourceTask.resource_id == resource_id)
            )
        ).scalar_one()
        self.assertEqual(desc_rows, [])
        self.assertEqual(emb_rows, [])
        self.assertEqual(reloaded.process_state, "registered")

        await session.close()

    async def test_commit_success_persists_description_embedding_and_milvus_vector(self):
        fake_milvus = _FakeMilvus()
        session, client = await self._make_client(milvus=fake_milvus)
        task = await self._create_registered_task(session, resource_id="res-test-002")

        with (
            patch("app.services.embedding_client.generate_embedding", new=AsyncMock(return_value=[0.1, 0.2, 0.3])),
            patch("app.services.embedding_client.get_model_version", return_value="unit-test-model"),
            patch.object(settings, "embedding_dimension", 3),
        ):
            response = await client.commit(
                CommitRequest(
                    resource_id=task.resource_id,
                    resource_type=task.resource_type,
                    description_main="stone floor texture",
                    description_detail="detail",
                    description_full="full",
                )
            )
            await session.commit()

        self.assertEqual(response.state, "committed")

        desc_rows = (await session.execute(select(ResourceDescription))).scalars().all()
        emb_rows = (await session.execute(select(ResourceEmbedding))).scalars().all()
        logs = (
            await session.execute(
                select(ProcessLog).where(ProcessLog.task_id == task.id).order_by(ProcessLog.id)
            )
        ).scalars().all()
        reloaded = (
            await session.execute(
                select(ResourceTask).where(ResourceTask.resource_id == task.resource_id)
            )
        ).scalar_one()

        self.assertEqual(len(desc_rows), 1)
        self.assertEqual(desc_rows[0].main_content, "stone floor texture")
        self.assertEqual(len(emb_rows), 1)
        self.assertEqual(emb_rows[0].dimension, 3)
        self.assertEqual(emb_rows[0].model_version, "unit-test-model")
        self.assertEqual(reloaded.process_state, "committed")
        self.assertEqual([log.event for log in logs], ["registered", "committed"])
        self.assertEqual(len(fake_milvus.insert_calls), 1)
        self.assertEqual(fake_milvus.insert_calls[0]["collection_name"], settings.milvus_collection)
        self.assertEqual(fake_milvus.insert_calls[0]["data"][0]["resource_id"], task.resource_id)

        await session.close()


if __name__ == "__main__":
    unittest.main()
