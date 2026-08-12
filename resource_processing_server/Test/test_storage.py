from pathlib import Path

from resource_processing_server.ObjectStorageUpload.storage_profiles import StorageProfile
from resource_processing_server.app.config import settings
from resource_processing_server.app.storage import ObjectStorage


class _FakeS3Client:
    def __init__(self):
        self.uploads = []

    def upload_file(self, source, bucket, key):
        self.uploads.append((source, bucket, key))


def test_generated_preview_uses_source_profile_and_allowed_prefix(tmp_path, monkeypatch):
    preview = tmp_path / "primary.webp"
    preview.write_bytes(b"preview")
    profile = StorageProfile(
        profile_id="game-ai-studio-resource3d-1252100362",
        bucket="game-ai-studio-resource3d-1252100362",
        allowed_prefixes=("resource-3d/",),
    )
    client = _FakeS3Client()
    storage = object.__new__(ObjectStorage)
    monkeypatch.setattr(storage, "_profile", lambda _profile_id="": profile)
    monkeypatch.setattr(storage, "_client", lambda _profile_id="": client)
    monkeypatch.setattr(settings, "generated_preview_profile_id", "")
    monkeypatch.setattr(settings, "generated_preview_prefix", "")

    uploaded = storage.upload_preview(
        str(preview),
        client_id="model-editor",
        client_resource_id="chair-001",
        storage_profile_id=profile.profile_id,
    )

    expected_key = "resource-3d/model-editor/previews/chair-001/primary.webp"
    assert uploaded.storage_profile_id == profile.profile_id
    assert uploaded.object_key == expected_key
    assert client.uploads == [(str(preview), profile.bucket, expected_key)]
