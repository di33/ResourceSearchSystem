from __future__ import annotations

import pytest

from ResourceProcessor.preview_metadata import PreviewInfo, PreviewStrategy, ResourceProcessingEntity
from resource_contracts.resource_types import SPINE_SKELETON_RESOURCE_TYPE
from resource_processing_server.app import adapters
from resource_processing_server.app.config import settings


@pytest.mark.asyncio
async def test_generate_previews_uses_spine_runtime_renderer(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "preview_max_size", 640)
    calls = {}

    async def fake_spine_runtime(entity, previews_dir, *, max_size, browser=""):
        calls["entity"] = entity
        calls["previews_dir"] = previews_dir
        calls["max_size"] = max_size
        calls["browser"] = browser
        return [
            PreviewInfo(
                strategy=PreviewStrategy.CONTACT_SHEET,
                path=str(tmp_path / "runtime.webp"),
                mode="spine_runtime_overview",
                renderer="spine-webgl-3.8-playwright",
            )
        ]

    monkeypatch.setattr(
        "spine_preview.runtime_preview.generate_spine_runtime_previews",
        fake_spine_runtime,
    )

    entity = ResourceProcessingEntity(
        resource_type=SPINE_SKELETON_RESOURCE_TYPE,
        source_directory=str(tmp_path),
        content_md5="spine-md5",
    )

    previews = await adapters.generate_previews(entity, tmp_path / "previews")

    assert previews[0].renderer == "spine-webgl-3.8-playwright"
    assert previews[0].mode == "spine_runtime_overview"
    assert calls == {
        "entity": entity,
        "previews_dir": tmp_path / "previews",
        "max_size": 640,
        "browser": "",
    }


@pytest.mark.asyncio
async def test_generate_previews_falls_back_when_spine_runtime_fails(tmp_path, monkeypatch):
    async def fail_spine_runtime(*args, **kwargs):
        raise RuntimeError("missing texture page")

    class FakeCrawlerThumbnailPolicy:
        def __init__(self, output_dir, *, max_size):
            self.output_dir = output_dir
            self.max_size = max_size

        async def generate_previews(self, entity):
            return [
                PreviewInfo(
                    strategy=PreviewStrategy.CONTACT_SHEET,
                    path=str(tmp_path / "fallback.webp"),
                    mode="atlas_regions",
                    renderer="crawler-policy",
                )
            ]

    monkeypatch.setattr(
        "spine_preview.runtime_preview.generate_spine_runtime_previews",
        fail_spine_runtime,
    )
    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy.CrawlerThumbnailPolicy",
        FakeCrawlerThumbnailPolicy,
    )

    entity = ResourceProcessingEntity(
        resource_type=SPINE_SKELETON_RESOURCE_TYPE,
        source_directory=str(tmp_path),
        content_md5="spine-md5",
    )

    previews = await adapters.generate_previews(entity, tmp_path / "previews")

    assert previews[0].renderer == "spine-runtime-fallback"
    assert previews[0].mode == "runtime_fallback_atlas_regions"
    assert previews[0].confidence == "low"
    assert previews[0].fail_reason == "missing texture page"
