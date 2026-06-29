from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from ResourceProcessor.preview.crawler_thumbnail_policy import (
    CrawlerThumbnailPolicy,
    _open_for_sheet,
    _open_pack_collage_image,
    _pack_child_preview_records,
    _pack_preview_pages,
    _render_dense_atlas_image,
    _save_contact_sheet,
    _save_pack_collage,
    _save_tileset_sheet,
    _save_metadata_card,
    _tileset_sheet_image_paths,
)
from ResourceProcessor.preview_metadata import FileInfo, ResourceProcessingEntity


def _make_image(path: Path, color: str, size: tuple[int, int] = (96, 96)):
    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new("RGB", size, color=color) as img:
        img.save(path)


def _make_child_preview(path: Path, color: str, size: tuple[int, int] = (180, 120)):
    path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new("RGB", size, color=color) as img:
        img.paste(Image.new("RGB", (54, 42), color="white"), (24, 18))
        img.paste(Image.new("RGB", (38, 34), color="black"), (112, 64))
        img.save(path)


def _make_unique_icon_preview(path: Path, index: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    color = ((index * 37) % 256, (index * 67) % 256, (index * 97) % 256)
    with Image.new("RGB", (48, 48), color=color) as img:
        draw = ImageDraw.Draw(img)
        draw.rectangle((6, 6, 28, 28), fill="white")
        draw.rectangle((18, 18, 42, 42), fill=(index % 256, 0, 0))
        draw.point((index % 48, (index // 48) % 48), fill="black")
        img.save(path)


def test_metadata_card_long_text_stays_inside_frame(tmp_path, monkeypatch):
    drawn_boxes = []
    original_text = ImageDraw.ImageDraw.text

    def record_text(self, xy, text, *args, **kwargs):
        bbox = self.textbbox(xy, text, font=kwargs.get("font"))
        drawn_boxes.append(bbox)
        return original_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)

    _save_metadata_card(
        tmp_path / "metadata.webp",
        "Audio: impactSoft medium 001.ogg",
        "Tags impact foley Category Audio Files 130× License Creative",
        [
            "Pack: Impact Sounds",
            "Type: audio_file",
            "Path: Audio/impactSoft_medium_001.ogg",
            "Tags: Audio, impact, foley",
            "Files: 1",
        ],
    )

    assert drawn_boxes
    assert all(box[0] >= 44 for box in drawn_boxes)
    assert all(box[2] <= 468 for box in drawn_boxes)
    assert all(box[3] <= 468 for box in drawn_boxes)


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
    assert Path(previews[0].path).parent.name == "single_image"


@pytest.mark.asyncio
async def test_single_image_tiny_png_is_upscaled_for_visibility(tmp_path):
    image_path = tmp_path / "flower.png"
    image = Image.new("RGBA", (6, 4), (0, 0, 0, 0))
    image.putpixel((2, 1), (80, 180, 80, 255))
    image.putpixel((3, 1), (100, 210, 100, 255))
    image.putpixel((2, 2), (70, 160, 70, 255))
    image.save(image_path)

    entity = ResourceProcessingEntity(
        resource_id="res-flower",
        resource_type="single_image",
        source_directory=str(tmp_path),
        pack_name="Pack",
        title="Tiny Flower",
        resource_path="flower.png",
        content_md5="tiny-flower-md5",
        files=[
            FileInfo(
                file_path=str(image_path),
                file_name="flower.png",
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5="tiny-file-md5",
                is_primary=True,
            )
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    with Image.open(previews[0].path) as preview:
        assert max(preview.size) >= 128


@pytest.mark.asyncio
async def test_single_image_preserves_transparent_canvas(tmp_path):
    image_path = tmp_path / "sheet.png"
    image = Image.new("RGBA", (128, 64), (0, 0, 0, 0))
    image.alpha_composite(Image.new("RGBA", (32, 32), (240, 40, 40, 255)), (0, 0))
    image.save(image_path)
    entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(tmp_path),
        title="Transparent Sheet",
        resource_path="sheet.png",
        content_md5="transparent-canvas-md5",
        files=[
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5="transparent-canvas-file-md5",
                is_primary=True,
            )
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    with Image.open(previews[0].path) as preview:
        assert preview.size == (128, 64)
        assert "A" in preview.getbands()
        alpha = preview.getchannel("A")
        assert alpha.getpixel((120, 56)) < 16
        assert alpha.getpixel((8, 8)) > 220


@pytest.mark.asyncio
async def test_single_image_all_black_source_preview_is_allowed(tmp_path):
    image_path = tmp_path / "pixel-black.png"
    Image.new("RGB", (1, 1), (0, 0, 0)).save(image_path)
    entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(tmp_path),
        title="Black Pixel",
        resource_path="pixel-black.png",
        content_md5="solid-black-md5",
        files=[
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5="solid-black-file-md5",
                is_primary=True,
            )
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert previews[0].path
    assert previews[0].fail_reason is None
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_single_image_all_transparent_source_preview_is_allowed(tmp_path):
    image_path = tmp_path / "pixel-transparent.png"
    Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(image_path)
    entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(tmp_path),
        title="Transparent Pixel",
        resource_path="pixel-transparent.png",
        content_md5="solid-transparent-md5",
        files=[
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5="solid-transparent-file-md5",
                is_primary=True,
            )
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert previews[0].path
    assert previews[0].fail_reason is None
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_single_image_black_preview_still_fails_for_non_solid_source(tmp_path, monkeypatch):
    image_path = tmp_path / "hero.png"
    _make_image(image_path, "red", size=(128, 128))

    def save_black_preview(_image_path: str, output_path: Path, _size: int = 512):
        Image.new("RGB", (128, 128), (0, 0, 0)).save(output_path, format="WEBP")

    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy._save_existing_raster_preview",
        save_black_preview,
    )
    entity = ResourceProcessingEntity(
        resource_type="single_image",
        source_directory=str(tmp_path),
        title="Hero",
        resource_path="hero.png",
        content_md5="bad-black-preview-md5",
        files=[
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5="hero-file-md5",
                is_primary=True,
            )
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert previews[0].path is None
    assert "black" in previews[0].fail_reason.lower()


@pytest.mark.asyncio
async def test_tileset_generates_primary_contact_sheet(tmp_path):
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
    assert len(previews) == 1
    assert previews[0].strategy.value == "contact_sheet"
    assert previews[0].mode == "packed_tilesheet"
    assert Path(previews[0].path).is_file()


def test_contact_sheet_uses_dense_atlas_layout(tmp_path, monkeypatch):
    calls = []

    def fake_dense_atlas(images, output_path: Path, size: int = 512, **kwargs):
        calls.append((len(images), kwargs))
        Image.new("RGB", (size, size), "white").save(output_path, format="WEBP")

    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy._save_dense_atlas_preview",
        fake_dense_atlas,
    )
    paths = []
    for idx, size in enumerate([(300, 80), (24, 24), (36, 18)]):
        path = tmp_path / f"item_{idx}.png"
        _make_image(path, "red", size=size)
        paths.append(str(path))

    output_path = tmp_path / "contact.webp"
    _save_contact_sheet(paths, output_path)

    assert output_path.is_file()
    assert calls
    assert calls[0][0] == len(paths)
    assert calls[0][1]["background"] == (246, 247, 249)


def test_contact_sheet_preserves_alpha_until_uniform_background(tmp_path):
    paths = []
    for idx, (hidden_rgb, visible_rgb) in enumerate(
        [
            ((12, 10, 8), (240, 40, 40)),
            ((53, 20, 36), (40, 220, 90)),
        ]
    ):
        path = tmp_path / f"transparent_{idx}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGBA", (64, 64), (*hidden_rgb, 0))
        draw = ImageDraw.Draw(image)
        draw.rectangle((18, 16, 45, 48), fill=(*visible_rgb, 255))
        image.save(path)
        paths.append(str(path))

    opened = [_open_for_sheet(path) for path in paths]
    try:
        assert all(image.mode == "RGBA" for image in opened)
        assert all(image.getchannel("A").getpixel((0, 0)) < 8 for image in opened)

        atlas = _render_dense_atlas_image(opened, 256, background=(246, 247, 249))
        pixels = list(atlas.convert("RGB").getdata())
        hidden_dark_pixels = sum(1 for r, g, b in pixels if r < 80 and g < 60 and b < 60)
        assert hidden_dark_pixels == 0
    finally:
        for image in opened:
            image.close()


def test_tileset_mixed_sizes_uses_dense_atlas(tmp_path, monkeypatch):
    calls = []

    def fake_dense_atlas(images, output_path: Path, size: int = 512, **kwargs):
        calls.append((len(images), kwargs))
        Image.new("RGB", (size, size), "white").save(output_path, format="WEBP")

    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy._save_dense_atlas_preview",
        fake_dense_atlas,
    )
    paths = []
    for idx, size in enumerate([(304, 176), (16, 16), (16, 16), (16, 16)]):
        path = tmp_path / f"tile_{idx}.png"
        _make_image(path, "red", size=size)
        paths.append(str(path))

    output_path = tmp_path / "tileset.webp"
    _save_tileset_sheet(paths, output_path, use_all=True)

    assert output_path.is_file()
    assert calls
    assert calls[0][0] == len(paths)


def test_tileset_same_size_keeps_regular_sheet(tmp_path, monkeypatch):
    def fail_dense_atlas(*args, **kwargs):
        raise AssertionError("same-size tileset should not use dense atlas")

    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy._save_dense_atlas_preview",
        fail_dense_atlas,
    )
    paths = []
    for idx in range(4):
        path = tmp_path / f"tile_{idx}.png"
        _make_image(path, "red", size=(16, 16))
        paths.append(str(path))

    output_path = tmp_path / "tileset.webp"
    _save_tileset_sheet(paths, output_path, use_all=True)

    assert output_path.is_file()


@pytest.mark.asyncio
async def test_empty_tiled_map_uses_tsx_tileset_sheet(tmp_path):
    map_dir = tmp_path / "Map"
    tiles_dir = tmp_path / "Tiles"
    map_dir.mkdir()
    tiles_dir.mkdir()

    tile_paths = []
    for name, color in [("grass.png", "red"), ("stone.png", "blue")]:
        image_path = tiles_dir / name
        _make_image(image_path, color, size=(16, 16))
        tile_paths.append(image_path)

    tsx_path = map_dir / "map_tiles.tsx"
    tsx_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tileset version="1.10" tiledversion="1.10.2" name="Map Tiles" tilewidth="16" tileheight="16" tilecount="2" columns="0">
 <tile id="0"><image width="16" height="16" source="../Tiles/grass.png"/></tile>
 <tile id="1"><image width="16" height="16" source="../Tiles/stone.png"/></tile>
</tileset>
""",
        encoding="utf-8",
    )
    tmx_path = map_dir / "empty.tmx"
    tmx_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<map version="1.10" tiledversion="1.10.2" orientation="orthogonal" renderorder="right-down" width="2" height="2" tilewidth="16" tileheight="16" infinite="0">
 <tileset firstgid="1" source="map_tiles.tsx"/>
 <layer id="1" name="Layer" width="2" height="2">
  <data encoding="csv">0,0,0,0</data>
 </layer>
</map>
""",
        encoding="utf-8",
    )

    files = [
        FileInfo(
            file_path=str(tmx_path),
            file_name=tmx_path.name,
            file_size=tmx_path.stat().st_size,
            file_format="tmx",
            content_md5="empty-tmx-file-md5",
            is_primary=True,
        ),
        FileInfo(
            file_path=str(tsx_path),
            file_name=tsx_path.name,
            file_size=tsx_path.stat().st_size,
            file_format="tsx",
            content_md5="tsx-file-md5",
            file_role="attachment",
        ),
    ]
    files.extend(
        FileInfo(
            file_path=str(path),
            file_name=path.name,
            file_size=path.stat().st_size,
            file_format="png",
            content_md5=f"{path.stem}-md5",
            file_role="tile",
        )
        for path in tile_paths
    )
    entity = ResourceProcessingEntity(
        resource_type="tiled_map",
        source_directory=str(tmp_path),
        pack_name="Pack",
        title="Empty Map",
        resource_path="Map/empty.tmx",
        content_md5="empty-map-md5",
        files=files,
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].strategy.value == "contact_sheet"
    assert previews[0].mode == "empty_map_tileset"
    with Image.open(previews[0].path) as preview:
        extrema = preview.convert("RGB").getextrema()
    assert min(channel[0] for channel in extrema) < 80
    assert max(channel[1] for channel in extrema) > 170


@pytest.mark.asyncio
async def test_tileset_prefers_nearby_tilemap_overview(tmp_path):
    tile_dir = tmp_path / "Dot Matrix" / "Tiles"
    overview_path = tmp_path / "Dot Matrix" / "Tilemap" / "tilemap.png"
    files = []
    for idx, color in enumerate(["red", "green", "blue", "yellow"]):
        image_path = tile_dir / f"tile_{idx:04d}.png"
        _make_image(image_path, color, size=(16, 16))
        files.append(
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5=f"tilemap-md5-{idx}",
                file_role="tile",
                is_primary=(idx == 0),
            )
        )
    with Image.new("RGB", (64, 16), color="black") as overview:
        for idx, color in enumerate(["red", "green", "blue", "yellow"]):
            with Image.new("RGB", (16, 16), color=color) as tile:
                overview.paste(tile, (idx * 16, 0))
        overview_path.parent.mkdir(parents=True, exist_ok=True)
        overview.save(overview_path)

    entity = ResourceProcessingEntity(
        resource_type="tileset",
        source_directory=str(tile_dir),
        pack_name="Monochrome Pirates",
        title="Dot Matrix Tiles",
        content_md5="tilemap-overview-md5",
        member_count=4,
        files=files,
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)
    assert len(previews) == 1
    assert previews[0].strategy.value == "static"
    assert previews[0].mode == "verified_overview"
    assert previews[0].width > previews[0].height
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_tileset_prefers_same_directory_tileset_overview(tmp_path):
    tile_dir = tmp_path / "Dungeon" / "1 Tiles"
    files = []
    colors = ["red", "green", "blue", "yellow"]
    for idx, color in enumerate(colors):
        image_path = tile_dir / f"Tile_{idx:02d}.png"
        _make_image(image_path, color, size=(16, 16))
        files.append(
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5=f"same-dir-tile-md5-{idx}",
                file_role="tile",
                is_primary=(idx == 0),
            )
        )

    overview_path = tile_dir / "Tileset.png"
    with Image.new("RGB", (64, 16), color="white") as overview:
        for idx, color in enumerate(colors):
            with Image.new("RGB", (16, 16), color=color) as tile:
                overview.paste(tile, (idx * 16, 0))
        overview.save(overview_path)
    files.append(
        FileInfo(
            file_path=str(overview_path),
            file_name=overview_path.name,
            file_size=overview_path.stat().st_size,
            file_format="png",
            content_md5="same-dir-overview-md5",
            file_role="tile",
        )
    )

    entity = ResourceProcessingEntity(
        resource_type="tileset",
        source_directory=str(tile_dir),
        pack_name="Dungeon",
        title="1 Tiles",
        content_md5="same-dir-tileset-md5",
        member_count=len(files),
        files=files,
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].strategy.value == "static"
    assert previews[0].mode == "verified_overview"
    assert previews[0].width > previews[0].height
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_tileset_fallback_ignores_overview_sized_image(tmp_path):
    tile_dir = tmp_path / "Dungeon" / "1 Tiles"
    files = []
    for idx, color in enumerate(["red", "green", "blue", "yellow", "purple", "orange", "cyan", "magenta", "navy"]):
        image_path = tile_dir / f"Tile_{idx:02d}.png"
        _make_image(image_path, color, size=(16, 16))
        files.append(
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5=f"fallback-tile-md5-{idx}",
                file_role="tile",
                is_primary=(idx == 0),
            )
        )

    overview_path = tile_dir / "Tileset.png"
    _make_image(overview_path, "white", size=(304, 176))
    files.append(
        FileInfo(
            file_path=str(overview_path),
            file_name=overview_path.name,
            file_size=overview_path.stat().st_size,
            file_format="png",
            content_md5="fallback-overview-md5",
            file_role="tile",
        )
    )

    image_paths = [file.file_path for file in files]
    sheet_paths = _tileset_sheet_image_paths(image_paths)
    assert str(overview_path) not in sheet_paths
    assert len(sheet_paths) == 9

    entity = ResourceProcessingEntity(
        resource_type="tileset",
        source_directory=str(tile_dir),
        pack_name="Dungeon",
        title="1 Tiles",
        content_md5="fallback-tileset-md5",
        member_count=len(files),
        files=files,
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].strategy.value == "contact_sheet"
    assert previews[0].mode == "packed_tilesheet"
    assert previews[0].width == 512
    assert previews[0].height == 512
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
async def test_atlas_resolves_missing_declared_image_to_sibling_sheet(tmp_path):
    xml_path = tmp_path / "spritesheet.xml"
    sheet_path = tmp_path / "spritesheet.png"
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<TextureAtlas imagePath="sprites.png">
  <SubTexture name="a.png" x="0" y="0" width="70" height="70"/>
  <SubTexture name="b.png" x="70" y="0" width="70" height="70"/>
</TextureAtlas>
""",
        encoding="utf-8",
    )
    with Image.new("RGBA", (140, 100), (0, 0, 0, 0)) as sheet:
        sheet.alpha_composite(Image.new("RGBA", (70, 70), (240, 90, 80, 255)), (0, 0))
        sheet.alpha_composite(Image.new("RGBA", (70, 70), (80, 160, 240, 255)), (70, 0))
        sheet.save(sheet_path)

    entity = ResourceProcessingEntity(
        resource_type="atlas",
        source_directory=str(tmp_path),
        pack_name="Mushrooms",
        title="spritesheet.xml",
        content_md5="atlas-sibling-md5",
        files=[
            FileInfo(
                file_path=str(xml_path),
                file_name="spritesheet.xml",
                file_size=xml_path.stat().st_size,
                file_format="xml",
                content_md5="atlas-xml-md5",
                file_role="attachment",
                is_primary=True,
            )
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].strategy.value == "contact_sheet"
    assert previews[0].width == 140
    assert previews[0].height == 100
    assert Path(previews[0].path).is_file()
    with Image.open(previews[0].path) as preview:
        assert "A" in preview.getbands()
        assert preview.getchannel("A").getpixel((120, 90)) < 16


@pytest.mark.asyncio
async def test_atlas_merges_multiple_declared_images(tmp_path):
    xml_a = tmp_path / "atlas_a.xml"
    xml_b = tmp_path / "atlas_b.xml"
    image_a = tmp_path / "atlas_a.png"
    image_b = tmp_path / "atlas_b.png"
    _make_image(image_a, "red", size=(256, 256))
    _make_image(image_b, "blue", size=(256, 256))
    xml_a.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<TextureAtlas imagePath="atlas_a.png">
  <SubTexture name="a.png" x="0" y="0" width="256" height="256"/>
</TextureAtlas>
""",
        encoding="utf-8",
    )
    xml_b.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<TextureAtlas imagePath="atlas_b.png">
  <SubTexture name="b.png" x="0" y="0" width="256" height="256"/>
</TextureAtlas>
""",
        encoding="utf-8",
    )

    entity = ResourceProcessingEntity(
        resource_type="atlas",
        source_directory=str(tmp_path),
        pack_name="Multi Atlas",
        title="multi atlas",
        content_md5="atlas-multi-md5",
        files=[
            FileInfo(
                file_path=str(xml_a),
                file_name=xml_a.name,
                file_size=xml_a.stat().st_size,
                file_format="xml",
                content_md5="atlas-a-xml-md5",
                file_role="attachment",
            ),
            FileInfo(
                file_path=str(xml_b),
                file_name=xml_b.name,
                file_size=xml_b.stat().st_size,
                file_format="xml",
                content_md5="atlas-b-xml-md5",
                file_role="attachment",
            ),
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].strategy.value == "contact_sheet"
    assert Path(previews[0].path).is_file()
    with Image.open(previews[0].path) as preview:
        pixels = list(preview.convert("RGB").getdata())
    red_pixels = sum(1 for r, g, b in pixels if r > 180 and g < 80 and b < 80)
    blue_pixels = sum(1 for r, g, b in pixels if b > 120 and r < 80 and g < 80)
    assert red_pixels > 1000
    assert blue_pixels > 1000


@pytest.mark.asyncio
async def test_atlas_all_black_source_preview_is_allowed(tmp_path):
    xml_path = tmp_path / "sheet_black.xml"
    sheet_path = tmp_path / "sheet_black.png"
    Image.new("RGB", (128, 128), (0, 0, 0)).save(sheet_path)
    xml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<TextureAtlas imagePath="sheet_black.png">
  <SubTexture name="icon.png" x="0" y="0" width="128" height="128"/>
</TextureAtlas>
""",
        encoding="utf-8",
    )
    entity = ResourceProcessingEntity(
        resource_type="atlas",
        source_directory=str(tmp_path),
        pack_name="Black Icons",
        title="sheet_black.xml",
        content_md5="atlas-black-md5",
        files=[
            FileInfo(
                file_path=str(xml_path),
                file_name=xml_path.name,
                file_size=xml_path.stat().st_size,
                file_format="xml",
                content_md5="atlas-black-xml-md5",
                file_role="attachment",
                is_primary=True,
            )
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert previews[0].path
    assert previews[0].fail_reason is None
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_atlas_region_fallback_includes_all_regions(tmp_path, monkeypatch):
    calls = []

    def fake_dense_atlas(images, output_path: Path, size: int = 512, **kwargs):
        calls.append(len(images))
        image = Image.new("RGB", (size, size), "white")
        image.paste(Image.new("RGB", (size // 2, size // 2), "red"), (0, 0))
        image.paste(Image.new("RGB", (size // 3, size // 3), "blue"), (size // 2, size // 2))
        image.save(output_path, format="WEBP")

    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy._save_dense_atlas_preview",
        fake_dense_atlas,
    )

    xml_path = tmp_path / "regions.xml"
    sheet_path = tmp_path / "sheet.png"
    region_count = 70
    with Image.new("RGBA", (region_count * 10, 10), (0, 0, 0, 0)) as sheet:
        for idx in range(region_count):
            sheet.alpha_composite(Image.new("RGBA", (8, 8), ((idx * 13) % 255, 90, 220, 255)), (idx * 10 + 1, 1))
        sheet.save(sheet_path)
    xml_path.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<TextureAtlas>\n"
        + "\n".join(
            f'  <SubTexture name="sprite_{idx}.png" x="{idx * 10}" y="0" width="10" height="10"/>'
            for idx in range(region_count)
        )
        + "\n</TextureAtlas>\n",
        encoding="utf-8",
    )

    entity = ResourceProcessingEntity(
        resource_type="atlas",
        source_directory=str(tmp_path),
        pack_name="Region Atlas",
        title="regions.xml",
        content_md5="atlas-region-all-md5",
        files=[
            FileInfo(
                file_path=str(xml_path),
                file_name=xml_path.name,
                file_size=xml_path.stat().st_size,
                file_format="xml",
                content_md5="atlas-region-xml-md5",
                file_role="attachment",
            ),
            FileInfo(
                file_path=str(sheet_path),
                file_name=sheet_path.name,
                file_size=sheet_path.stat().st_size,
                file_format="png",
                content_md5="atlas-region-sheet-md5",
                file_role="main",
            ),
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert Path(previews[0].path).is_file()
    assert calls == [region_count]


@pytest.mark.asyncio
async def test_atlas_without_xml_uses_companion_raster(tmp_path):
    aseprite_path = tmp_path / "Explosion.aseprite"
    raster_path = tmp_path / "Explosion.png"
    aseprite_path.write_bytes(b"dummy-aseprite")
    _make_image(raster_path, "red", size=(160, 80))
    entity = ResourceProcessingEntity(
        resource_type="atlas",
        source_directory=str(tmp_path),
        pack_name="Sprite Pack",
        title="Explosion.aseprite",
        content_md5="atlas-raster-md5",
        files=[
            FileInfo(
                file_path=str(aseprite_path),
                file_name=aseprite_path.name,
                file_size=aseprite_path.stat().st_size,
                file_format="aseprite",
                content_md5="aseprite-md5",
                is_primary=True,
            ),
            FileInfo(
                file_path=str(raster_path),
                file_name=raster_path.name,
                file_size=raster_path.stat().st_size,
                file_format="png",
                content_md5="raster-md5",
            ),
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].strategy.value == "static"
    assert previews[0].mode == "companion_raster"
    assert previews[0].path.endswith("_atlas.webp")
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_spine_skeleton_uses_atlas_full_region(tmp_path):
    atlas_path = tmp_path / "skeleton.atlas.txt"
    sheet_path = tmp_path / "skeleton.png"
    atlas_path.write_text(
        """
skeleton.png
size: 128,64
format: RGBA8888
filter: Linear,Linear
repeat: none
_full
  rotate: false
  xy: 12, 8
  size: 60, 40
  orig: 60, 40
  offset: 0, 0
  index: -1
eye
  rotate: false
  xy: 90, 8
  size: 16, 16
  orig: 16, 16
  offset: 0, 0
  index: -1
""".lstrip(),
        encoding="utf-8",
    )
    with Image.new("RGBA", (128, 64), (0, 0, 0, 0)) as sheet:
        sheet.alpha_composite(Image.new("RGBA", (60, 40), (230, 80, 70, 255)), (12, 8))
        sheet.alpha_composite(Image.new("RGBA", (16, 16), (60, 120, 230, 255)), (90, 8))
        sheet.save(sheet_path)

    entity = ResourceProcessingEntity(
        resource_type="spine_skeleton",
        source_directory=str(tmp_path),
        pack_name="Character Pack",
        title="skeleton.json",
        resource_path="skeleton.json",
        content_md5="spine-md5",
        member_count=3,
        files=[
            FileInfo(
                file_path=str(atlas_path),
                file_name=atlas_path.name,
                file_size=atlas_path.stat().st_size,
                file_format="txt",
                content_md5="spine-atlas-md5",
                file_role="attachment",
                is_primary=True,
            ),
            FileInfo(
                file_path=str(sheet_path),
                file_name=sheet_path.name,
                file_size=sheet_path.stat().st_size,
                file_format="png",
                content_md5="spine-sheet-md5",
                file_role="main",
            ),
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].strategy.value == "static"
    assert previews[0].mode == "full_region"
    assert previews[0].confidence == "medium"
    assert previews[0].path.endswith("_spine.webp")
    assert Path(previews[0].path).parent.name == "spine_skeleton"
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_animation_sequence_crops_frame_padding(tmp_path):
    files = []
    for idx, x in enumerate([56, 60]):
        image_path = tmp_path / f"run_{idx:02d}.png"
        image = Image.new("RGBA", (120, 96), (0, 0, 0, 0))
        with Image.new("RGBA", (34, 48), (240, 80, 80, 255)) as sprite:
            image.alpha_composite(sprite, (x, 36))
        image.save(image_path)
        files.append(
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5=f"anim-md5-{idx}",
                file_role="frame",
                is_primary=(idx == 0),
            )
        )

    entity = ResourceProcessingEntity(
        resource_type="animation_sequence",
        source_directory=str(tmp_path),
        pack_name="Sprite Pack",
        title="Run",
        content_md5="anim-md5",
        files=files,
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)
    assert len(previews) == 1
    assert previews[0].strategy.value == "gif"
    assert previews[0].width < previews[0].height
    assert previews[0].width < 512
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
    assert previews[0].width == 1024
    assert previews[0].height == 1024
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_pack_prefers_deduped_child_previews(tmp_path):
    direct_path = tmp_path / "direct.png"
    child_preview = tmp_path / "child_preview.webp"
    _make_image(direct_path, "red")
    with Image.new("RGB", (180, 80), color="blue") as image:
        image.paste(Image.new("RGB", (70, 50), color="white"), (55, 15))
        image.save(child_preview)

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path),
        pack_name="Vehicle Pack",
        title="Vehicle Pack",
        content_md5="child-preview-pack-md5",
        files=[
            FileInfo(
                file_path=str(direct_path),
                file_name=direct_path.name,
                file_size=direct_path.stat().st_size,
                file_format="png",
                content_md5="direct-md5",
                is_primary=True,
            )
        ],
        child_resource_count=12,
        contains_resource_types=["atlas", "single_image"],
        auxiliary_metadata={
            "child_previews": [
                {
                    "task_id": 1,
                    "resource_type": "atlas",
                    "resource_path": "Spritesheet/spritesheet.xml",
                    "preview_path": str(child_preview),
                    "priority": 10,
                    "coverage_count": 12,
                }
            ]
        },
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)
    assert len(previews) == 1
    assert previews[0].strategy.value == "static"
    assert previews[0].mode == "child_preview"
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_pack_child_previews_generate_gallery_pages(tmp_path):
    child_previews = []
    colors = [
        "red",
        "green",
        "blue",
        "yellow",
        "purple",
        "orange",
        "cyan",
        "magenta",
        "navy",
        "teal",
        "maroon",
        "olive",
        "gray",
    ]
    for idx, color in enumerate(colors):
        preview_path = tmp_path / "child_previews" / f"child_{idx:02d}.webp"
        _make_child_preview(preview_path, color)
        child_previews.append(
            {
                "task_id": idx + 1,
                "resource_type": "tiled_map" if idx < 9 else "single_image",
                "resource_path": f"Tiled_files/map_{idx:02d}.tmx" if idx < 9 else f"GUI/item_{idx:02d}.png",
                "preview_path": str(preview_path),
                "priority": 10 if idx < 9 else 70,
            }
        )

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path),
        pack_name="Sci-Fi Platformer",
        title="Sci-Fi Platformer",
        content_md5="pack-child-gallery-md5",
        child_resource_count=len(child_previews),
        contains_resource_types=["tiled_map", "single_image"],
        auxiliary_metadata={"child_previews": child_previews},
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 2
    assert previews[0].role == "primary"
    assert previews[0].mode == "child_previews"
    assert previews[1].role == "gallery"
    assert previews[1].mode == "child_previews_gallery"
    assert all(Path(preview.path).is_file() for preview in previews)


def _make_scene_showcase_child_previews(tmp_path, *, include_map: bool = False):
    def make_sprite(path: Path, color: str, size: tuple[int, int], box: tuple[int, int, int, int] | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        with Image.new("RGBA", size, (0, 0, 0, 0)) as image:
            draw = ImageDraw.Draw(image)
            draw.rectangle(box or (0, 0, size[0] - 1, size[1] - 1), fill=color)
            if box is None:
                draw.line((0, 0, size[0] - 1, size[1] - 1), fill="white", width=max(1, size[1] // 10))
            image.save(path)

    child_previews = []
    if include_map:
        map_path = tmp_path / "source" / "Tiled_files" / "Map1.png"
        make_sprite(map_path, "navy", (420, 260), None)
        child_previews.append(
            {
                "task_id": 100,
                "resource_type": "tiled_map",
                "resource_path": "Tiled_files/Map1.tmx",
                "preview_path": str(map_path),
                "source_paths": [str(map_path)],
                "priority": 10,
            }
        )

    sources = [
        ("Backgrounds/sky.png", "skyblue", (420, 260), None),
        ("Terrain/grass_tile.png", "green", (64, 32), None),
        ("Main Characters/Robot Boy/Idle (48x48).png", "red", (48, 48), (12, 6, 34, 45)),
        ("Enemies/Alien/Idle (40x40).png", "purple", (40, 40), (8, 8, 32, 36)),
        ("Items/Fruits/Apple.png", "yellow", (28, 28), (5, 5, 23, 23)),
        ("Traps/Spikes/Idle.png", "gray", (42, 24), (4, 4, 38, 22)),
    ]
    for idx, (resource_path, color, size, box) in enumerate(sources):
        source_path = tmp_path / "source" / resource_path
        make_sprite(source_path, color, size, box)
        child_previews.append(
            {
                "task_id": idx + 1,
                "resource_type": "single_image",
                "resource_path": resource_path,
                "preview_path": str(source_path),
                "source_paths": [str(source_path)],
                "priority": 70,
            }
        )
    return child_previews


@pytest.mark.asyncio
async def test_pack_child_previews_without_map_skip_generated_showcase(tmp_path):
    child_previews = _make_scene_showcase_child_previews(tmp_path, include_map=False)

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path / "source"),
        pack_name="Demo Platformer",
        title="Demo Platformer",
        content_md5="scene-no-map-pack-md5",
        child_resource_count=len(child_previews),
        contains_resource_types=["single_image"],
        auxiliary_metadata={"child_previews": child_previews},
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].strategy.value == "contact_sheet"
    assert previews[0].mode == "child_previews"
    assert previews[0].role == "primary"
    assert Path(previews[0].path).name == "scene-no-map-pack-md5_pack.webp"


@pytest.mark.asyncio
async def test_pack_child_previews_with_map_generate_scene_showcase_with_full_gallery(tmp_path):
    child_previews = _make_scene_showcase_child_previews(tmp_path, include_map=True)

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path / "source"),
        pack_name="Demo Platformer",
        title="Demo Platformer",
        content_md5="scene-showcase-pack-md5",
        child_resource_count=len(child_previews),
        contains_resource_types=["tiled_map", "single_image"],
        auxiliary_metadata={"child_previews": child_previews},
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 2
    assert previews[0].strategy.value == "static"
    assert previews[0].mode == "generated_showcase"
    assert previews[0].role == "primary"
    assert previews[1].role == "gallery"
    assert previews[1].mode == "child_previews_gallery"
    assert Path(previews[1].path).name == "scene-showcase-pack-md5_pack_gallery_02.webp"
    assert all(Path(preview.path).is_file() for preview in previews)
    with Image.open(previews[0].path) as image:
        colors = image.convert("RGB").getcolors(maxcolors=512 * 512)
    assert colors is not None
    assert len(colors) > 4


@pytest.mark.asyncio
async def test_pack_child_preview_collage_preserves_source_size_ratio(tmp_path):
    large_source = tmp_path / "source" / "large.png"
    small_source = tmp_path / "source" / "small.png"
    _make_image(large_source, "red", size=(300, 200))
    _make_image(small_source, "blue", size=(30, 30))

    large_preview = tmp_path / "previews_in" / "large.webp"
    small_preview = tmp_path / "previews_in" / "small.webp"
    _make_child_preview(large_preview, "gray", size=(128, 128))
    _make_child_preview(small_preview, "purple", size=(128, 128))

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path),
        pack_name="Mixed Size Sprites",
        title="Mixed Size Sprites",
        content_md5="mixed-size-pack-md5",
        child_resource_count=2,
        contains_resource_types=["single_image"],
        auxiliary_metadata={
            "child_previews": [
                {
                    "task_id": 1,
                    "resource_type": "single_image",
                    "resource_path": "large.png",
                    "preview_path": str(large_preview),
                    "source_paths": [str(large_source)],
                    "priority": 70,
                },
                {
                    "task_id": 2,
                    "resource_type": "single_image",
                    "resource_path": "small.png",
                    "preview_path": str(small_preview),
                    "source_paths": [str(small_source)],
                    "priority": 70,
                },
            ]
        },
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    with Image.open(previews[0].path) as image:
        pixels = list(image.convert("RGB").getdata())
    red_pixels = sum(1 for r, g, b in pixels if r > 170 and g < 90 and b < 90)
    blue_pixels = sum(1 for r, g, b in pixels if b > 120 and r < 100 and g < 120)
    assert red_pixels > blue_pixels * 20


def test_pack_grid_collage_stacks_wide_same_size_items(tmp_path):
    red_path = tmp_path / "wide_red.png"
    blue_path = tmp_path / "wide_blue.png"
    _make_image(red_path, "red", size=(320, 120))
    _make_image(blue_path, "blue", size=(320, 120))

    output_path = tmp_path / "wide_pack.webp"
    _save_pack_collage([str(red_path), str(blue_path)], output_path, "Wide Pack", max_items=2)

    with Image.open(output_path) as image:
        rgb = image.convert("RGB")
        top = rgb.getpixel((256, 124))
        bottom = rgb.getpixel((256, 348))
        left_mid = rgb.getpixel((144, 256))
        right_mid = rgb.getpixel((368, 256))

    assert top[0] > 160 and top[1] < 80 and top[2] < 80
    assert bottom[2] > 120 and bottom[0] < 100 and bottom[1] < 120
    left_is_red = left_mid[0] > 160 and left_mid[1] < 80 and left_mid[2] < 80
    right_is_blue = right_mid[2] > 120 and right_mid[0] < 100 and right_mid[1] < 120
    assert not (left_is_red and right_is_blue)


def test_pack_collage_converts_sprite_strip_to_keyframes(tmp_path):
    strip_path = tmp_path / "Run (64x64).png"
    with Image.new("RGBA", (640, 64), (0, 0, 0, 0)) as strip:
        for idx in range(10):
            color = ((idx * 20) % 255, 80, 220, 255)
            strip.alpha_composite(Image.new("RGBA", (48, 48), color), (idx * 64 + 8, 8))
        strip.save(strip_path)

    item = {
        "resource_type": "single_image",
        "resource_path": "Characters/Run (64x64).png",
        "preview_path": str(strip_path),
        "source_paths": [str(strip_path)],
    }

    preview = _open_pack_collage_image(item, prefer_source=True)

    assert preview.width < 320
    assert preview.height > 80


@pytest.mark.asyncio
async def test_pack_dynamic_pages_pack_large_items_into_downscaled_atlas(tmp_path):
    child_previews = []
    colors = ["red", "green", "blue"]
    sizes = [(400, 400), (420, 360), (390, 430)]
    for idx, color in enumerate(colors):
        source_path = tmp_path / "source" / f"large_{idx}.png"
        preview_path = tmp_path / "preview_in" / f"large_{idx}.webp"
        _make_image(source_path, color, size=sizes[idx])
        _make_child_preview(preview_path, color, size=(128, 128))
        child_previews.append(
            {
                "task_id": idx + 1,
                "resource_type": "single_image",
                "resource_path": f"large_{idx}.png",
                "preview_path": str(preview_path),
                "source_paths": [str(source_path)],
                "priority": 70,
            }
        )

    pages = _pack_preview_pages(child_previews)
    assert len(pages) == 1
    assert len(pages[0]) == 3

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path),
        pack_name="Large Pieces",
        title="Large Pieces",
        content_md5="large-pieces-pack-md5",
        child_resource_count=len(child_previews),
        contains_resource_types=["single_image"],
        auxiliary_metadata={"child_previews": child_previews},
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].role == "primary"


@pytest.mark.asyncio
async def test_pack_child_previews_skip_vector_format_when_sheet_covers(tmp_path):
    child_previews = []
    atlas_colors = ["red", "green", "blue", "yellow", "purple", "orange", "cyan", "magenta", "navy", "teal"]
    for idx, color in enumerate(atlas_colors):
        preview_path = tmp_path / "atlas_previews" / f"atlas_{idx:02d}.webp"
        _make_child_preview(preview_path, color)
        child_previews.append(
            {
                "task_id": idx + 1,
                "resource_type": "atlas",
                "resource_path": f"Spritesheet/sheet_{idx:02d}.xml",
                "preview_path": str(preview_path),
                "priority": 10,
            }
        )
    for idx, color in enumerate(["lime", "pink", "brown"]):
        preview_path = tmp_path / "vector_previews" / f"vector_{idx:02d}.webp"
        _make_child_preview(preview_path, color)
        child_previews.append(
            {
                "task_id": 100 + idx,
                "resource_type": "single_image",
                "resource_path": f"Vector/rune_{idx:02d}.svg",
                "preview_path": str(preview_path),
                "priority": 70,
            }
        )

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path),
        pack_name="Rune Pack",
        title="Rune Pack",
        content_md5="rune-pack-md5",
        child_resource_count=len(child_previews),
        contains_resource_types=["atlas", "single_image"],
        auxiliary_metadata={"child_previews": child_previews},
    )

    records = _pack_child_preview_records(entity)
    assert len(records) == len(atlas_colors)
    assert {record["top_dir"] for record in records} == {"spritesheet"}

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)
    assert len(previews) == 2
    assert previews[0].mode == "child_previews"
    assert previews[1].role == "gallery"


def test_pack_child_previews_skip_vector_format_when_raster_covers_same_stems(tmp_path):
    child_previews = []
    for idx in range(10):
        png_preview = tmp_path / "png_previews" / f"cursor_{idx:02d}.webp"
        vector_preview = tmp_path / "vector_previews" / f"cursor_{idx:02d}.webp"
        _make_child_preview(png_preview, (idx * 13 % 255, 80, 120))
        _make_child_preview(vector_preview, (idx * 17 % 255, 140, 80))
        child_previews.append(
            {
                "task_id": idx + 1,
                "resource_type": "single_image",
                "resource_path": f"PNG/Basic/cursor_{idx:02d}.png",
                "preview_path": str(png_preview),
                "priority": 70,
            }
        )
        child_previews.append(
            {
                "task_id": 100 + idx,
                "resource_type": "single_image",
                "resource_path": f"Vector/Basic/cursor_{idx:02d}.svg",
                "preview_path": str(vector_preview),
                "priority": 70,
            }
        )

    unique_vector_preview = tmp_path / "vector_previews" / "vector_only.webp"
    _make_child_preview(unique_vector_preview, "purple")
    child_previews.append(
        {
            "task_id": 999,
            "resource_type": "single_image",
            "resource_path": "Vector/Basic/vector_only.svg",
            "preview_path": str(unique_vector_preview),
            "priority": 70,
        }
    )

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path / "Cursor Pack"),
        pack_name="Cursor Pack",
        title="Cursor Pack",
        content_md5="cursor-pack-md5",
        child_resource_count=len(child_previews),
        contains_resource_types=["single_image"],
        auxiliary_metadata={"child_previews": child_previews},
    )

    records = _pack_child_preview_records(entity)
    paths = {record["resource_path"] for record in records}

    assert len(records) == 11
    assert all(f"PNG/Basic/cursor_{idx:02d}.png" in paths for idx in range(10))
    assert all(f"Vector/Basic/cursor_{idx:02d}.svg" not in paths for idx in range(10))
    assert "Vector/Basic/vector_only.svg" in paths


@pytest.mark.asyncio
async def test_large_icon_pack_uses_dense_gallery_pages(tmp_path):
    child_previews = []
    for idx in range(501):
        preview_path = tmp_path / "icon_previews" / f"icon_{idx:03d}.webp"
        _make_unique_icon_preview(preview_path, idx)
        child_previews.append(
            {
                "task_id": idx + 1,
                "resource_type": "single_image",
                "resource_path": f"icons/icon_{idx:03d}.svg",
                "preview_path": str(preview_path),
                "priority": 70,
            }
        )

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path / "Game Icons" / "icons-master"),
        pack_name="Game Icons",
        title="Game Icons",
        content_md5="large-icon-pack-md5",
        child_resource_count=len(child_previews),
        contains_resource_types=["single_image"],
        auxiliary_metadata={"child_previews": child_previews},
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 2
    assert previews[0].role == "primary"
    assert previews[1].role == "gallery"
    assert previews[0].width == 1024
    assert previews[0].height == 1024
    assert previews[1].width == 1024
    assert previews[1].height == 1024


@pytest.mark.asyncio
async def test_large_single_image_pack_uses_dense_pages_without_name_hint(tmp_path):
    child_previews = []
    for idx in range(501):
        preview_path = tmp_path / "cursor_previews" / f"cursor_{idx:03d}.webp"
        _make_unique_icon_preview(preview_path, idx)
        child_previews.append(
            {
                "task_id": idx + 1,
                "resource_type": "single_image",
                "resource_path": f"PNG/Basic/cursor_{idx:03d}.png",
                "preview_path": str(preview_path),
                "priority": 70,
            }
        )

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path / "Asset Pack"),
        pack_name="Asset Pack",
        title="Asset Pack",
        content_md5="large-cursor-pack-md5",
        child_resource_count=len(child_previews),
        contains_resource_types=["single_image"],
        auxiliary_metadata={"child_previews": child_previews},
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 2
    assert previews[0].width == 1024
    assert previews[0].height == 1024
    assert previews[1].role == "gallery"


@pytest.mark.asyncio
async def test_audio_pack_generates_single_summary_preview(tmp_path):
    files = []
    for idx, name in enumerate(["click_001.ogg", "click_002.ogg", "close_001.ogg", "open_001.ogg"]):
        audio_path = tmp_path / "Audio" / name
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"OggS")
        files.append(
            FileInfo(
                file_path=str(audio_path),
                file_name=name,
                file_size=audio_path.stat().st_size,
                file_format="ogg",
                content_md5=f"audio-md5-{idx}",
                is_primary=(idx == 0),
            )
        )

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path),
        pack_name="Interface Sounds",
        title="Interface Sounds",
        content_md5="audio-pack-md5",
        files=files,
        child_resource_count=len(files),
        contains_resource_types=["audio_file"],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].role == "primary"
    assert previews[0].strategy.value == "static"
    assert previews[0].mode == "audio_summary"
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_pack_prefers_official_sample_over_child_previews(tmp_path):
    sample_path = tmp_path / "Sample.png"
    child_preview = tmp_path / "child_preview.webp"
    with Image.new("RGB", (220, 120), color="red") as image:
        image.paste(Image.new("RGB", (80, 60), color="white"), (70, 30))
        image.save(sample_path)
    with Image.new("RGB", (180, 80), color="blue") as image:
        image.paste(Image.new("RGB", (70, 50), color="white"), (55, 15))
        image.save(child_preview)

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path),
        pack_name="UI Pack",
        title="UI Pack",
        content_md5="official-sample-pack-md5",
        files=[
            FileInfo(
                file_path=str(sample_path),
                file_name=sample_path.name,
                file_size=sample_path.stat().st_size,
                file_format="png",
                content_md5="sample-md5",
                is_primary=False,
            )
        ],
        child_resource_count=1,
        contains_resource_types=["atlas"],
        auxiliary_metadata={
            "child_previews": [
                {
                    "task_id": 1,
                    "resource_type": "atlas",
                    "resource_path": "Spritesheet/spritesheet.xml",
                    "preview_path": str(child_preview),
                    "priority": 10,
                    "coverage_count": 1,
                }
            ]
        },
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)
    assert len(previews) == 1
    assert previews[0].strategy.value == "static"
    assert previews[0].mode == "official_preview"
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_pack_detects_root_large_showcase_images_as_official_previews(tmp_path, monkeypatch):
    def fail_pack_collage(*args, **kwargs):
        raise AssertionError("root showcase previews should skip generated pack collage")

    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy._save_pack_collage",
        fail_pack_collage,
    )

    files = []
    for idx, name in enumerate(["20 Enemies.png", "Hello.png"]):
        image_path = tmp_path / name
        _make_child_preview(image_path, ["red", "blue"][idx], size=(630, 500))
        files.append(
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5=f"showcase-md5-{idx}",
                is_primary=(idx == 0),
            )
        )
    for idx in range(12):
        image_path = tmp_path / "Sprites" / f"sprite_{idx:02d}.png"
        _make_image(image_path, "green", size=(32, 32))
        files.append(
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5=f"sprite-md5-{idx}",
            )
        )

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path),
        pack_name="Pixel Adventure",
        title="Pixel Adventure",
        content_md5="root-showcase-pack-md5",
        files=files,
        child_resource_count=12,
        contains_resource_types=["single_image"],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 2
    assert previews[0].strategy.value == "static"
    assert previews[0].mode == "official_preview"
    assert previews[0].role == "primary"
    assert previews[1].mode == "official_preview_gallery"
    assert previews[1].role == "gallery"
    assert Path(previews[0].path).name == "root-showcase-pack-md5_pack.webp"
    assert Path(previews[1].path).name == "root-showcase-pack-md5_pack_gallery_02.webp"
    assert all(Path(preview.path).is_file() for preview in previews)


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
