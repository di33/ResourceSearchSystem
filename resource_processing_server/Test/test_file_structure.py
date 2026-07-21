from __future__ import annotations

import zipfile

import pytest
from PIL import Image

from resource_contracts.file_structure import scan_source_file_structure
from resource_processing_server.app.models import Description, ObjectRef, PreviewRef, ResourceManifest
from resource_processing_server.app.processor import JobStore, ProcessingService
from resource_processing_server.app.snapshots import ProcessedSnapshotStore
from resource_processing_server.Test.test_processing_service import FakeSearchClient, FakeStorage


def test_scan_zip_builds_structure_without_extracting(tmp_path):
    archive_path = tmp_path / "source.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("tiles/grass.png", b"png")
        archive.writestr("docs/readme.txt", b"hello")

    structure = scan_source_file_structure(archive_path, checksum="zip-md5")

    assert structure["source"] == "processor"
    assert structure["source_object_checksum"] == "zip-md5"
    assert structure["entry_count"] == 2
    assert [entry["path"] for entry in structure["entries"]] == [
        "tiles/grass.png",
        "docs/readme.txt",
    ]
    assert not (tmp_path / "tiles").exists()
    assert not (tmp_path / "docs").exists()


def test_legacy_source_files_are_upgraded_but_not_serialized():
    manifest = ResourceManifest.model_validate({
        "client_resource_id": "asset-1",
        "resource_type": "single_image",
        "source_object": {"object_key": "raw/source.png", "checksum": "abc"},
        "source_files": [{
            "file_name": "source.png",
            "file_format": "png",
            "file_size": 12,
            "checksum": "abc",
            "is_primary": True,
        }],
    })

    dumped = manifest.model_dump(mode="json")
    assert "source_files" not in dumped
    assert dumped["file_structure"]["entry_count"] == 1
    assert dumped["file_structure"]["entries"][0]["path"] == "source.png"


def test_structure_checksum_must_match_source_object():
    with pytest.raises(ValueError, match="does not match"):
        ResourceManifest.model_validate({
            "client_resource_id": "asset-1",
            "resource_type": "single_image",
            "source_object": {"object_key": "raw/source.png", "checksum": "actual"},
            "file_structure": {
                "source": "client",
                "source_object_checksum": "other",
                "entries": [{"path": "source.png", "name": "source.png"}],
            },
        })


@pytest.mark.asyncio
async def test_processor_generates_missing_structure_without_extracting_when_preview_exists(tmp_path, monkeypatch):
    source_zip = tmp_path / "source.zip"
    with zipfile.ZipFile(source_zip, "w") as archive:
        archive.writestr("tiles/a.png", b"a")
        archive.writestr("tiles/b.png", b"b")
    preview = tmp_path / "preview.png"
    image = Image.new("RGB", (128, 128), (20, 80, 160))
    for x in range(24, 104):
        for y in range(24, 104):
            image.putpixel((x, y), (180, 90, 30))
    image.save(preview)
    storage = FakeStorage(
        {
            ("default", "raw/source.zip"): source_zip,
            ("default", "previews/primary.png"): preview,
        },
        tmp_path / "uploaded",
    )
    search = FakeSearchClient()
    service = ProcessingService(
        storage=storage,
        search_client=search,
        snapshot_store=ProcessedSnapshotStore(str(tmp_path / "snapshots.db")),
        store=JobStore(),
    )
    manifest = ResourceManifest(
        client_resource_id="tiles-1",
        resource_type="tileset",
        source_object=ObjectRef(object_key="raw/source.zip", file_name="source.zip", checksum="zip-md5"),
        previews=[PreviewRef(object_key="previews/primary.png", origin="provided")],
        description=Description(summary="tiles"),
    )

    created = await service.create_job(client_id="client-a", manifest=manifest)
    await service.run_job(created.job_id)
    job = await service.get_job(created.job_id, client_id="client-a")

    assert job is not None and job.state == "completed"
    assert storage.downloaded_keys.count("raw/source.zip") == 1
    payload = search.payloads[0]
    assert "source_files" not in payload
    assert payload["file_structure"]["source"] == "processor"
    assert payload["file_structure"]["entry_count"] == 2


@pytest.mark.asyncio
async def test_large_provided_structure_does_not_use_extract_member_limit(tmp_path):
    preview = tmp_path / "preview.png"
    image = Image.new("RGB", (128, 128), (30, 70, 120))
    for x in range(20, 108):
        for y in range(20, 108):
            image.putpixel((x, y), (190, 110, 40))
    image.save(preview)
    storage = FakeStorage(
        {("default", "previews/primary.png"): preview},
        tmp_path / "uploaded",
    )
    search = FakeSearchClient()
    service = ProcessingService(
        storage=storage,
        search_client=search,
        snapshot_store=ProcessedSnapshotStore(str(tmp_path / "snapshots.db")),
        store=JobStore(),
    )
    entries = [
        {"path": f"tiles/{index:04d}.png", "name": f"{index:04d}.png", "size": index + 1}
        for index in range(1036)
    ]
    manifest = ResourceManifest.model_validate({
        "client_resource_id": "large-tiles",
        "resource_type": "tileset",
        "source_object": {"object_key": "raw/source.zip", "checksum": "zip-md5"},
        "file_structure": {
            "source": "client",
            "source_object_checksum": "zip-md5",
            "entries": entries,
        },
        "previews": [{"object_key": "previews/primary.png", "origin": "provided"}],
        "description": {"summary": "large tiles"},
    })

    created = await service.create_job(client_id="client-a", manifest=manifest)
    await service.run_job(created.job_id)
    job = await service.get_job(created.job_id, client_id="client-a")

    assert job is not None and job.state == "completed"
    assert "raw/source.zip" not in storage.downloaded_keys
    assert search.payloads[0]["file_structure"]["entry_count"] == 1036
