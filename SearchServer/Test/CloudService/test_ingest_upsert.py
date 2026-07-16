from __future__ import annotations

import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_SERVER_DIR = Path(__file__).resolve().parents[2]
_ROOT = _SERVER_DIR.parent
for _p in (str(_ROOT), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.config import settings
from app.models.tables import Base, ResourceDescription, ResourceEmbedding, ResourceFile, ResourcePreview, ResourceTask, VectorSyncJob, _utcnow
from app.routers import ingest as ingest_router
from app.routers.ingest import (
    DeleteResourceIn,
    UpsertResourceIn,
    _claim_next_vector_sync_jobs,
    _backfill_fts_once,
    _run_vector_sync_jobs_batch,
    _run_vector_sync_job,
    delete_processed_resource,
    start_vector_sync_worker,
    stop_vector_sync_worker,
    upsert_processed_resource,
)


class _FakeMilvus:
    def __init__(self):
        self.upsert_calls = []
        self.delete_calls = []

    def upsert(self, **kwargs):
        self.upsert_calls.append(kwargs)

    def delete(self, **kwargs):
        self.delete_calls.append(kwargs)


class _FailingMilvus(_FakeMilvus):
    def upsert(self, **kwargs):
        super().upsert(**kwargs)
        raise RuntimeError("milvus down")


class TestIngestUpsert(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _upsert(self, body: UpsertResourceIn, session):
        return await upsert_processed_resource(body, background_tasks=BackgroundTasks(), session=session)

    async def _run_pending_vector_jobs(self, session):
        jobs = (
            await session.execute(
                select(VectorSyncJob)
                .where(VectorSyncJob.state.in_(["pending", "failed"]))
                .order_by(VectorSyncJob.id)
            )
        ).scalars().all()
        for job in jobs:
            await _run_vector_sync_job(session, job)

    async def test_upsert_processed_resource_persists_object_refs_and_classification(self):
        session = self.session_factory()
        fake_milvus = _FakeMilvus()
        body = UpsertResourceIn(
            client_id="client-a",
            client_resource_id="asset-1",
            resource_type="single_image",
            title="",
            client_metadata={
                "display_title": "Blue Square",
                "resource_path": "icons/blue_square.png",
            },
            source_object={
                "storage_profile_id": "default",
                "object_key": "raw/source.png",
                "file_name": "source.png",
                "file_format": "png",
                "size": 128,
                "checksum": "file-md5",
            },
            source_files=[
                {
                    "file_name": "source.png",
                    "file_format": "png",
                    "file_size": 128,
                    "checksum": "file-md5",
                    "is_primary": True,
                }
            ],
            package_object={
                "storage_profile_id": "package-profile",
                "object_key": "packages/source-pack.zip",
            },
            previews=[
                {
                    "role": "primary",
                    "storage_profile_id": "default",
                    "object_key": "processed/previews/asset-1.webp",
                    "width": 128,
                    "height": 128,
                    "size": 2048,
                    "origin": "generated",
                    "renderer": "unit-test",
                }
            ],
            description={
                "summary": "blue square image",
                "detail": "detail",
                "full": "full",
            },
            classification={
                "category": "icon",
                "tags": ["crawler-tag"],
                "usage_space": "2D",
                "usage_category": "物件",
                "usage_subcategories": ["图标"],
            },
        )

        embedding_mock = AsyncMock(return_value=[0.1, 0.2])
        with (
            patch("app.routers.ingest.generate_embedding", new=embedding_mock),
            patch("app.routers.ingest.get_model_version", return_value="unit-embedding"),
            patch("app.routers.ingest.get_milvus", return_value=fake_milvus),
            patch.object(settings, "embedding_dimension", 2),
        ):
            response = await self._upsert(body, session)
            self.assertEqual(embedding_mock.await_count, 0)
            self.assertEqual(fake_milvus.upsert_calls, [])
            self.assertEqual((await session.execute(select(ResourceEmbedding))).scalars().all(), [])
            await self._run_pending_vector_jobs(session)

        self.assertEqual(response.state, "committed")
        task = (await session.execute(select(ResourceTask))).scalar_one()
        file_row = (await session.execute(select(ResourceFile))).scalar_one()
        preview_row = (await session.execute(select(ResourcePreview))).scalar_one()
        desc = (await session.execute(select(ResourceDescription))).scalar_one()
        emb = (await session.execute(select(ResourceEmbedding))).scalar_one()

        self.assertEqual(task.source, "client-a")
        self.assertEqual(task.source_resource_id, "asset-1")
        self.assertEqual(
            json.loads(task.client_metadata_json),
            {"display_title": "Blue Square", "resource_path": "icons/blue_square.png"},
        )
        self.assertEqual(task.category, "icon")
        self.assertEqual(json.loads(task.tags_json), ["crawler-tag"])
        self.assertEqual(task.source_storage_profile_id, "default")
        self.assertEqual(task.source_object_key, "raw/source.png")
        self.assertEqual(task.package_storage_profile_id, "package-profile")
        self.assertEqual(task.package_object_key, "packages/source-pack.zip")
        self.assertEqual(file_row.file_name, "source.png")
        self.assertEqual(file_row.object_key, "")
        self.assertEqual(preview_row.origin, "generated")
        self.assertEqual(preview_row.storage_profile_id, "default")
        self.assertEqual(preview_row.object_key, "processed/previews/asset-1.webp")
        self.assertEqual(desc.main_content, "blue square image")
        self.assertEqual(desc.usage_space, "2D")
        self.assertEqual(desc.usage_category, "物件")
        self.assertEqual(json.loads(desc.usage_subcategories_json), ["图标"])
        self.assertEqual(emb.dimension, 2)
        self.assertEqual(task.vector_state, "synced")
        self.assertEqual(fake_milvus.upsert_calls[0]["data"][0]["resource_id"], response.resource_id)

        await session.close()

    async def test_upsert_processed_resource_replaces_existing_rows(self):
        session = self.session_factory()
        fake_milvus = _FakeMilvus()

        def body(source_key: str, preview_key: str, summary: str) -> UpsertResourceIn:
            return UpsertResourceIn(
                client_id="client-a",
                client_resource_id="asset-refresh",
                resource_type="single_image",
                source_object={
                    "storage_profile_id": "default",
                    "object_key": source_key,
                    "file_name": source_key.rsplit("/", 1)[-1],
                    "file_format": "png",
                    "size": 128,
                    "checksum": f"{source_key}-md5",
                },
                source_files=[
                    {
                        "file_name": "source.png",
                        "file_format": "png",
                        "file_size": 128,
                        "checksum": f"{source_key}-md5",
                        "is_primary": True,
                    }
                ],
                previews=[
                    {
                        "role": "primary",
                        "storage_profile_id": "default",
                        "object_key": preview_key,
                        "width": 128,
                        "height": 128,
                        "size": 2048,
                    }
                ],
                description={"summary": summary},
            )

        embedding_mock = AsyncMock(return_value=[0.1, 0.2])
        with (
            patch("app.routers.ingest.generate_embedding", new=embedding_mock),
            patch("app.routers.ingest.get_model_version", return_value="unit-embedding"),
            patch("app.routers.ingest.get_milvus", return_value=fake_milvus),
            patch.object(settings, "embedding_dimension", 2),
        ):
            first = await self._upsert(
                body("raw/source-v1.png", "processed/previews/asset-v1.webp", "first description"),
                session,
            )
            second = await self._upsert(
                body("raw/source-v2.png", "processed/previews/asset-v2.webp", "second description"),
                session,
            )
            self.assertEqual(embedding_mock.await_count, 0)
            await self._run_pending_vector_jobs(session)

        self.assertEqual(second.resource_id, first.resource_id)
        task = (await session.execute(select(ResourceTask))).scalar_one()
        previews = (await session.execute(select(ResourcePreview))).scalars().all()
        descriptions = (await session.execute(select(ResourceDescription))).scalars().all()
        embeddings = (await session.execute(select(ResourceEmbedding))).scalars().all()

        self.assertEqual(task.source_object_key, "raw/source-v2.png")
        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0].object_key, "processed/previews/asset-v2.webp")
        self.assertEqual(len(descriptions), 1)
        self.assertEqual(descriptions[0].main_content, "second description")
        self.assertEqual(len(embeddings), 1)
        self.assertEqual(len(fake_milvus.upsert_calls), 1)
        self.assertEqual(fake_milvus.upsert_calls[0]["data"][0]["resource_id"], first.resource_id)
        jobs = (await session.execute(select(VectorSyncJob).order_by(VectorSyncJob.id))).scalars().all()
        self.assertEqual([job.state for job in jobs], ["superseded", "completed"])

        await session.close()

    async def test_delete_processed_resource_removes_db_rows_and_vector(self):
        session = self.session_factory()
        fake_milvus = _FakeMilvus()
        body = UpsertResourceIn(
            client_id="client-a",
            client_resource_id="asset-delete",
            resource_type="single_image",
            source_object={
                "storage_profile_id": "default",
                "object_key": "raw/delete.png",
                "file_name": "delete.png",
                "file_format": "png",
                "size": 128,
                "checksum": "delete-md5",
            },
            source_files=[
                {
                    "file_name": "delete.png",
                    "file_format": "png",
                    "file_size": 128,
                    "checksum": "delete-md5",
                    "is_primary": True,
                }
            ],
            package_object={
                "storage_profile_id": "package-profile",
                "object_key": "packages/delete-pack.zip",
            },
            previews=[
                {
                    "role": "primary",
                    "storage_profile_id": "default",
                    "object_key": "processed/previews/delete.webp",
                    "width": 128,
                    "height": 128,
                    "size": 2048,
                }
            ],
            description={"summary": "delete me"},
        )

        with (
            patch("app.routers.ingest.generate_embedding", new=AsyncMock(return_value=[0.1, 0.2])),
            patch("app.routers.ingest.get_model_version", return_value="unit-embedding"),
            patch("app.routers.ingest.get_milvus", return_value=fake_milvus),
            patch.object(settings, "embedding_dimension", 2),
        ):
            created = await self._upsert(body, session)
            deleted = await delete_processed_resource(
                DeleteResourceIn(client_id="client-a", client_resource_id="asset-delete"),
                session=session,
            )

        self.assertEqual(deleted.state, "deleted")
        self.assertEqual(deleted.resource_id, created.resource_id)
        self.assertEqual(
            {(ref.storage_profile_id, ref.object_key, ref.kind) for ref in deleted.object_refs},
            {
                ("default", "raw/delete.png", "source_object"),
                ("package-profile", "packages/delete-pack.zip", "package_object"),
                ("default", "processed/previews/delete.webp", "preview"),
            },
        )
        self.assertEqual((await session.execute(select(ResourceTask))).scalars().all(), [])
        self.assertEqual((await session.execute(select(ResourcePreview))).scalars().all(), [])
        self.assertEqual(fake_milvus.delete_calls[0]["filter"], f'resource_id == "{created.resource_id}"')

        await session.close()

    async def test_delete_by_resource_id_rejects_other_client(self):
        session = self.session_factory()
        fake_milvus = _FakeMilvus()
        body = UpsertResourceIn(
            client_id="client-a",
            client_resource_id="asset-private",
            resource_type="single_image",
            source_object={"storage_profile_id": "default", "object_key": "raw/private.png"},
            source_files=[{"file_name": "private.png", "file_format": "png", "is_primary": True}],
            description={"summary": "private asset"},
        )

        with (
            patch("app.routers.ingest.generate_embedding", new=AsyncMock(return_value=[0.1, 0.2])),
            patch("app.routers.ingest.get_model_version", return_value="unit-embedding"),
            patch("app.routers.ingest.get_milvus", return_value=fake_milvus),
            patch.object(settings, "embedding_dimension", 2),
        ):
            created = await self._upsert(body, session)
            with self.assertRaises(HTTPException) as raised:
                await delete_processed_resource(
                    DeleteResourceIn(client_id="client-b", resource_id=created.resource_id),
                    session=session,
                )

        self.assertEqual(raised.exception.status_code, 403)
        self.assertIsNotNone((await session.execute(select(ResourceTask))).scalar_one_or_none())
        await session.close()

    async def test_vector_sync_failure_keeps_db_row_and_failed_job(self):
        session = self.session_factory()
        body = UpsertResourceIn(
            client_id="client-a",
            client_resource_id="asset-vector-fail",
            resource_type="single_image",
            source_object={"storage_profile_id": "default", "object_key": "raw/vector.png"},
            source_files=[{"file_name": "vector.png", "file_format": "png", "is_primary": True}],
            description={"summary": "vector failure asset"},
        )

        with (
            patch("app.routers.ingest.generate_embedding", new=AsyncMock(return_value=[0.1, 0.2])),
            patch("app.routers.ingest.get_model_version", return_value="unit-embedding"),
            patch("app.routers.ingest.get_milvus", return_value=_FailingMilvus()),
            patch.object(settings, "embedding_dimension", 2),
        ):
            await self._upsert(body, session)
            job = (await session.execute(select(VectorSyncJob))).scalar_one()
            with self.assertRaises(HTTPException) as raised:
                await _run_vector_sync_job(session, job)

        self.assertEqual(raised.exception.status_code, 502)
        task = (await session.execute(select(ResourceTask))).scalar_one()
        job = (await session.execute(select(VectorSyncJob))).scalar_one()
        self.assertEqual(task.vector_state, "failed")
        self.assertEqual(job.state, "failed")
        self.assertIn("milvus down", job.last_error)
        await session.close()

    async def test_upsert_processed_resource_accepts_long_prompt_version(self):
        session = self.session_factory()
        fake_milvus = _FakeMilvus()
        body = UpsertResourceIn(
            client_id="client-a",
            client_resource_id="asset-long-prompt-version",
            resource_type="single_image",
            source_object={"storage_profile_id": "default", "object_key": "raw/source.png"},
            source_files=[{"file_name": "source.png", "file_format": "png", "is_primary": True}],
            description={"summary": "image asset", "detail": "detail", "full": "full"},
            processing={"description_prompt_version": "prompt_v1+resource_processing_server"},
        )

        with (
            patch("app.routers.ingest.generate_embedding", new=AsyncMock(return_value=[0.1, 0.2])),
            patch("app.routers.ingest.get_model_version", return_value="unit-embedding"),
            patch("app.routers.ingest.get_milvus", return_value=fake_milvus),
            patch.object(settings, "embedding_dimension", 2),
        ):
            await self._upsert(body, session)

        desc = (await session.execute(select(ResourceDescription))).scalar_one()
        self.assertEqual(desc.prompt_version, "prompt_v1+resource_processing_server")
        await session.close()

    async def test_upsert_processed_resource_rejects_pack_resources(self):
        session = self.session_factory()
        body = UpsertResourceIn(
            client_id="client-a",
            client_resource_id="pack-rejected",
            resource_type="pack",
            source_object={"storage_profile_id": "default", "object_key": "raw/source.zip"},
            source_files=[{"file_name": "source.zip", "file_format": "zip", "is_primary": True}],
            description={"summary": "package only"},
        )

        with self.assertRaises(HTTPException) as raised:
            await self._upsert(body, session)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual((await session.execute(select(ResourceTask))).scalars().all(), [])
        await session.close()

    async def test_vector_sync_worker_claims_pending_jobs(self):
        session = self.session_factory()
        jobs = [
            VectorSyncJob(
                resource_id=f"res-worker-{i}",
                action="upsert",
                resource_type="single_image",
                embedding_text=f"worker generated embedding {i}",
                state="pending",
            )
            for i in range(3)
        ]
        session.add_all(jobs)
        await session.commit()

        claimed = await _claim_next_vector_sync_jobs(session, 2)

        self.assertEqual(claimed, [jobs[0].id, jobs[1].id])
        refreshed = (await session.execute(select(VectorSyncJob).order_by(VectorSyncJob.id))).scalars().all()
        self.assertEqual([job.state for job in refreshed], ["running", "running", "pending"])
        await session.close()

    async def test_vector_sync_batch_does_not_count_failed_writes_as_processed(self):
        session = self.session_factory()
        task = ResourceTask(
            resource_id="res-batch-write-failure",
            content_md5="batch-write-failure",
            resource_type="single_image",
            process_state="committed",
        )
        job = VectorSyncJob(
            resource_id=task.resource_id,
            action="upsert",
            resource_type=task.resource_type,
            embedding_text="embedding text",
            state="running",
        )
        session.add_all([task, job])
        await session.commit()
        job_id = job.id
        await session.close()

        with (
            patch("app.routers.ingest.async_session_factory", self.session_factory),
            patch("app.routers.ingest.generate_embeddings", new=AsyncMock(return_value=[[0.1, 0.2]])),
            patch("app.routers.ingest._write_vectors", return_value="vector insert failed: unavailable"),
            patch.object(settings, "embedding_dimension", 2),
        ):
            processed = await _run_vector_sync_jobs_batch([job_id])

        self.assertEqual(processed, 0)
        verify = self.session_factory()
        refreshed = await verify.get(VectorSyncJob, job_id)
        self.assertEqual(refreshed.state, "failed")
        await verify.close()

    async def test_vector_sync_worker_defers_recent_failed_jobs(self):
        session = self.session_factory()
        now = _utcnow()
        jobs = [
            VectorSyncJob(
                resource_id="res-pending",
                action="upsert",
                resource_type="single_image",
                embedding_text="pending embedding",
                state="pending",
            ),
            VectorSyncJob(
                resource_id="res-failed-later",
                action="upsert",
                resource_type="single_image",
                embedding_text="failed later embedding",
                state="failed",
                retry_after=now + timedelta(minutes=1),
            ),
            VectorSyncJob(
                resource_id="res-failed-ready",
                action="upsert",
                resource_type="single_image",
                embedding_text="failed ready embedding",
                state="failed",
                retry_after=now - timedelta(seconds=1),
            ),
        ]
        session.add_all(jobs)
        await session.commit()

        claimed = await _claim_next_vector_sync_jobs(session, 3)

        self.assertEqual(claimed, [jobs[0].id, jobs[2].id])
        refreshed = (await session.execute(select(VectorSyncJob).order_by(VectorSyncJob.id))).scalars().all()
        self.assertEqual([job.state for job in refreshed], ["running", "failed", "running"])
        await session.close()

    async def test_fts_worker_backfills_null_search_vectors(self):
        session = self.session_factory()
        description = ResourceDescription(
            task_id=1,
            full_description="blue square icon",
            search_vector=None,
        )
        session.add(description)
        await session.commit()

        with patch.object(settings, "search_text_config", "simple"):
            processed = await _backfill_fts_once(10, session)

        self.assertEqual(processed, 1)
        refreshed = await session.get(ResourceDescription, description.id)
        self.assertTrue(refreshed.search_vector)
        await session.close()

    async def test_vector_sync_worker_starts_configured_concurrency(self):
        stop_calls = []

        async def _fake_worker(stop_event, worker_id=1):
            stop_calls.append(worker_id)
            await stop_event.wait()

        with (
            patch("app.routers.ingest._vector_sync_worker_loop", new=_fake_worker),
            patch.object(settings, "vector_sync_worker_concurrency", 3),
            patch.object(settings, "vector_sync_worker_enabled", True),
        ):
            start_vector_sync_worker()
            self.assertEqual(len(ingest_router._vector_sync_worker_tasks), 3)
            await stop_vector_sync_worker()

        self.assertEqual(sorted(stop_calls), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
