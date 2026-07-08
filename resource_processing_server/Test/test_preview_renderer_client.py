from __future__ import annotations

import io
import json
import zipfile

import pytest

from resource_processing_server.app.preview_renderer_client import PreviewRendererClient


def _preview_zip() -> bytes:
    payload = io.BytesIO()
    manifest = {
        "client_resource_id": "asset-remote",
        "preview_count": 1,
        "previews": [
            {
                "role": "primary",
                "file_name": "primary.webp",
                "content_type": "image/webp",
                "width": 64,
                "height": 64,
                "size": 7,
                "checksum": "abc",
                "strategy": "static",
                "origin": "generated",
                "renderer": "preview-renderer",
            }
        ],
    }
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("primary.webp", b"preview")
    return payload.getvalue()


def test_extract_preview_zip_saves_manifest_files(tmp_path):
    client = PreviewRendererClient(base_url="http://renderer")

    previews = client.extract_preview_zip(_preview_zip(), tmp_path)

    assert len(previews) == 1
    assert previews[0].path == tmp_path / "primary.webp"
    assert previews[0].path.read_bytes() == b"preview"
    assert previews[0].role == "primary"
    assert previews[0].width == 64
    assert previews[0].renderer == "preview-renderer"


def test_extract_preview_zip_rejects_missing_manifest(tmp_path):
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("primary.webp", b"preview")

    client = PreviewRendererClient(base_url="http://renderer")

    with pytest.raises(ValueError, match="manifest.json"):
        client.extract_preview_zip(payload.getvalue(), tmp_path)


def test_save_primary_response_uses_metadata_header(tmp_path):
    client = PreviewRendererClient(base_url="http://renderer")

    preview = client.save_primary_response(
        b"primary-bytes",
        tmp_path,
        content_type="image/webp",
        metadata_header=json.dumps({
            "role": "primary",
            "file_name": "primary.webp",
            "width": 32,
            "height": 32,
            "renderer": "preview-renderer",
        }),
    )

    assert preview.path == tmp_path / "primary.webp"
    assert preview.path.read_bytes() == b"primary-bytes"
    assert preview.width == 32
    assert preview.content_type == "image/webp"
