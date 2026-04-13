from pathlib import Path

import pytest
from PIL import Image

from ResourceProcessor.preview.crawler_thumbnail_policy import CrawlerThumbnailPolicy
from ResourceProcessor.preview_metadata import FileInfo, ResourceProcessingEntity


def _make_image(path: Path, color: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new("RGB", (96, 96), color=color) as img:
        img.save(path)


@pytest.mark.asyncio
async def test_single_image_preview_generation(tmp_path):
    image_path = tmp_path / "hero.png"
    _make_image(image_path, "red")
    entity = ResourceProcessingEntity(
        resource_id="res-hero",
        resource_type="single_image",
        source_directory=str(tmp_path),
        pack_name="Pack",
        title="Hero",
        resource_path="hero.png",
        content_md5="abc123",
        files=[
            FileInfo(
                file_path=str(image_path),
                file_name="hero.png",
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5="filemd5",
                is_primary=True,
            )
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)
    assert len(previews) == 1
    assert previews[0].mode == "direct"
    assert previews[0].confidence == "high"
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_tileset_generates_contact_sheet_and_gallery(tmp_path):
    files = []
    for idx, color in enumerate(["red", "green", "blue", "yellow"]):
        image_path = tmp_path / f"tile_{idx:02d}.png"
        _make_image(image_path, color)
        files.append(
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5=f"md5-{idx}",
                file_role="tile",
                is_primary=(idx == 0),
            )
        )

    entity = ResourceProcessingEntity(
        resource_id="res-tiles",
        resource_type="tileset",
        source_directory=str(tmp_path),
        pack_name="Pack",
        title="Tiles",
        resource_path="tiles",
        content_md5="tiles-md5",
        member_count=4,
        files=files,
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)
    assert len(previews) == 2
    assert previews[0].strategy.value == "contact_sheet"
    assert previews[0].mode == "composed"
    assert previews[1].role == "gallery"
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_audio_falls_back_to_metadata_card(tmp_path):
    entity = ResourceProcessingEntity(
        resource_id="res-audio",
        resource_type="audio_file",
        source_directory=str(tmp_path),
        pack_name="Pack",
        title="Coin Pickup",
        resource_path="audio/coin.ogg",
        tags=["ui", "coin"],
        content_md5="audio-md5",
        member_count=1,
        missing_files=["audio/coin.ogg"],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)
    assert len(previews) == 1
    assert previews[0].mode == "metadata_only"
    assert previews[0].confidence == "low"
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_pack_generates_collage_preview(tmp_path):
    files = []
    for idx, color in enumerate(["red", "green", "blue", "yellow"]):
        image_path = tmp_path / f"pack_{idx:02d}.png"
        _make_image(image_path, color)
        files.append(
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5=f"pack-md5-{idx}",
                is_primary=(idx == 0),
            )
        )

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path),
        pack_name="UI Pack",
        title="UI Bundle",
        content_md5="pack-md5",
        files=files,
        child_resource_count=4,
        contains_resource_types=["single_image", "tileset"],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)
    assert len(previews) == 1
    assert previews[0].strategy.value == "contact_sheet"
    assert previews[0].mode == "composed"
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_pack_preview_handles_mixed_natural_sort_names(tmp_path):
    files = []
    names = [
        "outline-zoom-reset.svg.png",
        "outline-zoom-2.png",
        "outline-zoom-10.png",
        "outline-zoom.png",
    ]
    for idx, name in enumerate(names):
        image_path = tmp_path / name
        _make_image(image_path, ["red", "green", "blue", "yellow"][idx])
        files.append(
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5=f"mixed-pack-md5-{idx}",
                is_primary=(idx == 0),
            )
        )

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path),
        pack_name="Cursor Pack",
        title="Cursor Icons",
        content_md5="mixed-pack-md5",
        files=files,
        child_resource_count=4,
        contains_resource_types=["single_image"],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)
    assert len(previews) == 1
    assert Path(previews[0].path).is_file()
