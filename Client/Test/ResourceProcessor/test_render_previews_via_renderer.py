from __future__ import annotations

import io
import json
import zipfile

import requests

from ResourceProcessor.render_previews_via_renderer import (
    _extract_preview_zip,
    _resource_file_prefix,
    _resource_output_dir,
    _save_primary_response,
    render_preview_manifest,
)


class CaptureSession:
    def __init__(self, response):
        self.response = response
        self.payload = None
        self.headers = None
        self.url = ""

    def post(self, url, *, json, headers, timeout):
        self.url = url
        self.payload = json
        self.headers = headers
        return self.response


def _zip_response() -> requests.Response:
    payload = io.BytesIO()
    manifest = {
        "client_resource_id": "asset-client",
        "preview_count": 1,
        "previews": [{"role": "primary", "file_name": "primary.webp"}],
    }
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("primary.webp", b"preview")

    response = requests.Response()
    response.status_code = 200
    response._content = payload.getvalue()
    response.headers["Content-Type"] = "application/zip"
    return response


def test_extract_preview_zip_saves_local_files(tmp_path):
    payload = io.BytesIO()
    manifest = {
        "client_resource_id": "asset-client",
        "preview_count": 1,
        "previews": [
            {
                "role": "primary",
                "file_name": "primary.webp",
                "content_type": "image/webp",
                "renderer": "preview-renderer",
            }
        ],
    }
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("primary.webp", b"preview")

    result = _extract_preview_zip(payload.getvalue(), tmp_path)

    assert result["preview_count"] == 1
    assert result["previews"][0]["path"] == str(tmp_path / "primary.webp")
    assert (tmp_path / "primary.webp").read_bytes() == b"preview"


def test_render_preview_manifest_derives_source_object_from_legacy_file_ref(monkeypatch, tmp_path):
    session = CaptureSession(_zip_response())

    class FakeUploader:
        def __init__(self, *, storage_profile_id=None):
            assert storage_profile_id == "profile"

        def generate_download_url(self, object_key, *, expires=900):
            assert object_key == "resource-crawler/files/asset-client/source.png"
            assert expires == 900
            return "https://storage.local/generated-source-url"

    monkeypatch.setattr("ObjectStorageUpload.uploader.ObjectStorageUploader", FakeUploader)

    manifest = {
        "client_resource_id": "asset-client",
        "resource_type": "single_image",
        "source_object": None,
        "source_files": [
            {
                "role": "main",
                "storage_profile_id": "profile",
                "object_key": "resource-crawler/files/asset-client/source.png",
                "file_name": "source.png",
                "file_format": "png",
                "size": 107,
                "checksum": "abc",
                "etag": "etag-1",
                "is_primary": True,
            }
        ],
    }

    result = render_preview_manifest(
        manifest,
        preview_renderer="http://renderer",
        client_id="resource-crawler",
        output_root=tmp_path,
        session=session,
    )

    assert session.url == "http://renderer/previews/render"
    assert session.headers["X-Client-Id"] == "resource-crawler"
    assert session.payload["source_object"] == {
        "storage_profile_id": "profile",
        "object_key": "resource-crawler/files/asset-client/source.png",
        "file_name": "source.png",
        "file_format": "png",
        "size": 107,
        "checksum": "abc",
        "etag": "etag-1",
        "is_primary": True,
    }
    assert session.payload["source_object_url"] == "https://storage.local/generated-source-url"
    assert result["preview_count"] == 1
    assert result["previews"][0]["path"] == str(tmp_path / "single_image" / "asset-client_primary.webp")


def test_extract_preview_zip_prefixes_local_file_names(tmp_path):
    payload = io.BytesIO()
    manifest = {
        "client_resource_id": "asset-client",
        "preview_count": 2,
        "previews": [
            {"role": "primary", "file_name": "primary.webp"},
            {"role": "gallery", "file_name": "gallery-001.webp"},
        ],
    }
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("primary.webp", b"primary")
        archive.writestr("gallery-001.webp", b"gallery")

    result = _extract_preview_zip(payload.getvalue(), tmp_path, file_prefix="asset_001")

    assert [item["file_name"] for item in result["previews"]] == [
        "asset_001_primary.webp",
        "asset_001_gallery-001.webp",
    ]
    assert (tmp_path / "asset_001_primary.webp").read_bytes() == b"primary"
    assert (tmp_path / "asset_001_gallery-001.webp").read_bytes() == b"gallery"


def test_save_primary_response_saves_local_file(tmp_path):
    response = requests.Response()
    response.status_code = 200
    response._content = b"primary"
    response.headers["Content-Type"] = "image/webp"
    response.headers["X-Preview-Metadata"] = json.dumps({
        "role": "primary",
        "file_name": "primary.webp",
        "renderer": "preview-renderer",
    })

    result = _save_primary_response(response, tmp_path)

    assert result["preview_count"] == 1
    assert result["previews"][0]["path"] == str(tmp_path / "primary.webp")
    assert (tmp_path / "primary.webp").read_bytes() == b"primary"


def test_resource_output_dir_groups_by_resource_type(tmp_path):
    output_dir = _resource_output_dir(
        tmp_path,
        {
            "client_resource_id": "asset:001",
            "resource_type": "single_image",
        },
    )

    assert output_dir == tmp_path / "single_image"
    assert _resource_file_prefix({"client_resource_id": "asset:001"}) == "asset_001"
