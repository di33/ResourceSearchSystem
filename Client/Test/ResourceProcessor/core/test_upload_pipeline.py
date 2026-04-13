from pathlib import Path

from ResourceProcessor.core.upload_pipeline import infer_upload_resource_type, upload_enriched_resources
from ResourceProcessor.preview_metadata import FileInfo, PreviewInfo, PreviewStrategy, ResourceProcessingEntity


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def test_infer_upload_resource_type_prefers_entity_type():
    entity = ResourceProcessingEntity(
        resource_type="tileset",
        source_directory="/tmp",
    )
    assert infer_upload_resource_type(entity) == "tileset"


def test_upload_pipeline_skips_metadata_only_resources(monkeypatch, tmp_path):
    preview_path = tmp_path / "preview.webp"
    preview_path.write_bytes(b"preview")
    resource = ResourceProcessingEntity(
        resource_id="res-meta",
        resource_type="audio_file",
        source_directory=str(tmp_path),
        title="Coin",
        content_md5="abc123",
        previews=[
            PreviewInfo(
                strategy=PreviewStrategy.STATIC,
                path=str(preview_path),
            )
        ],
    )

    posted = []

    def fake_get(url, timeout):
        assert url.endswith("/health")
        return _FakeResponse({"status": "ok"})

    def fake_post(url, **kwargs):
        posted.append(url)
        return _FakeResponse({"success": True, "state": "committed", "resource_id": "res-1", "preview_count": 1})

    monkeypatch.setattr("ResourceProcessor.core.upload_pipeline.requests.get", fake_get)
    monkeypatch.setattr("ResourceProcessor.core.upload_pipeline.requests.post", fake_post)

    summary = upload_enriched_resources(
        [
            {
                "resource": resource,
                "resource_type": "audio_file",
                "description": {"main": "m", "detail": "d", "full": "主体：m\n细节：d"},
            }
        ],
        "http://localhost:8000",
    )

    assert summary.skipped_no_files == 1
    assert summary.success_count == 0
    assert posted == []


def test_upload_pipeline_registers_zip_for_multifile_resource(monkeypatch, tmp_path):
    file_a = tmp_path / "a.png"
    file_b = tmp_path / "b.png"
    file_a.write_bytes(b"a")
    file_b.write_bytes(b"b")

    resource = ResourceProcessingEntity(
        resource_type="tileset",
        source_directory=str(tmp_path),
        title="Tiles",
        content_md5="tiles-md5",
        source_resource_id="src-tiles",
        files=[
            FileInfo(
                file_path=str(file_a),
                file_name="a.png",
                file_size=file_a.stat().st_size,
                file_format="png",
                content_md5="md5-a",
                is_primary=True,
            ),
            FileInfo(
                file_path=str(file_b),
                file_name="b.png",
                file_size=file_b.stat().st_size,
                file_format="png",
                content_md5="md5-b",
            ),
        ],
    )

    register_payloads = []
    upload_files_payloads = []

    def fake_get(url, timeout):
        return _FakeResponse({"status": "ok"})

    def fake_post(url, **kwargs):
        if url.endswith("/register"):
            register_payloads.append(kwargs["json"])
            return _FakeResponse({"resource_id": "res-1", "exists": False, "upload_mode": "direct", "multipart_chunk_size": 0, "state": "registered"})
        if url.endswith("/upload-batch"):
            upload_files_payloads.append(kwargs["files"])
            return _FakeResponse({"success": True, "file_count": 2, "uploaded_bytes": 2})
        if url.endswith("/commit"):
            return _FakeResponse({"state": "committed", "resource_id": "res-1"})
        raise AssertionError(url)

    monkeypatch.setattr("ResourceProcessor.core.upload_pipeline.requests.get", fake_get)
    monkeypatch.setattr("ResourceProcessor.core.upload_pipeline.requests.post", fake_post)

    summary = upload_enriched_resources(
        [
            {
                "resource": resource,
                "resource_type": "tileset",
                "description": {"main": "m", "detail": "d", "full": "主体：m\n细节：d"},
            }
        ],
        "http://localhost:8000",
    )

    assert summary.success_count == 1
    assert register_payloads[0]["source_resource_id"] == "src-tiles"
    assert register_payloads[0]["download_file_name"].endswith(".zip")
    names = [entry[1][0] for entry in upload_files_payloads[0]]
    assert "a.png" in names
    assert "b.png" in names
    assert any(name.endswith(".zip") for name in names)
