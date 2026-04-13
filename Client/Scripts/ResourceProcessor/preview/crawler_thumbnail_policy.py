from __future__ import annotations

import asyncio
import math
import os
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from PIL import Image, ImageDraw, ImageFont

from ResourceProcessor.preview.thumbnail_generator import ThumbnailGenerator, validate_preview
from ResourceProcessor.preview_metadata import PreviewInfo, PreviewStrategy, ResourceProcessingEntity

RASTER_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
SVG_EXTS = {".svg"}
FONT_EXTS = {".ttf", ".otf"}
AUDIO_EXTS = {".ogg", ".wav", ".mp3", ".flac"}


def _natural_sort_key(path: str) -> list[tuple[int, object]]:
    parts: list[tuple[int, object]] = []
    chunk = ""
    is_digit = False
    for ch in Path(path).name.lower():
        if ch.isdigit():
            if chunk and not is_digit:
                parts.append((1, chunk))
                chunk = ""
            chunk += ch
            is_digit = True
        else:
            if chunk and is_digit:
                parts.append((0, int(chunk)))
                chunk = ""
            chunk += ch
            is_digit = False
    if chunk:
        parts.append((0, int(chunk)) if is_digit else (1, chunk))
    return parts


def _sample_paths(paths: list[str], limit: int) -> list[str]:
    if len(paths) <= limit:
        return paths
    step = (len(paths) - 1) / float(limit - 1)
    sampled = []
    for idx in range(limit):
        sampled.append(paths[round(idx * step)])
    return sampled


def _wrap_text(text: str, width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _default_font(size: int = 18):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def _save_metadata_card(
    output_path: Path,
    title: str,
    subtitle: str,
    lines: list[str],
    size: int = 512,
) -> None:
    image = Image.new("RGB", (size, size), (42, 54, 74))
    draw = ImageDraw.Draw(image)
    title_font = _default_font(28)
    body_font = _default_font(18)

    y = 36
    draw.rounded_rectangle((24, 24, size - 24, size - 24), radius=18, outline=(119, 141, 169), width=2)
    draw.text((40, y), title[:56], fill=(244, 247, 250), font=title_font)
    y += 48
    if subtitle:
        for line in _wrap_text(subtitle, 34)[:2]:
            draw.text((40, y), line, fill=(194, 203, 216), font=body_font)
            y += 26
        y += 8
    for line in lines[:10]:
        for wrapped in _wrap_text(line, 38):
            draw.text((40, y), wrapped, fill=(222, 230, 240), font=body_font)
            y += 24
    image.save(output_path, format="WEBP")


def _contact_sheet_background(size: int) -> Image.Image:
    return Image.new("RGB", (size, size), (246, 247, 249))


def _open_for_sheet(path: str) -> Image.Image:
    with Image.open(path) as img:
        if img.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", img.size, (240, 240, 240))
            bg.paste(img, mask=img.split()[-1])
            return bg
        return img.convert("RGB")


def _save_contact_sheet(image_paths: list[str], output_path: Path, size: int = 512) -> None:
    sheet = _contact_sheet_background(size)
    grid = min(4, max(2, math.ceil(math.sqrt(len(image_paths)))))
    cell = size // grid
    padding = 8
    for idx, path in enumerate(image_paths[: grid * grid]):
        row = idx // grid
        col = idx % grid
        x0 = col * cell
        y0 = row * cell
        with _open_for_sheet(path) as img:
            img.thumbnail((cell - padding * 2, cell - padding * 2))
            paste_x = x0 + (cell - img.width) // 2
            paste_y = y0 + (cell - img.height) // 2
            sheet.paste(img, (paste_x, paste_y))
    sheet.save(output_path, format="WEBP")


def _save_pack_collage(image_paths: list[str], output_path: Path, title: str, size: int = 512) -> None:
    sheet = _contact_sheet_background(size)
    draw = ImageDraw.Draw(sheet)
    title_font = _default_font(24)
    body_font = _default_font(16)
    draw.rounded_rectangle((20, 20, size - 20, size - 20), radius=18, outline=(180, 186, 196), width=2)
    draw.text((36, 32), title[:28], fill=(33, 37, 41), font=title_font)

    collage_top = 88
    collage_size = size - collage_top - 28
    grid = min(3, max(2, math.ceil(math.sqrt(max(len(image_paths), 1)))))
    cell = collage_size // grid
    padding = 6
    for idx, path in enumerate(image_paths[: grid * grid]):
        row = idx // grid
        col = idx % grid
        x0 = 28 + col * cell
        y0 = collage_top + row * cell
        with _open_for_sheet(path) as img:
            img.thumbnail((cell - padding * 2, cell - padding * 2))
            paste_x = x0 + (cell - img.width) // 2
            paste_y = y0 + (cell - img.height) // 2
            sheet.paste(img, (paste_x, paste_y))
    if not image_paths:
        draw.text((36, collage_top + 12), "No child previews available", fill=(96, 103, 112), font=body_font)
    sheet.save(output_path, format="WEBP")


def _save_gif(image_paths: list[str], output_path: Path, size: int = 512) -> None:
    frames: list[Image.Image] = []
    for path in image_paths:
        with _open_for_sheet(path) as img:
            img.thumbnail((size, size))
            canvas = Image.new("RGB", (size, size), (245, 245, 245))
            canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2))
            frames.append(canvas)
    if len(frames) == 1:
        frames.append(frames[0].copy())
    first, *rest = frames
    first.save(output_path, save_all=True, append_images=rest, duration=120, loop=0, optimize=True)


def _try_rasterize_svg(svg_path: str, output_path: str, size: int = 1024) -> bool:
    try:
        import cairosvg  # type: ignore
    except Exception:
        return False
    try:
        cairosvg.svg2png(url=svg_path, write_to=output_path, output_width=size, output_height=size)
        return True
    except Exception:
        return False


class CrawlerThumbnailPolicy:
    def __init__(self, output_dir: str, max_size: int = 512):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_size = max_size
        self.generator = ThumbnailGenerator(str(self.output_dir))

    async def generate_previews(self, entity: ResourceProcessingEntity) -> list[PreviewInfo]:
        resource_type = entity.resource_type
        if resource_type == "pack":
            return [await self._generate_pack_preview(entity)]
        if resource_type == "single_image":
            return [await self._generate_single_image_preview(entity)]
        if resource_type == "tileset":
            return await self._generate_tileset_previews(entity)
        if resource_type == "animation_sequence":
            return [await self._generate_animation_preview(entity)]
        if resource_type == "audio_file":
            return [await self._generate_audio_preview(entity)]
        if resource_type == "font_file":
            return [await self._generate_font_preview(entity)]
        return [await self._generate_metadata_preview(entity, mode="metadata_only")]

    async def _generate_single_image_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        primary = entity.primary_file or (entity.files[0] if entity.files else None)
        if primary is None:
            return await self._generate_metadata_preview(entity, mode="metadata_only")

        ext = Path(primary.file_path).suffix.lower()
        if ext in RASTER_EXTS:
            preview_path = await self.generator.generate_preview(primary.file_path, entity.content_md5, max_size=self.max_size)
            return self._preview_info(preview_path, PreviewStrategy.STATIC, "direct", "high")
        if ext in SVG_EXTS:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp:
                temp_path = temp.name
            try:
                if _try_rasterize_svg(primary.file_path, temp_path):
                    preview_path = await self.generator.generate_preview(temp_path, entity.content_md5, max_size=self.max_size)
                    return self._preview_info(preview_path, PreviewStrategy.STATIC, "direct", "medium")
            finally:
                Path(temp_path).unlink(missing_ok=True)
        return await self._generate_metadata_preview(entity, mode="fallback")

    async def _generate_tileset_previews(self, entity: ResourceProcessingEntity) -> list[PreviewInfo]:
        image_paths = [f.file_path for f in entity.files if Path(f.file_path).suffix.lower() in RASTER_EXTS]
        if not image_paths:
            return [await self._generate_metadata_preview(entity, mode="metadata_only")]

        sampled = _sample_paths(sorted(image_paths, key=_natural_sort_key), 16)
        contact_sheet_path = self.output_dir / f"{entity.content_md5}_tileset.webp"
        await asyncio.get_running_loop().run_in_executor(None, _save_contact_sheet, sampled, contact_sheet_path, self.max_size)
        previews = [self._preview_info(str(contact_sheet_path), PreviewStrategy.CONTACT_SHEET, "composed", "high")]

        primary_tile = sampled[0]
        primary_preview_path = await self.generator.generate_preview(primary_tile, f"{entity.content_md5}_tile", max_size=self.max_size)
        gallery = self._preview_info(primary_preview_path, PreviewStrategy.STATIC, "direct", "medium")
        gallery.role = "gallery"
        previews.append(gallery)
        return previews

    async def _generate_animation_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        image_paths = [f.file_path for f in entity.files if Path(f.file_path).suffix.lower() in RASTER_EXTS]
        if len(image_paths) < 2:
            return await self._generate_metadata_preview(entity, mode="metadata_only")

        sampled = _sample_paths(sorted(image_paths, key=_natural_sort_key), 12)
        gif_path = self.output_dir / f"{entity.content_md5}_sequence.gif"
        await asyncio.get_running_loop().run_in_executor(None, _save_gif, sampled, gif_path, self.max_size)
        return self._preview_info(str(gif_path), PreviewStrategy.GIF, "composed", "high")

    async def _generate_audio_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        return await self._generate_metadata_preview(entity, mode="metadata_only", title_prefix="Audio")

    async def _generate_font_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        primary = entity.primary_file or (entity.files[0] if entity.files else None)
        if primary and Path(primary.file_path).suffix.lower() in FONT_EXTS:
            output_path = self.output_dir / f"{entity.content_md5}_font.webp"

            def render_font_preview() -> None:
                image = Image.new("RGB", (self.max_size, self.max_size), (250, 248, 244))
                draw = ImageDraw.Draw(image)
                try:
                    font = ImageFont.truetype(primary.file_path, 48)
                except OSError:
                    font = _default_font(24)
                draw.text((32, 42), entity.title or primary.file_name, fill=(48, 54, 61), font=_default_font(24))
                draw.text((40, 160), "Aa Bb Cc", fill=(20, 24, 30), font=font)
                draw.text((40, 260), "0123456789", fill=(20, 24, 30), font=font)
                draw.text((40, 360), "Game UI Preview", fill=(52, 59, 72), font=font)
                image.save(output_path, format="WEBP")

            await asyncio.get_running_loop().run_in_executor(None, render_font_preview)
            return self._preview_info(str(output_path), PreviewStrategy.STATIC, "composed", "medium")
        return await self._generate_metadata_preview(entity, mode="metadata_only")

    async def _generate_pack_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        image_paths = [f.file_path for f in entity.files if Path(f.file_path).suffix.lower() in RASTER_EXTS]
        if image_paths:
            sampled = _sample_paths(sorted(image_paths, key=_natural_sort_key), 9)
            output_path = self.output_dir / f"{entity.content_md5}_pack.webp"
            await asyncio.get_running_loop().run_in_executor(
                None,
                _save_pack_collage,
                sampled,
                output_path,
                entity.title or entity.pack_name or "Pack",
                self.max_size,
            )
            return self._preview_info(str(output_path), PreviewStrategy.CONTACT_SHEET, "composed", "medium")
        return await self._generate_metadata_preview(entity, mode="metadata_only", title_prefix="Pack")

    async def _generate_metadata_preview(
        self,
        entity: ResourceProcessingEntity,
        mode: str,
        title_prefix: str = "",
    ) -> PreviewInfo:
        output_path = self.output_dir / f"{entity.content_md5}_metadata.webp"
        title = entity.title or entity.resource_type
        if title_prefix:
            title = f"{title_prefix}: {title}"
        lines = [
            f"Pack: {entity.pack_name or 'Unknown'}",
            f"Type: {entity.resource_type}",
        ]
        if entity.resource_path:
            lines.append(f"Path: {entity.resource_path}")
        if entity.tags:
            lines.append(f"Tags: {', '.join(entity.tags[:6])}")
        if entity.member_count:
            lines.append(f"Files: {entity.member_count}")
        if entity.missing_files:
            lines.append(f"Missing: {len(entity.missing_files)}")
        if entity.child_resource_count:
            lines.append(f"Children: {entity.child_resource_count}")
        if entity.contains_resource_types:
            lines.append(f"Contains: {', '.join(entity.contains_resource_types[:4])}")
        subtitle = entity.source_description or entity.category
        await asyncio.get_running_loop().run_in_executor(
            None,
            _save_metadata_card,
            output_path,
            title,
            subtitle,
            lines,
            self.max_size,
        )
        return self._preview_info(str(output_path), PreviewStrategy.STATIC, mode, "low")

    def _preview_info(
        self,
        preview_path: str,
        strategy: PreviewStrategy,
        mode: str,
        confidence: str,
    ) -> PreviewInfo:
        passed, reason = validate_preview(preview_path)
        if not passed:
            return PreviewInfo(
                strategy=strategy,
                mode=mode,
                confidence="low",
                fail_reason=reason,
            )
        with Image.open(preview_path) as image:
            width, height = image.size
        return PreviewInfo(
            strategy=strategy,
            role="primary",
            path=os.path.abspath(preview_path),
            mode=mode,
            confidence=confidence,
            format=Path(preview_path).suffix.lstrip("."),
            width=width,
            height=height,
            size=os.path.getsize(preview_path),
            renderer="crawler-policy",
        )
