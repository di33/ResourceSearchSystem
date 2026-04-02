import os
from PIL import Image
import asyncio
import logging
from pathlib import Path
from typing import Union

logging.basicConfig(level=logging.INFO)

class ThumbnailGenerator:
    def __init__(self, output_dir: str):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    async def generate_thumbnail(self, input_path: str, size: tuple = (256, 256)) -> str:
        """
        Generate a thumbnail for an image file.

        Args:
            input_path (str): Path to the input image file.
            size (tuple): Size of the thumbnail (width, height).

        Returns:
            str: Path to the generated thumbnail.
        """
        try:
            input_path = Path(input_path)
            output_path = self.output_dir / f"{input_path.stem}_thumbnail{input_path.suffix}"

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._create_thumbnail, input_path, output_path, size)

            logging.info(f"Thumbnail generated: {output_path}")
            return str(output_path)
        except Exception as e:
            logging.error(f"Failed to generate thumbnail for {input_path}: {e}")
            raise

    def _create_thumbnail(self, input_path: Path, output_path: Path, size: tuple):
        with Image.open(input_path) as img:
            img.thumbnail(size)
            img.save(output_path)

    async def generate_gif(self, input_dir: str, output_name: str = "output.gif") -> str:
        """
        Generate a GIF from a sequence of images in a directory.

        Args:
            input_dir (str): Path to the directory containing images.
            output_name (str): Name of the output GIF file.

        Returns:
            str: Path to the generated GIF.
        """
        try:
            input_dir = Path(input_dir)
            output_path = self.output_dir / output_name

            images = []
            for image_file in sorted(input_dir.glob("*.png")):
                with Image.open(image_file) as img:
                    images.append(img.copy())

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._create_gif, images, output_path)

            logging.info(f"GIF generated: {output_path}")
            return str(output_path)
        except Exception as e:
            logging.error(f"Failed to generate GIF: {e}")
            raise

    def _create_gif(self, images: list, output_path: Path):
        images[0].save(output_path, save_all=True, append_images=images[1:], loop=0, duration=500)

    async def render_model_thumbnail(self, model_path: str) -> str:
        """
        Render a thumbnail for a 3D model (stub implementation).

        Args:
            model_path (str): Path to the 3D model file.

        Returns:
            str: Path to the rendered thumbnail.
        """
        # Placeholder for actual 3D rendering logic
        logging.info(f"Rendering thumbnail for model: {model_path}")
        return ""