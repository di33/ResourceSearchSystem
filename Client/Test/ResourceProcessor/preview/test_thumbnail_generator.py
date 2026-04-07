import pytest
import os
from pathlib import Path
from ResourceProcessor.preview.thumbnail_generator import ThumbnailGenerator
import pytest_asyncio
from PIL import Image
@pytest_asyncio.fixture
async def setup_generator(tmp_path):
    generator = ThumbnailGenerator(output_dir=tmp_path)
    yield generator

def create_valid_png(file_path):
    """Create a valid PNG image for testing."""
    with Image.new("RGB", (100, 100), color="red") as img:
        img.save(file_path)

@pytest.mark.asyncio
async def test_generate_thumbnail(setup_generator, tmp_path):
    generator = setup_generator

    # Create a valid image
    input_image = tmp_path / "test_image.png"
    create_valid_png(input_image)

    # Generate thumbnail
    result = await generator.generate_thumbnail(str(input_image))

    assert Path(result).exists()

@pytest.mark.asyncio
async def test_generate_gif(setup_generator, tmp_path):
    generator = setup_generator

    # Create valid images
    input_dir = tmp_path / "images"
    input_dir.mkdir()
    for i in range(3):
        create_valid_png(input_dir / f"image_{i}.png")

    # Generate GIF
    result = await generator.generate_gif(str(input_dir))

    assert Path(result).exists()

@pytest.mark.asyncio
async def test_render_model_thumbnail(setup_generator):
    generator = setup_generator

    # Test stub for model rendering
    model_path = "dummy_model.obj"
    result = await generator.render_model_thumbnail(model_path)

    assert result == ""  # Placeholder for actual implementation


@pytest.mark.asyncio
async def test_generate_fbx_preview_gif_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "ResourceProcessor.preview.thumbnail_generator.find_blender_executable",
        lambda: None,
    )
    out = tmp_path / "previews"
    out.mkdir()
    fbx = tmp_path / "model.fbx"
    fbx.write_bytes(b"dummy")
    gen = ThumbnailGenerator(str(out))
    result = await gen.generate_fbx_preview_gif(str(fbx), "m_preview.gif")
    p = Path(result)
    assert p.suffix.lower() == ".gif"
    assert p.is_file()
    assert p.stat().st_size > 0


@pytest.mark.asyncio
async def test_render_model_thumbnail_fbx(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "ResourceProcessor.preview.thumbnail_generator.find_blender_executable",
        lambda: None,
    )
    out = tmp_path / "previews"
    out.mkdir()
    fbx = tmp_path / "a.fbx"
    fbx.write_bytes(b"x")
    gen = ThumbnailGenerator(str(out))
    result = await gen.render_model_thumbnail(str(fbx))
    assert Path(result).suffix.lower() == ".gif"


# ---------------------------------------------------------------------------
# New tests for Spec 1
# ---------------------------------------------------------------------------

import hashlib
from ResourceProcessor.preview.thumbnail_generator import validate_preview, write_placeholder_model_gif


def _content_md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _make_image(path: Path, size=(200, 150), color="red"):
    """Helper to create a test image at *path*."""
    with Image.new("RGB", size, color=color) as img:
        img.save(path)


@pytest.mark.asyncio
async def test_generate_preview_webp_format(tmp_path):
    """Preview should default to webp format."""
    src = tmp_path / "photo.png"
    _make_image(src, size=(800, 600))
    gen = ThumbnailGenerator(str(tmp_path / "out"))
    md5 = _content_md5(src)
    result = await gen.generate_preview(str(src), md5)
    assert Path(result).suffix.lower() == ".webp"


@pytest.mark.asyncio
async def test_generate_preview_long_edge_512(tmp_path):
    """1000x500 image should become 512x256 (long edge 512, proportional)."""
    src = tmp_path / "wide.png"
    _make_image(src, size=(1000, 500))
    gen = ThumbnailGenerator(str(tmp_path / "out"))
    md5 = _content_md5(src)
    result = await gen.generate_preview(str(src), md5)
    with Image.open(result) as im:
        assert max(im.size) == 512
        assert im.size == (512, 256)


@pytest.mark.asyncio
async def test_generate_preview_naming_convention(tmp_path):
    """Output filename must be {content_md5}_preview.webp."""
    src = tmp_path / "img.png"
    _make_image(src, size=(600, 400))
    gen = ThumbnailGenerator(str(tmp_path / "out"))
    md5 = _content_md5(src)
    result = Path(await gen.generate_preview(str(src), md5))
    assert result.name == f"{md5}_preview.webp"


@pytest.mark.asyncio
async def test_generate_preview_small_image_not_upscaled(tmp_path):
    """Images smaller than max_size must not be upscaled."""
    src = tmp_path / "tiny.png"
    _make_image(src, size=(100, 50))
    gen = ThumbnailGenerator(str(tmp_path / "out"))
    md5 = _content_md5(src)
    result = await gen.generate_preview(str(src), md5)
    with Image.open(result) as im:
        assert im.size == (100, 50)


def test_validate_preview_passes_normal_image(tmp_path):
    """A normal image should pass validation."""
    p = tmp_path / "ok.webp"
    _make_image(p, size=(512, 256), color="blue")
    passed, reason = validate_preview(str(p))
    assert passed, reason


def test_validate_preview_fails_all_black(tmp_path):
    """An all-black image must fail validation."""
    p = tmp_path / "black.png"
    Image.new("RGB", (128, 128), (0, 0, 0)).save(p)
    passed, reason = validate_preview(str(p))
    assert not passed
    assert "black" in reason.lower()


def test_validate_preview_fails_all_white(tmp_path):
    """An all-white image must fail validation."""
    p = tmp_path / "white.png"
    Image.new("RGB", (128, 128), (255, 255, 255)).save(p)
    passed, reason = validate_preview(str(p))
    assert not passed
    assert "white" in reason.lower()


def test_validate_preview_fails_nonexistent():
    """Validation must fail for a non-existent file."""
    passed, reason = validate_preview("/no/such/file.png")
    assert not passed
    assert "exist" in reason.lower() or "not found" in reason.lower()


def test_placeholder_gif_512(tmp_path):
    """Placeholder GIF frames should be 512x512."""
    out = tmp_path / "placeholder.gif"
    model = tmp_path / "m.fbx"
    model.write_bytes(b"dummy")
    write_placeholder_model_gif(model, out)
    with Image.open(out) as im:
        assert im.size == (512, 512)


@pytest.mark.asyncio
async def test_fbx_preview_default_frame_size_512(tmp_path, monkeypatch):
    """FBX preview methods should default to frame_size=512."""
    monkeypatch.setattr(
        "ResourceProcessor.preview.thumbnail_generator.find_blender_executable",
        lambda: None,
    )
    out = tmp_path / "previews"
    out.mkdir()
    fbx = tmp_path / "model.fbx"
    fbx.write_bytes(b"fbx-data")
    gen = ThumbnailGenerator(str(out))
    result = await gen.generate_fbx_preview_gif_result(str(fbx), "test.gif")
    with Image.open(result["path"]) as im:
        assert im.size == (512, 512)