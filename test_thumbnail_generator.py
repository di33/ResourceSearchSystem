import pytest
import os
from pathlib import Path
from thumbnail_generator import ThumbnailGenerator
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