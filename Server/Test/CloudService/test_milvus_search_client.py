import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_ROOT = Path(__file__).resolve().parents[3]
_SERVER_DIR = _ROOT / "Server"
for _p in (str(_ROOT), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from CloudService.search_client import DownloadLinkRequest, SearchRequest
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
    def __init__(self):
        self.presign_calls = []
        self.list_calls = []

    def generate_presigned_download_url(self, key, expires=None):
        self.presign_calls.append((key, expires))
        suffix = f"?expires={expires}" if expires is not None else ""
        return f"https://storage.local/{key}{suffix}"

    def list_keys(self, prefix, max_keys=100):
        self.list_calls.append((prefix, max_keys))
        return []


class TestMilvusSearchClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed_search_task(self, session):
        task = ResourceTask(
            content_md5="md5-search",
            resource_type="single_image",
            source_directory="assets",
            process_state="committed",
            resource_id="res-search-001",
            title="Stone Floor",
            source_resource_id="src-search-001",
            contains_resource_types_json=json.dumps(["single_image"]),
            download_object_key="downloads/res-search-001/stone-floor.zip",
            download_file_name="stone-floor.zip",
            download_content_type="application/zip",
            download_file_size=4096,
            idempotency_key="register-search-001",
        )
        session.add(task)
        await session.flush()
        session.add(
            ResourceDescription(
                task_id=task.id,
                main_content="stone floor material",
                detail_content="detail",
                full_description="full",
            )
        )
        session.add(
            ResourceFile(
                task_id=task.id,
                file_path="assets/floor.png",
                file_name="floor.png",
                file_size=1024,
                file_format="png",
                content_md5="file-md5",
                ks3_key="files/res-search-001/floor.png",
                is_primary=True,
            )
        )
        session.add(
            ResourcePreview(
                task_id=task.id,
                strategy="static",
                role="primary",
                path="stone-floor.webp",
                format="webp",
            )
        )
        await session.commit()
        return task

    async def test_search_builds_results_from_milvus_and_database(self):
        session = self.session_factory()
        await self._seed_search_task(session)

        fake_milvus = _FakeMilvus(
            [[{"distance": 0.91, "entity": {"resource_id": "res-search-001", "resource_type": "single_image"}}]]
        )
        fake_storage = _FakeStorage()
        client = MilvusSearchClient(fake_milvus, session, fake_storage)

        with patch("app.services.milvus_search_client._embed_query", new=AsyncMock(return_value=[0.1, 0.2, 0.3])):
            response = await client.search(
                SearchRequest(
                    query_text="stone floor",
                    resource_type="single_image",
                    format_filter=["png"],
                    top_k=5,
                    similarity_threshold=0.5,
                )
            )

        self.assertEqual(response.total_count, 1)
        self.assertIsNone(response.suggestion)
        item = response.results[0]
        self.assertEqual(item.resource_id, "res-search-001")
        self.assertEqual(item.title, "Stone Floor")
        self.assertEqual(item.description_summary, "stone floor material")
        self.assertEqual(item.file_format, "png")
        self.assertEqual(item.file_count, 1)
        self.assertEqual(item.file_download_url, "https://storage.local/downloads/res-search-001/stone-floor.zip")
        self.assertEqual(item.primary_preview_url, "https://storage.local/previews/res-search-001/stone-floor.webp")

        self.assertEqual(fake_milvus.search_calls[0]["filter"], 'resource_type == "single_image"')
        self.assertEqual(fake_milvus.search_calls[0]["limit"], 30)

        await session.close()

    async def test_search_filters_by_any_resource_file_format(self):
        session = self.session_factory()
        await self._seed_search_task(session)

        task = ResourceTask(
            content_md5="md5-pack",
            resource_type="pack",
            source_directory="assets/pack",
            process_state="committed",
            resource_id="res-pack-001",
            title="Starter Pack",
            source_resource_id="src-pack-001",
            contains_resource_types_json=json.dumps(["single_image", "atlas"]),
            idempotency_key="register-pack-001",
        )
        session.add(task)
        await session.flush()
        session.add_all(
            [
                ResourceDescription(
                    task_id=task.id,
                    main_content="starter pack",
                    detail_content="detail",
                    full_description="full",
                ),
                ResourceFile(
                    task_id=task.id,
                    file_path="assets/pack/model.fbx",
                    file_name="model.fbx",
                    file_size=2048,
                    file_format="fbx",
                    content_md5="fbx-md5",
                    ks3_key="files/res-pack-001/model.fbx",
                    is_primary=True,
                ),
                ResourceFile(
                    task_id=task.id,
                    file_path="assets/pack/preview.png",
                    file_name="preview.png",
                    file_size=512,
                    file_format="png",
                    content_md5="png-md5",
                    ks3_key="files/res-pack-001/preview.png",
                    is_primary=False,
                ),
            ]
        )
        await session.commit()

        fake_milvus = _FakeMilvus(
            [[
                {"distance": 0.95, "entity": {"resource_id": "res-pack-001", "resource_type": "pack"}},
                {"distance": 0.91, "entity": {"resource_id": "res-search-001", "resource_type": "single_image"}},
            ]]
        )
        client = MilvusSearchClient(fake_milvus, session, _FakeStorage())

        with patch("app.services.milvus_search_client._embed_query", new=AsyncMock(return_value=[0.1, 0.2, 0.3])):
            response = await client.search(
                SearchRequest(
                    query_text="starter pack",
                    format_filter=[".fbx"],
                    top_k=5,
                    similarity_threshold=0.5,
                )
            )

        self.assertEqual(response.total_count, 1)
        self.assertEqual(response.results[0].resource_id, "res-pack-001")
        self.assertEqual(response.results[0].resource_type, "pack")
        self.assertEqual(response.results[0].file_format, "fbx")
        self.assertEqual(fake_milvus.search_calls[0]["limit"], 30)

        await session.close()

    async def test_search_returns_suggestion_when_threshold_filters_everything(self):
        session = self.session_factory()
        fake_milvus = _FakeMilvus(
            [[{"distance": 0.2, "entity": {"resource_id": "res-miss", "resource_type": "single_image"}}]]
        )
        client = MilvusSearchClient(fake_milvus, session, _FakeStorage())

        with patch("app.services.milvus_search_client._embed_query", new=AsyncMock(return_value=[0.1, 0.2, 0.3])):
            response = await client.search(
                SearchRequest(query_text="stone floor", similarity_threshold=0.8)
            )

        self.assertEqual(response.total_count, 0)
        self.assertIsNotNone(response.suggestion)
        self.assertIn("format_filter", response.suggestion.relaxable_filters)

        await session.close()

    async def test_get_download_link_uses_download_object_and_expiry(self):
        session = self.session_factory()
        await self._seed_search_task(session)
        fake_storage = _FakeStorage()
        client = MilvusSearchClient(_FakeMilvus([]), session, fake_storage)

        before = datetime.now(timezone.utc)
        response = await client.get_download_link(
            DownloadLinkRequest(resource_id="res-search-001", expire_seconds=600)
        )
        after = datetime.now(timezone.utc)

        self.assertEqual(response.file_name, "stone-floor.zip")
        self.assertEqual(response.file_size, 4096)
        self.assertEqual(response.content_type, "application/zip")
        self.assertEqual(
            response.download_url,
            "https://storage.local/downloads/res-search-001/stone-floor.zip?expires=600",
        )

        expires_at = datetime.fromisoformat(response.expires_at)
        self.assertGreaterEqual(expires_at, before + timedelta(seconds=599))
        self.assertLessEqual(expires_at, after + timedelta(seconds=601))
        self.assertEqual(fake_storage.presign_calls[-1], ("downloads/res-search-001/stone-floor.zip", 600))

        await session.close()


if __name__ == "__main__":
    unittest.main()
