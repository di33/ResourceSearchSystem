from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from ResourceProcessor.generate_previews import _dedupe_pack_child_resources
from ResourceProcessor.preview.crawler_thumbnail_policy import (
    CrawlerThumbnailPolicy,
    _open_for_sheet,
    _open_pack_collage_image,
    _pack_child_resource_records,
    _pack_child_preview_records,
    _pack_direct_image_pages,
    _pack_preview_pages,
    _pack_collage_item_sizes,
    _render_dense_atlas_image,
    _save_contact_sheet,
    _save_pack_collage,
    _save_tileset_sheet,
    _save_metadata_card,
    _tileset_sheet_image_paths,
)
from ResourceProcessor.preview_metadata import FileInfo, ResourceProcessingEntity
from spriter_preview.runtime_preview import (
    FileRef,
    RenderSprite,
    Spatial,
    _bounds_for_animation,
    _parse_scml,
    _render_animation_frame,
    _scale_for_bounds,
    _sprite_corners,
    _unmap_from_parent,
)


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


def test_spriter_sprite_pivot_y_one_is_top_origin():
    sprite = RenderSprite(
        file_ref=FileRef(
            folder=0,
            file=0,
            name="part.png",
            width=40,
            height=100,
            pivot_x=0.0,
            pivot_y=1.0,
            path=None,
        ),
        spatial=Spatial(),
        pivot_x=0.0,
        pivot_y=1.0,
        z_index=0,
    )

    corners = _sprite_corners(sprite)
    assert corners[0] == (0.0, 0.0)
    assert corners[2] == (40.0, -100.0)


def test_spriter_mirrored_parent_flips_child_angle():
    parent = Spatial(angle=30.0, scale_x=1.0, scale_y=-2.0)
    child = Spatial(angle=70.0, scale_x=1.0, scale_y=0.5)

    world = _unmap_from_parent(child, parent)

    assert world.angle == -40.0
    assert world.scale_x == 1.0
    assert world.scale_y == -1.0


def test_spriter_actions_render_with_shared_scale(tmp_path):
    image_path = tmp_path / "body.png"
    _make_image(image_path, "red", size=(20, 40))
    scml_path = tmp_path / "shared_scale.scml"
    scml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<spriter_data scml_version="1.0" generator="BrashMonkey Spriter" generator_version="r11">
  <folder id="0">
    <file id="0" name="body.png" width="20" height="40" pivot_x="0.5" pivot_y="0.5"/>
  </folder>
  <entity id="0" name="entity_000">
    <animation id="0" name="idle" length="1000" looping="true">
      <mainline>
        <key id="0">
          <object_ref id="0" timeline="0" key="0" z_index="0"/>
        </key>
      </mainline>
      <timeline id="0" name="body">
        <key id="0">
          <object folder="0" file="0" x="0" y="0"/>
        </key>
      </timeline>
    </animation>
    <animation id="1" name="dash" length="1000" looping="true">
      <mainline>
        <key id="0">
          <object_ref id="0" timeline="0" key="0" z_index="0"/>
        </key>
      </mainline>
      <timeline id="0" name="body">
        <key id="0">
          <object folder="0" file="0" x="-200" y="0"/>
        </key>
        <key id="1" time="500">
          <object folder="0" file="0" x="200" y="0"/>
        </key>
      </timeline>
    </animation>
  </entity>
</spriter_data>
""",
        encoding="utf-8",
    )
    data = _parse_scml(scml_path, [image_path])
    idle, dash = data.animations[:2]
    idle_bounds = _bounds_for_animation(data, idle, 5)
    dash_bounds = _bounds_for_animation(data, dash, 5)
    shared_scale = _scale_for_bounds([idle_bounds, dash_bounds], size=160)

    assert shared_scale < _scale_for_bounds([idle_bounds], size=160)

    idle_frame = _render_animation_frame(data, idle, 0, idle_bounds, size=160, scale=shared_scale)
    dash_frame = _render_animation_frame(data, dash, 0, dash_bounds, size=160, scale=shared_scale)
    idle_box = idle_frame.getchannel("A").getbbox()
    dash_box = dash_frame.getchannel("A").getbbox()

    assert idle_box is not None
    assert dash_box is not None
    assert abs((idle_box[2] - idle_box[0]) - (dash_box[2] - dash_box[0])) <= 1
    assert abs((idle_box[3] - idle_box[1]) - (dash_box[3] - dash_box[1])) <= 1


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
async def test_single_image_without_source_fails_instead_of_generating_metadata_fallback(tmp_path):
    entity = ResourceProcessingEntity(
        resource_id="res-missing-image",
        resource_type="single_image",
        source_directory=str(tmp_path),
        pack_name="Pack",
        title="Missing Image",
        resource_path="missing.svg",
        content_md5="missing-image-md5",
        files=[],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))

    with pytest.raises(RuntimeError, match="no source image file"):
        await policy.generate_previews(entity)


@pytest.mark.asyncio
async def test_single_image_svg_rasterization_failure_does_not_generate_fallback(tmp_path, monkeypatch):
    image_path = tmp_path / "broken.svg"
    image_path.write_text("<svg><broken>", encoding="utf-8")
    entity = ResourceProcessingEntity(
        resource_id="res-broken-svg",
        resource_type="single_image",
        source_directory=str(tmp_path),
        pack_name="Pack",
        title="Broken SVG",
        resource_path=image_path.name,
        content_md5="broken-svg-md5",
        files=[
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="svg",
                content_md5="broken-svg-file-md5",
                is_primary=True,
            )
        ],
    )
    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy._try_rasterize_svg",
        lambda *_args: False,
    )
    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))

    with pytest.raises(RuntimeError, match="could not rasterize SVG 'broken.svg'"):
        await policy.generate_previews(entity)

    assert not list((tmp_path / "previews").rglob("*_metadata.webp"))


@pytest.mark.asyncio
async def test_spriter_preview_uses_scml_runtime_renderer(tmp_path):
    image_path = tmp_path / "body.png"
    _make_image(image_path, "red", size=(48, 64))
    scml_path = tmp_path / "hero.scml"
    scml_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<spriter_data scml_version="1.0" generator="BrashMonkey Spriter" generator_version="r11">
  <folder id="0">
    <file id="0" name="body.png" width="48" height="64" pivot_x="0.5" pivot_y="0.5"/>
  </folder>
  <entity id="0" name="entity_000">
    <animation id="0" name="idle" length="500" looping="true">
      <mainline>
        <key id="0">
          <object_ref id="0" timeline="0" key="0" z_index="0"/>
        </key>
        <key id="1" time="250">
          <object_ref id="0" timeline="0" key="1" z_index="0"/>
        </key>
      </mainline>
      <timeline id="0" name="body">
        <key id="0">
          <object folder="0" file="0" x="-12" y="0" angle="0"/>
        </key>
        <key id="1" time="250">
          <object folder="0" file="0" x="12" y="0" angle="18"/>
        </key>
      </timeline>
    </animation>
  </entity>
</spriter_data>
""",
        encoding="utf-8",
    )
    entity = ResourceProcessingEntity(
        resource_id="res-spriter",
        resource_type="spriter",
        source_directory=str(tmp_path),
        pack_name="Pack",
        title="Hero Spriter",
        resource_path="hero.scml",
        content_md5="spriter-md5",
        files=[
            FileInfo(
                file_path=str(scml_path),
                file_name="hero.scml",
                file_size=scml_path.stat().st_size,
                file_format="scml",
                content_md5="scml-md5",
                is_primary=True,
            ),
            FileInfo(
                file_path=str(image_path),
                file_name="body.png",
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5="png-md5",
            ),
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)
    assert len(previews) == 1
    assert previews[0].mode == "spriter_runtime_actions_gif"
    assert previews[0].renderer == "spriter-scml-pillow"
    assert previews[0].format == "gif"
    assert previews[0].width < 260
    assert Path(previews[0].path).is_file()
    assert Path(previews[0].path).parent.name == "spriter"


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
async def test_single_image_all_transparent_source_preview_is_rejected(tmp_path):
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

    assert not previews[0].path
    assert previews[0].fail_reason
    assert "transparent" in previews[0].fail_reason.lower()


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


@pytest.mark.asyncio
async def test_tiled_tileset_uses_tileset_preview_path(tmp_path):
    tsx_path = tmp_path / "terrain.tsx"
    image_path = tmp_path / "terrain.png"
    with Image.new("RGB", (32, 32), color=(255, 0, 255)) as image:
        draw = ImageDraw.Draw(image)
        draw.rectangle((8, 8, 23, 23), fill="green")
        image.save(image_path)
    tsx_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tileset version="1.10" tiledversion="1.10.2" name="Terrain" tilewidth="16" tileheight="16" tilecount="1" columns="1">
 <image source="terrain.png" trans="ff00ff" width="32" height="32"/>
</tileset>
""",
        encoding="utf-8",
    )
    entity = ResourceProcessingEntity(
        resource_type="tiled_tileset",
        source_directory=str(tmp_path),
        title="terrain.tsx",
        resource_path="terrain.tsx",
        content_md5="tiled-tileset-md5",
        files=[
            FileInfo(
                file_path=str(tsx_path),
                file_name=tsx_path.name,
                file_size=tsx_path.stat().st_size,
                file_format="tsx",
                content_md5="tsx-md5",
                is_primary=True,
            ),
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5="png-md5",
            ),
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].path is not None
    assert previews[0].mode != "metadata_only"
    assert Path(previews[0].path).parent.name == "tiled_tileset"
    with Image.open(previews[0].path).convert("RGB") as preview:
        colors = preview.getcolors(maxcolors=preview.width * preview.height) or []
        assert all(color != (255, 0, 255) for _count, color in colors)


@pytest.mark.asyncio
async def test_tiled_tileset_reflows_extreme_aspect_source(tmp_path):
    tsx_path = tmp_path / "tile2map.tsx"
    image_path = tmp_path / "tile2map.png"
    image = Image.new("RGB", (64, 2048), color=(20, 20, 20))
    draw = ImageDraw.Draw(image)
    for row in range(128):
        for col in range(4):
            x = col * 16
            y = row * 16
            color = ((row * 19 + col * 47) % 256, (row * 37 + col * 23) % 256, (row * 11 + col * 71) % 256)
            draw.rectangle((x, y, x + 15, y + 15), fill=color)
    image.save(image_path)
    tsx_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<tileset name="tile2map" tilewidth="16" tileheight="16">
 <image source="tile2map.png" width="64" height="2048"/>
</tileset>
""",
        encoding="utf-8",
    )
    entity = ResourceProcessingEntity(
        resource_type="tiled_tileset",
        source_directory=str(tmp_path),
        title="tile2map.tsx",
        resource_path="tile2map.tsx",
        content_md5="tiled-tileset-reflow-md5",
        files=[
            FileInfo(
                file_path=str(tsx_path),
                file_name=tsx_path.name,
                file_size=tsx_path.stat().st_size,
                file_format="tsx",
                content_md5="tsx-reflow-md5",
                is_primary=True,
            ),
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5="png-reflow-md5",
            ),
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].strategy.value == "contact_sheet"
    assert previews[0].mode == "reflowed_tilesheet"
    with Image.open(previews[0].path) as preview:
        assert preview.size == (512, 512)


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
async def test_animation_sequence_single_image_uses_source_preview(tmp_path):
    image_path = tmp_path / "chest.png"
    image = Image.new("RGBA", (128, 32), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for idx, color in enumerate(["red", "green", "blue", "yellow"]):
        draw.rectangle((idx * 32 + 4, 8, idx * 32 + 27, 27), fill=color)
    image.save(image_path)

    entity = ResourceProcessingEntity(
        resource_type="animation_sequence",
        source_directory=str(tmp_path),
        pack_name="Sprite Pack",
        title="Chest",
        content_md5="single-sheet-md5",
        files=[
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="png",
                content_md5="single-sheet-file-md5",
                file_role="frame",
                is_primary=True,
            )
        ],
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)
    assert len(previews) == 1
    assert previews[0].strategy.value == "static"
    assert previews[0].mode == "source_image"
    assert previews[0].format == "webp"
    assert previews[0].width == 128
    assert previews[0].height == 32
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_animation_sequence_supports_tga_frames(tmp_path):
    files = []
    for idx, color in enumerate(["red", "green", "blue"]):
        image_path = tmp_path / f"walk_{idx:02d}.tga"
        with Image.new("RGBA", (48, 48), (0, 0, 0, 0)) as image:
            ImageDraw.Draw(image).ellipse((8 + idx, 8, 39 + idx, 39), fill=color)
            image.save(image_path)
        files.append(
            FileInfo(
                file_path=str(image_path),
                file_name=image_path.name,
                file_size=image_path.stat().st_size,
                file_format="tga",
                content_md5=f"tga-md5-{idx}",
                file_role="frame",
                is_primary=(idx == 0),
            )
        )

    entity = ResourceProcessingEntity(
        resource_type="animation_sequence",
        source_directory=str(tmp_path),
        pack_name="Sprite Pack",
        title="Walk",
        content_md5="tga-sequence-md5",
        files=files,
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)
    assert len(previews) == 1
    assert previews[0].strategy.value == "gif"
    assert previews[0].mode == "composed"
    assert previews[0].format == "gif"
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
    assert 64 <= previews[0].width <= 1024
    assert 64 <= previews[0].height <= 1024
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


def test_pack_child_resource_dedupe_keeps_distinct_animation_sequences():
    def animation_record(task_id: int, resource_path: str, coverage: set[str]) -> dict:
        return {
            "task_id": task_id,
            "source_resource_id": f"res-{task_id}",
            "resource_type": "animation_sequence",
            "title": resource_path,
            "resource_path": resource_path,
            "source_paths": [],
            "files": [],
            "priority": 30,
            "coverage_count": len(coverage),
            "_coverage_keys": coverage,
            "_file_keys": {f"{resource_path}/Attack1.png"},
        }

    shared = {"attack1", "attack1.png", "attack2", "attack2.png", "idle", "idle.png", "walk", "walk.png"}
    records = [
        animation_record(1, "1", shared | {"attack3_1", "attack3_1.png"}),
        animation_record(2, "2", shared | {"projectile", "projectile.png"}),
        animation_record(3, "3", shared | {"projectile", "projectile.png", "attack4_2", "attack4_2.png"}),
    ]

    deduped = _dedupe_pack_child_resources(records)

    assert sorted(record["resource_path"] for record in deduped) == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_pack_child_resources_generate_preview_without_child_previews(tmp_path):
    source_paths = []
    for idx, color in enumerate(["red", "blue"]):
        source_path = tmp_path / "source" / f"child_{idx}.png"
        _make_child_preview(source_path, color)
        source_paths.append(source_path)

    child_resources = [
        {
            "task_id": idx + 1,
            "resource_type": "single_image",
            "title": source_path.name,
            "resource_path": f"PNG/{source_path.name}",
            "source_paths": [str(source_path)],
            "files": [
                {
                    "file_path": str(source_path),
                    "file_name": source_path.name,
                    "file_size": source_path.stat().st_size,
                    "file_format": "png",
                    "content_md5": f"source-md5-{idx}",
                    "file_role": "main",
                    "is_primary": True,
                }
            ],
            "priority": 70,
        }
        for idx, source_path in enumerate(source_paths)
    ]
    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path / "source"),
        pack_name="Source Pack",
        title="Source Pack",
        content_md5="source-pack-md5",
        child_resource_count=len(child_resources),
        contains_resource_types=["single_image"],
        auxiliary_metadata={"child_resources": child_resources},
    )

    records = _pack_child_resource_records(entity)
    assert len(records) == 2
    assert {Path(record["preview_path"]).name for record in records} == {"child_0.png", "child_1.png"}

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].strategy.value == "contact_sheet"
    assert previews[0].mode == "child_previews"
    assert Path(previews[0].path).is_file()


@pytest.mark.asyncio
async def test_pack_single_svg_child_resource_rasterizes_preview(tmp_path):
    svg_path = tmp_path / "source" / "svg" / "Sample.svg"
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="128" height="96" viewBox="0 0 128 96">
<rect width="128" height="96" fill="#55aa66"/>
<circle cx="64" cy="48" r="28" fill="#ffffff"/>
</svg>
""",
        encoding="utf-8",
    )
    child_resources = [
        {
            "task_id": 101,
            "resource_type": "single_image",
            "title": "Sample.svg",
            "resource_path": "svg/Sample.svg",
            "source_paths": [str(svg_path)],
            "files": [
                {
                    "file_path": str(svg_path),
                    "file_name": svg_path.name,
                    "file_size": svg_path.stat().st_size,
                    "file_format": "svg",
                    "content_md5": "single-svg-child-md5",
                    "file_role": "main",
                    "is_primary": True,
                }
            ],
            "priority": 70,
        }
    ]
    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path / "source"),
        pack_name="SVG Pack",
        title="SVG Pack",
        content_md5="svg-pack-md5",
        child_resource_count=len(child_resources),
        contains_resource_types=["single_image"],
        auxiliary_metadata={"child_resources": child_resources},
    )

    policy = CrawlerThumbnailPolicy(str(tmp_path / "previews"))
    previews = await policy.generate_previews(entity)

    assert len(previews) == 1
    assert previews[0].mode == "child_preview"
    assert Path(previews[0].path).is_file()
    with Image.open(previews[0].path) as image:
        assert max(image.size) == 512


@pytest.mark.asyncio
async def test_pack_child_previews_fit_dense_atlas_page_without_fixed_gallery_split(tmp_path):
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

    assert len(previews) == 1
    assert previews[0].role == "primary"
    assert previews[0].mode == "child_previews"
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


def test_pack_collage_uses_dense_atlas_for_same_size_items(tmp_path, monkeypatch):
    red_path = tmp_path / "wide_red.png"
    blue_path = tmp_path / "wide_blue.png"
    _make_image(red_path, "red", size=(320, 120))
    _make_image(blue_path, "blue", size=(320, 120))

    calls = []

    def render_wrapper(images, output_size, *, layout_size=None, background=None, pad_to_output=True):
        calls.append(len(images))
        return _render_dense_atlas_image(
            images,
            output_size,
            layout_size=layout_size,
            background=background,
            pad_to_output=pad_to_output,
        )

    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy._render_dense_atlas_image",
        render_wrapper,
    )

    output_path = tmp_path / "wide_pack.webp"
    _save_pack_collage([str(red_path), str(blue_path)], output_path, "Wide Pack", max_items=2)

    with Image.open(output_path) as image:
        pixels = list(image.convert("RGB").getdata())

    red_pixels = sum(1 for r, g, b in pixels if r > 170 and g < 90 and b < 90)
    blue_pixels = sum(1 for r, g, b in pixels if b > 120 and r < 100 and g < 120)
    assert calls == [2]
    assert red_pixels > 0
    assert blue_pixels > 0


def test_pack_collage_does_not_pad_small_atlas_to_square_background(tmp_path):
    first_path = tmp_path / "asset_page_1.png"
    second_path = tmp_path / "asset_page_2.png"
    _make_image(first_path, "red", size=(256, 224))
    _make_image(second_path, "blue", size=(256, 224))

    output_path = tmp_path / "small_pack.webp"
    _save_pack_collage([str(first_path), str(second_path)], output_path, "Small Pack", size=1024, max_items=2)

    with Image.open(output_path) as image:
        assert image.size == (520, 224)


def test_pack_collage_crops_sparse_transparent_raster(tmp_path):
    first_path = tmp_path / "asset_page_1.png"
    second_path = tmp_path / "asset_page_2.png"
    _make_image(first_path, "red", size=(256, 224))
    second_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.new("RGBA", (256, 224), (0, 0, 0, 0)) as image:
        image.alpha_composite(Image.new("RGBA", (229, 76), (0, 0, 255, 255)), (15, 13))
        image.save(second_path)

    output_path = tmp_path / "cropped_pack.webp"
    _save_pack_collage([str(first_path), str(second_path)], output_path, "Cropped Pack", size=1024, max_items=2)

    with Image.open(output_path) as image:
        assert image.width < 520
        assert image.height <= 224


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


def test_pack_collage_keeps_short_sprite_strip_as_strip(tmp_path):
    strip_path = tmp_path / "Attack (64x64).png"
    with Image.new("RGBA", (256, 64), (0, 0, 0, 0)) as strip:
        for idx in range(4):
            color = ((idx * 40) % 255, 80, 220, 255)
            strip.alpha_composite(Image.new("RGBA", (48, 48), color), (idx * 64 + 8, 8))
        strip.save(strip_path)

    item = {
        "resource_type": "single_image",
        "resource_path": "Characters/Attack (64x64).png",
        "preview_path": str(strip_path),
        "source_paths": [str(strip_path)],
    }

    preview = _open_pack_collage_image(item, prefer_source=True)

    assert preview.width > 200
    assert preview.height < 80


def test_pack_collage_item_sizes_parse_svg_without_rasterizing(tmp_path, monkeypatch):
    svg_path = tmp_path / "icon.svg"
    svg_path.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 42 24">
<rect width="42" height="24" fill="red"/>
</svg>
""",
        encoding="utf-8",
    )

    def fail_rasterize(*args, **kwargs):
        raise AssertionError("SVG size probing should not rasterize")

    monkeypatch.setattr(
        "ResourceProcessor.preview.crawler_thumbnail_policy._try_rasterize_svg",
        fail_rasterize,
    )

    sizes = _pack_collage_item_sizes(
        [
            {
                "resource_type": "single_image",
                "resource_path": "icons/icon.svg",
                "preview_path": str(svg_path),
                "source_paths": [str(svg_path)],
            }
        ]
    )

    assert sizes == [(42, 24)]


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
    assert len(previews) == 1
    assert previews[0].mode == "child_previews"
    assert previews[0].role == "primary"


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
async def test_large_icon_pack_uses_sampled_dense_atlas(tmp_path):
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

    assert len(previews) >= 1
    assert previews[0].role == "primary"
    assert all(64 <= preview.width <= 1024 for preview in previews)
    assert all(64 <= preview.height <= 1024 for preview in previews)
    assert all(preview.role == "gallery" for preview in previews[1:])


@pytest.mark.asyncio
async def test_large_single_image_pack_uses_sampled_dense_atlas_without_name_hint(tmp_path):
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

    assert len(previews) >= 1
    assert previews[0].role == "primary"
    assert all(64 <= preview.width <= 1024 for preview in previews)
    assert all(64 <= preview.height <= 1024 for preview in previews)
    assert all(preview.role == "gallery" for preview in previews[1:])


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


def test_pack_direct_image_pages_sample_large_mixed_pack_by_directory():
    paths = []
    for idx in range(260):
        paths.append(f"K:/pack/1 Main Character/Run/Run_{idx:04d}.png")
    for idx in range(180):
        paths.append(f"K:/pack/3 Enemies/Alien/Idle_{idx:04d}.png")
    for idx in range(120):
        paths.append(f"K:/pack/2 Location/Tiles/tile_{idx:04d}.png")
    for idx in range(80):
        paths.append(f"K:/pack/4 GUI/Icons/icon_{idx:04d}.png")

    pages = _pack_direct_image_pages(paths)
    sampled = [path for page in pages for path in page]

    assert len(sampled) <= 96
    assert pages
    assert any("Main Character" in path for path in sampled)
    assert any("Enemies" in path for path in sampled)
    assert any("Location" in path for path in sampled)
    assert any("GUI" in path for path in sampled)


def test_pack_direct_image_pages_keep_icon_gallery_capacity():
    paths = [f"K:/pack/icons/icon_{idx:04d}.png" for idx in range(700)]

    pages = _pack_direct_image_pages(paths)
    sampled = [path for page in pages for path in page]

    assert len(sampled) <= 96
    assert pages


def test_pack_child_resource_records_sample_large_mixed_pack_before_layout(tmp_path):
    child_resources = []
    buckets = [
        ("1 Main Character", "hero"),
        ("2 Location/Tiles", "tile"),
        ("3 Enemies", "enemy"),
        ("4 Props", "prop"),
    ]
    for idx in range(180):
        bucket, stem = buckets[idx % len(buckets)]
        source_path = tmp_path / "source" / bucket / f"{stem}_{idx:04d}.png"
        _make_image(
            source_path,
            color=((idx * 31) % 256, (idx * 53) % 256, (idx * 71) % 256),
            size=(16 + idx % 5, 18 + idx % 7),
        )
        resource_type = "tileset" if idx % 45 == 0 else "single_image"
        child_resources.append(
            {
                "task_id": idx + 1,
                "resource_type": resource_type,
                "title": source_path.name,
                "resource_path": f"{bucket}/{source_path.name}",
                "source_paths": [str(source_path)],
                "files": [
                    {
                        "file_path": str(source_path),
                        "file_name": source_path.name,
                        "file_size": source_path.stat().st_size,
                        "file_format": "png",
                        "content_md5": f"source-md5-{idx}",
                        "file_role": "main",
                        "is_primary": True,
                    }
                ],
                "priority": 20 if resource_type == "tileset" else 70,
            }
        )

    entity = ResourceProcessingEntity(
        resource_type="pack",
        source_directory=str(tmp_path / "source"),
        pack_name="Large Mixed Pack",
        title="Large Mixed Pack",
        content_md5="large-mixed-pack-md5",
        child_resource_count=len(child_resources),
        contains_resource_types=["single_image", "tileset"],
        auxiliary_metadata={"child_resources": child_resources},
    )

    records = _pack_child_resource_records(entity)
    pages = _pack_preview_pages(records, entity)
    paged_records = [record for page in pages for record in page]

    assert len(records) <= 96
    assert pages
    assert {record["task_id"] for record in paged_records} == {record["task_id"] for record in records}
    assert any("Main Character" in record["resource_path"] for record in records)
    assert any("Location" in record["resource_path"] for record in records)
    assert any("Enemies" in record["resource_path"] for record in records)
