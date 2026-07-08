from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from resource_processing_server.app.config import settings
from resource_processing_server.app.models import (
    DeleteProcessedResourceIn,
    JobState,
    JobStep,
    ObjectRef,
    PreviewRef,
    ProvidedDescription,
    ResourceManifest,
    SourceFileRef,
)
from resource_processing_server.app.processor import JobStore, ProcessingService
from resource_processing_server.app.preview_renderer_client import RenderedPreviewFile
from resource_processing_server.app.snapshots import ProcessedSnapshotStore


class FakeStorage:
    def __init__(self, objects: dict[tuple[str, str], Path], upload_dir: Path):
        self.objects = objects
        self.upload_dir = upload_dir
        self.uploaded: list[Path] = []
        self.deleted_refs = []

    def download_ref(self, ref, target_dir: Path, filename: str = "") -> Path:
        target_dir.mkdir(parents=True, exist_ok=True)
        source = self.objects[(ref.storage_profile_id or "default", ref.object_key)]
        target = target_dir / (filename or Path(ref.object_key).name)
        shutil.copy2(source, target)
        return target

    def upload_preview(
        self,
        local_path: str,
        *,
        client_id: str,
        client_resource_id: str,
        preview_name: str = "",
        role: str = "primary",
    ) -> PreviewRef:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        source = Path(local_path)
        target = self.upload_dir / (preview_name or source.name)
        shutil.copy2(source, target)
        self.uploaded.append(target)
        return PreviewRef(
            role=role,
            storage_profile_id="default",
            object_key=f"{client_id}/previews/{client_resource_id}/{preview_name or source.name}",
            size=target.stat().st_size,
            origin="generated",
            renderer="fake-storage",
        )

    def generate_read_url(self, ref, *, expires=None) -> str:
        return f"https://storage.local/{ref.object_key}?expires={expires or 0}"

    def delete_refs(self, refs):
        self.deleted_refs.extend(refs)
        return len(refs)


class FakeSearchClient:
    def __init__(self):
        self.payloads = []
        self.delete_payloads = []
        self.delete_result = None

    async def upsert_resource(self, payload):
        self.payloads.append(payload)
        return {"resource_id": f"res-{payload['client_resource_id']}"}

    async def delete_resource(self, payload):
        self.delete_payloads.append(payload)
        if self.delete_result is not None:
            return self.delete_result
        return {
            "resource_id": payload.get("resource_id") or f"res-{payload['client_resource_id']}",
            "state": "deleted",
            "deleted": True,
            "object_refs": [],
        }


class FailingSearchClient:
    async def upsert_resource(self, payload):
        raise RuntimeError("search is down")


class FakePreviewRenderer:
    enabled = True

    async def render_previews(self, *, client_id, manifest, source_object_url, output_dir):
        assert source_object_url.startswith("https://storage.local/")
        output_dir.mkdir(parents=True, exist_ok=True)
        preview_path = output_dir / "primary.png"
        _write_test_image(preview_path)
        return [
            RenderedPreviewFile(
                path=preview_path,
                role="primary",
                file_name="primary.png",
                content_type="image/png",
                width=128,
                height=128,
                size=preview_path.stat().st_size,
                strategy="static",
                origin="generated",
                renderer="preview-renderer",
            )
        ]


def _write_test_image(path: Path) -> None:
    image = Image.new("RGB", (128, 128), (220, 120, 80))
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 108, 108), fill=(40, 120, 220))
    image.save(path)


@pytest.mark.asyncio
async def test_processing_service_reuses_legacy_preview_and_description(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "work_dir", str(tmp_path / "work"))
    monkeypatch.setattr(settings, "llm_provider", "mock")

    source_path = tmp_path / "source.png"
    _write_test_image(source_path)

    storage = FakeStorage({("default", "raw/source.png"): source_path}, tmp_path / "uploaded")
    search = FakeSearchClient()
    service = ProcessingService(
        storage=storage,
        search_client=search,
        snapshot_store=ProcessedSnapshotStore(str(tmp_path / "snapshots.db")),
        store=JobStore(),
    )

    manifest = ResourceManifest(
        request_id="req-1",
        client_resource_id="asset-1",
        resource_type="single_image",
        source_object=ObjectRef(
                object_key="raw/source.png",
                file_name="source.png",
                file_format="png",
                size=source_path.stat().st_size,
        ),
        source_files=[
            SourceFileRef(
                file_name="source.png",
                file_format="png",
                file_size=source_path.stat().st_size,
                is_primary=True,
            )
        ],
        package_object=ObjectRef(
            storage_profile_id="package-profile",
            object_key="packages/source-pack.zip",
        ),
        client_metadata={
            "tags": ["crawler-tag"],
            "generation_prompt": "blue square icon",
        },
    )

    created = await service.create_job(client_id="client-a", manifest=manifest)
    await service.run_job(created.job_id)
    job = await service.get_job(created.job_id, client_id="client-a")

    assert job is not None
    assert job.state == "completed"
    assert job.search_resource_id == "res-asset-1"
    assert storage.uploaded
    assert len(search.payloads) == 1

    payload = search.payloads[0]
    assert payload["client_id"] == "client-a"
    assert payload["client_resource_id"] == "asset-1"
    assert payload["client_metadata"]["generation_prompt"] == "blue square icon"
    assert payload["title"] == "source.png"
    assert payload["package_object"] == {
        "storage_profile_id": "package-profile",
        "object_key": "packages/source-pack.zip",
        "file_name": "",
        "file_format": "",
        "size": 0,
        "checksum": "",
        "etag": "",
        "is_primary": False,
    }
    assert payload["previews"][0]["origin"] == "generated"
    assert payload["previews"][0]["object_key"] == "client-a/previews/asset-1/primary.webp"
    assert payload["description"]["summary"]


@pytest.mark.asyncio
async def test_processing_service_uploads_remote_renderer_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "work_dir", str(tmp_path / "work"))
    monkeypatch.setattr(settings, "llm_provider", "mock")

    source_path = tmp_path / "source.png"
    _write_test_image(source_path)

    storage = FakeStorage({("default", "raw/source.png"): source_path}, tmp_path / "uploaded")
    search = FakeSearchClient()
    service = ProcessingService(
        storage=storage,
        search_client=search,
        snapshot_store=ProcessedSnapshotStore(str(tmp_path / "snapshots.db")),
        store=JobStore(),
    )
    service.preview_renderer = FakePreviewRenderer()

    manifest = ResourceManifest(
        request_id="req-renderer",
        client_resource_id="asset-renderer",
        resource_type="single_image",
        source_object=ObjectRef(
            object_key="raw/source.png",
            file_name="source.png",
            file_format="png",
            size=source_path.stat().st_size,
        ),
        source_files=[
            SourceFileRef(
                file_name="source.png",
                file_format="png",
                file_size=source_path.stat().st_size,
                is_primary=True,
            )
        ],
    )

    created = await service.create_job(client_id="client-a", manifest=manifest)
    await service.run_job(created.job_id)

    assert storage.uploaded
    payload = search.payloads[0]
    assert payload["previews"][0]["origin"] == "generated"
    assert payload["previews"][0]["renderer"] == "preview-renderer"
    assert payload["previews"][0]["object_key"] == "client-a/previews/asset-renderer/primary.png"
    assert payload["previews"][0]["width"] == 128


@pytest.mark.asyncio
async def test_processing_service_uses_provided_preview_and_description(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "work_dir", str(tmp_path / "work"))
    monkeypatch.setattr(settings, "llm_provider", "mock")

    source_path = tmp_path / "source.png"
    preview_path = tmp_path / "preview.png"
    _write_test_image(source_path)
    _write_test_image(preview_path)

    async def fail_generate_previews(*args, **kwargs):
        raise AssertionError("preview generation should be skipped")

    async def fail_generate_description(*args, **kwargs):
        raise AssertionError("description generation should be skipped")

    monkeypatch.setattr("resource_processing_server.app.processor.generate_previews", fail_generate_previews)
    monkeypatch.setattr("resource_processing_server.app.processor.generate_description", fail_generate_description)

    storage = FakeStorage(
        {
            ("default", "raw/source.png"): source_path,
            ("default", "provided/preview.png"): preview_path,
        },
        tmp_path / "uploaded",
    )
    search = FakeSearchClient()
    snapshot_store = ProcessedSnapshotStore(str(tmp_path / "snapshots.db"))
    service = ProcessingService(storage=storage, search_client=search, snapshot_store=snapshot_store, store=JobStore())

    manifest = ResourceManifest(
        request_id="req-2",
        client_resource_id="asset-2",
        resource_type="single_image",
        source_object=ObjectRef(
                object_key="raw/source.png",
                file_name="source.png",
                file_format="png",
                size=source_path.stat().st_size,
        ),
        source_files=[
            SourceFileRef(
                file_name="source.png",
                file_format="png",
                file_size=source_path.stat().st_size,
                is_primary=True,
            )
        ],
        provided_previews=[
            PreviewRef(
                object_key="provided/preview.png",
                width=128,
                height=128,
                origin="provided",
            )
        ],
        provided_description=ProvidedDescription(
            main_content="客户端提供的主描述",
            detail_content="客户端提供的详细描述",
            prompt_version="client-desc-v1",
        ),
    )

    created = await service.create_job(client_id="client-a", manifest=manifest)
    await service.run_job(created.job_id)

    assert not storage.uploaded
    payload = search.payloads[0]
    assert payload["previews"][0]["origin"] == "provided"
    assert payload["description"]["summary"] == "客户端提供的主描述"
    assert payload["description"]["detail"] == "客户端提供的详细描述"
    assert payload["processing"]["preview_source"] == "provided"
    assert payload["processing"]["description_source"] == "provided"

    snapshot = snapshot_store.get(client_id="client-a", client_resource_id="asset-2")
    assert snapshot is not None
    assert snapshot["search_upsert_state"] == "upserted"
    assert snapshot["search_resource_id"] == "res-asset-2"
    assert snapshot["snapshot"]["description"]["summary"] == "客户端提供的主描述"
    assert snapshot["snapshot"]["description"]["full"] == "客户端提供的主描述\n客户端提供的详细描述"


@pytest.mark.asyncio
async def test_processing_service_creates_batch_jobs(tmp_path):
    service = ProcessingService(
        storage=FakeStorage({}, tmp_path / "uploaded"),
        search_client=FakeSearchClient(),
        snapshot_store=ProcessedSnapshotStore(str(tmp_path / "snapshots.db")),
        store=JobStore(),
    )

    def manifest(resource_id: str) -> ResourceManifest:
        return ResourceManifest(
            client_resource_id=resource_id,
            resource_type="single_image",
            source_object=ObjectRef(
                object_key=f"raw/{resource_id}.png",
                file_name=f"{resource_id}.png",
            ),
            source_files=[
                SourceFileRef(
                    file_name=f"{resource_id}.png",
                )
            ],
        )

    created = await service.create_batch(
        client_id="client-a",
        manifests=[manifest("asset-a"), manifest("asset-b")],
    )

    assert created.batch_id.startswith("batch_")
    assert [item.client_resource_id for item in created.jobs] == ["asset-a", "asset-b"]
    stored = [await service.store.get(item.job_id) for item in created.jobs]
    assert {job.batch_id for job in stored if job is not None} == {created.batch_id}


@pytest.mark.asyncio
async def test_processing_job_store_persists_jobs_and_marks_interrupted(tmp_path):
    db_path = tmp_path / "jobs.db"
    manifest = ResourceManifest(
        client_resource_id="asset-persist",
        resource_type="single_image",
        source_object=ObjectRef(object_key="raw/asset.png"),
        source_files=[SourceFileRef(file_name="asset.png")],
    )
    store = JobStore(str(db_path))

    created = await store.create(client_id="client-a", manifest=manifest)
    await store.append_step(created.job_id, JobStep(name="validate", state="completed"))
    await store.update(created.job_id, state=JobState.SUBMITTING)

    reopened = JobStore(str(db_path))
    restored = await reopened.get(created.job_id)

    assert restored is not None
    assert restored.state == JobState.SUBMITTING
    assert restored.steps[0].name == "validate"
    assert restored.manifest.client_resource_id == "asset-persist"

    assert reopened.mark_interrupted_failed("server restarted") == 1
    failed = await reopened.get(created.job_id)
    assert failed is not None
    assert failed.state == JobState.FAILED
    assert failed.error == "server restarted"


@pytest.mark.asyncio
async def test_processing_service_job_access_is_scoped_by_client_id(tmp_path):
    service = ProcessingService(
        storage=FakeStorage({}, tmp_path / "uploaded"),
        search_client=FakeSearchClient(),
        snapshot_store=ProcessedSnapshotStore(str(tmp_path / "snapshots.db")),
        store=JobStore(),
    )
    manifest = ResourceManifest(
        client_resource_id="asset-scope",
        resource_type="single_image",
        source_object=ObjectRef(object_key="raw/asset.png"),
        source_files=[SourceFileRef(file_name="asset.png")],
    )

    created = await service.create_job(client_id="client-a", manifest=manifest)

    assert await service.get_job(created.job_id, client_id="client-b") is None
    assert await service.retry_job(created.job_id, client_id="client-b") is None
    assert await service.cancel_job(created.job_id, client_id="client-b") is None

    cancelled = await service.cancel_job(created.job_id, client_id="client-a")

    assert cancelled is not None
    assert cancelled.state == "cancelled"


@pytest.mark.asyncio
async def test_delete_processed_resource_cancels_active_job_and_skips_upsert(tmp_path):
    snapshot_store = ProcessedSnapshotStore(str(tmp_path / "snapshots.db"))
    search = FakeSearchClient()
    search.delete_result = {
        "resource_id": "",
        "state": "not_found",
        "deleted": False,
        "object_refs": [],
    }
    service = ProcessingService(
        storage=FakeStorage({}, tmp_path / "uploaded"),
        search_client=search,
        snapshot_store=snapshot_store,
        store=JobStore(),
    )
    manifest = ResourceManifest(
        client_resource_id="asset-delete-active",
        resource_type="single_image",
        source_object=ObjectRef(object_key="raw/asset.png"),
        source_files=[SourceFileRef(file_name="asset.png")],
    )

    created = await service.create_job(client_id="client-a", manifest=manifest)
    result = await service.delete_processed_resource(
        client_id="client-a",
        request=DeleteProcessedResourceIn(client_resource_id="asset-delete-active"),
    )
    await service.run_job(created.job_id)
    job = await service.get_job(created.job_id, client_id="client-a")

    assert result.state == "not_found"
    assert job is not None
    assert job.state == "cancelled"
    assert snapshot_store.is_deleted(client_id="client-a", client_resource_id="asset-delete-active")
    assert search.delete_payloads[0]["client_resource_id"] == "asset-delete-active"
    assert search.payloads == []


@pytest.mark.asyncio
async def test_processing_service_keeps_snapshot_when_search_upsert_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "work_dir", str(tmp_path / "work"))
    monkeypatch.setattr(settings, "llm_provider", "mock")

    source_path = tmp_path / "source.png"
    preview_path = tmp_path / "preview.png"
    _write_test_image(source_path)
    _write_test_image(preview_path)

    storage = FakeStorage(
        {
            ("default", "raw/source.png"): source_path,
            ("default", "provided/preview.png"): preview_path,
        },
        tmp_path / "uploaded",
    )
    snapshot_store = ProcessedSnapshotStore(str(tmp_path / "snapshots.db"))
    service = ProcessingService(
        storage=storage,
        search_client=FailingSearchClient(),
        snapshot_store=snapshot_store,
        store=JobStore(),
    )

    manifest = ResourceManifest(
        request_id="req-fail",
        client_resource_id="asset-fail",
        resource_type="single_image",
        source_object=ObjectRef(
                object_key="raw/source.png",
                file_name="source.png",
                file_format="png",
                size=source_path.stat().st_size,
        ),
        source_files=[
            SourceFileRef(
                file_name="source.png",
                file_format="png",
                file_size=source_path.stat().st_size,
                is_primary=True,
            )
        ],
        provided_previews=[PreviewRef(object_key="provided/preview.png")],
        provided_description=ProvidedDescription(
            main_content="可恢复主描述",
            detail_content="可恢复详细描述",
            prompt_version="client-desc-v1",
        ),
    )

    created = await service.create_job(client_id="client-a", manifest=manifest)
    await service.run_job(created.job_id)
    job = await service.get_job(created.job_id, client_id="client-a")

    assert job.state == "failed"
    snapshot = snapshot_store.get(client_id="client-a", client_resource_id="asset-fail")
    assert snapshot is not None
    assert snapshot["search_upsert_state"] == "upsert_failed"
    assert "search is down" in snapshot["last_upsert_error"]
    assert snapshot["snapshot"]["description"]["summary"] == "可恢复主描述"

    service.search_client = FakeSearchClient()
    replayed = await service.replay_snapshot(client_id="client-a", client_resource_id="asset-fail")

    assert replayed.state == "upserted"
    assert replayed.search_resource_id == "res-asset-fail"
    snapshot_after_replay = snapshot_store.get(client_id="client-a", client_resource_id="asset-fail")
    assert snapshot_after_replay["search_upsert_state"] == "upserted"


@pytest.mark.asyncio
async def test_processing_service_delete_merges_search_and_snapshot_object_refs(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "work_dir", str(tmp_path / "work"))
    monkeypatch.setattr(settings, "llm_provider", "mock")

    snapshot_store = ProcessedSnapshotStore(str(tmp_path / "snapshots.db"))
    snapshot_store.save_pending(
        {
            "client_id": "client-a",
            "client_resource_id": "asset-delete",
            "resource_type": "single_image",
            "source_object": {
                "storage_profile_id": "default",
                "object_key": "raw/delete.png",
            },
            "source_files": [],
            "previews": [
                {
                    "storage_profile_id": "default",
                    "object_key": "provided/delete-preview.webp",
                }
            ],
            "description": {"summary": "delete"},
            "processing": {"preview_source": "provided", "description_source": "provided"},
        },
        resource_fingerprint="fingerprint",
    )
    snapshot_store.mark_upserted(
        client_id="client-a",
        client_resource_id="asset-delete",
        search_resource_id="res-asset-delete",
    )

    storage = FakeStorage({}, tmp_path / "uploaded")
    search = FakeSearchClient()
    search.delete_result = {
        "resource_id": "res-asset-delete",
        "state": "deleted",
        "deleted": True,
        "object_refs": [
            {
                "storage_profile_id": "default",
                "object_key": "raw/delete.png",
                "kind": "source_object",
            },
            {
                "storage_profile_id": "default",
                "object_key": "generated/delete-preview.webp",
                "kind": "preview",
                "origin": "generated",
                "renderer": "resource-processing-server",
            },
        ],
    }
    service = ProcessingService(
        storage=storage,
        search_client=search,
        snapshot_store=snapshot_store,
        store=JobStore(),
    )

    result = await service.delete_processed_resource(
        client_id="client-a",
        request=DeleteProcessedResourceIn(client_resource_id="asset-delete"),
    )

    assert result.state == "deleted"
    assert result.search_deleted is True
    assert result.objects_deleted == 1
    assert result.snapshot_deleted is True
    assert snapshot_store.get(client_id="client-a", client_resource_id="asset-delete") is None
    assert search.delete_payloads[0]["client_id"] == "client-a"
    assert search.delete_payloads[0]["client_resource_id"] == "asset-delete"
    assert search.delete_payloads[0]["delete_objects"] is False
    assert {ref["object_key"] for ref in storage.deleted_refs} == {"generated/delete-preview.webp"}


@pytest.mark.asyncio
async def test_processing_service_cleanup_replaced_objects_only_deletes_generated_previews(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "work_dir", str(tmp_path / "work"))
    storage = FakeStorage({}, tmp_path / "uploaded")
    service = ProcessingService(
        storage=storage,
        search_client=FakeSearchClient(),
        snapshot_store=ProcessedSnapshotStore(str(tmp_path / "snapshots.db")),
        store=JobStore(),
    )
    created = await service.create_job(
        client_id="client-a",
        manifest=ResourceManifest(
            client_resource_id="cleanup-test",
            resource_type="single_image",
            source_object=ObjectRef(object_key="raw/new.png"),
            source_files=[SourceFileRef(file_name="new.png")],
            provided_description=ProvidedDescription(main_content="cleanup"),
        ),
    )

    await service._cleanup_replaced_objects(
        created.job_id,
        [
            {"storage_profile_id": "default", "object_key": "raw/old.png", "kind": "source_object"},
            {
                "storage_profile_id": "default",
                "object_key": "provided/old-preview.webp",
                "kind": "preview",
                "origin": "provided",
            },
            {
                "storage_profile_id": "default",
                "object_key": "generated/old-preview.webp",
                "kind": "preview",
                "origin": "generated",
            },
        ],
        [{"storage_profile_id": "default", "object_key": "raw/new.png", "kind": "source_object"}],
    )

    assert {ref["object_key"] for ref in storage.deleted_refs} == {"generated/old-preview.webp"}
