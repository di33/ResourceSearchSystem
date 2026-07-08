from __future__ import annotations

import pytest
from pydantic import ValidationError

from preview_renderer.app.models import PreviewRenderRequest


def test_preview_render_request_requires_caller_supplied_source_object_url():
    with pytest.raises(ValidationError, match="source_object_url"):
        PreviewRenderRequest(
            client_resource_id="asset-1",
            resource_type="single_image",
            source_object={"object_key": "raw/asset.png"},
            source_object_url="",
            source_files=[{"file_name": "asset.png"}],
        )


def test_preview_render_request_accepts_presigned_source_object_url():
    request = PreviewRenderRequest(
        client_resource_id="asset-1",
        resource_type="single_image",
        source_object={"object_key": "raw/asset.png"},
        source_object_url="https://storage.local/raw/asset.png?sign=test",
        source_files=[{"file_name": "asset.png"}],
    )

    assert request.source_object_url.startswith("https://storage.local/")
