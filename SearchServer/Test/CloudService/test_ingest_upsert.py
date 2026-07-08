from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_SERVER_DIR = Path(__file__).resolve().parents[2]
_ROOT = _SERVER_DIR.parent
for _p in (str(_ROOT), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.config import settings
from app.models.tables import Base, ResourceDescription, ResourceEmbedding, ResourceFile, ResourcePreview, ResourceTask, VectorSyncJob
from app.routers.ingest import DeleteResourceIn, UpsertResourceIn, delete_processed_resource, upsert_processed_resource


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

    async def test_upsert_processed_resource_persists_object_refs_and_client_metadata(self):
        session = self.session_factory()
        fake_milvus = _FakeMilvus()
        body = UpsertResourceIn(
            client_id="client-a",
            client_resource_id="asset-1",
            resource_type="single_image",
            client_metadata={"tags": ["crawler-tag"], "generation_prompt": "blue square"},
            title="",
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
                "usage_space": "2D",
                "usage_category": "物件",
                "usage_subcategories": ["图标"],
            },
        )

        with (
            patch("app.routers.ingest.generate_embedding", new=AsyncMock(return_value=[0.1, 0.2])),
            patch("app.routers.ingest.get_model_version", return_value="unit-embedding"),
            patch("app.routers.ingest.get_milvus", return_value=fake_milvus),
            patch.object(settings, "embedding_dimension", 2),
        ):
            response = await upsert_processed_resource(body, session=session)

        self.assertEqual(response.state, "committed")
        task = (await session.execute(select(ResourceTask))).scalar_one()
        file_row = (await session.execute(select(ResourceFile))).scalar_one()
        preview_row = (await session.execute(select(ResourcePreview))).scalar_one()
        desc = (await session.execute(select(ResourceDescription))).scalar_one()
        emb = (await session.execute(select(ResourceEmbedding))).scalar_one()

        self.assertEqual(task.source, "client-a")
        self.assertEqual(task.source_resource_id, "asset-1")
        self.assertEqual(json.loads(task.client_metadata_json)["generation_prompt"], "blue square")
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
        self.assertEqual(emb.dimension, 2)
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

        with (
            patch("app.routers.ingest.generate_embedding", new=AsyncMock(return_value=[0.1, 0.2])),
            patch("app.routers.ingest.get_model_version", return_value="unit-embedding"),
            patch("app.routers.ingest.get_milvus", return_value=fake_milvus),
            patch.object(settings, "embedding_dimension", 2),
        ):
            first = await upsert_processed_resource(
                body("raw/source-v1.png", "processed/previews/asset-v1.webp", "first description"),
                session=session,
            )
            second = await upsert_processed_resource(
                body("raw/source-v2.png", "processed/previews/asset-v2.webp", "second description"),
                session=session,
            )

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
        self.assertEqual(len(fake_milvus.upsert_calls), 2)
        self.assertEqual(fake_milvus.upsert_calls[1]["data"][0]["resource_id"], first.resource_id)

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
            created = await upsert_processed_resource(body, session=session)
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
            created = await upsert_processed_resource(body, session=session)
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
            with self.assertRaises(HTTPException) as raised:
                await upsert_processed_resource(body, session=session)

        self.assertEqual(raised.exception.status_code, 502)
        task = (await session.execute(select(ResourceTask))).scalar_one()
        job = (await session.execute(select(VectorSyncJob))).scalar_one()
        self.assertEqual(task.vector_state, "failed")
        self.assertEqual(job.state, "failed")
        self.assertIn("milvus down", job.last_error)
        await session.close()


if __name__ == "__main__":
    unittest.main()
