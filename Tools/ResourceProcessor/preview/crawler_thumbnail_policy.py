from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat

from ResourceProcessor.preview.thumbnail_generator import ThumbnailGenerator, validate_preview
from ResourceProcessor.preview_metadata import PreviewInfo, PreviewStrategy, ResourceProcessingEntity
from resource_contracts.resource_types import (
    ANIMATION_SEQUENCE_RESOURCE_TYPE,
    ATLAS_RESOURCE_TYPE,
    AUDIO_FILE_RESOURCE_TYPE,
    FONT_FILE_RESOURCE_TYPE,
    OTHER_RESOURCE_TYPE,
    PACK_RESOURCE_TYPE,
    SINGLE_IMAGE_RESOURCE_TYPE,
    SPINE_SKELETON_RESOURCE_TYPE,
    SPRITER_RESOURCE_TYPE,
    TILED_TILESET_RESOURCE_TYPE,
    TILED_MAP_RESOURCE_TYPE,
    TILESET_RESOURCE_TYPE,
)

try:
    from fontTools.ttLib import TTFont
except Exception:  # pragma: no cover - optional dependency
    TTFont = None

try:
    import rpack
except Exception:  # pragma: no cover - optional dependency
    rpack = None

try:
    from rectpack import MaxRectsBssf as RectpackMaxRectsBssf
    from rectpack import PackingBin as RectpackPackingBin
    from rectpack import SORT_AREA as RECTPACK_SORT_AREA
    from rectpack import newPacker as rectpack_new_packer
except Exception:  # pragma: no cover - optional dependency
    RectpackMaxRectsBssf = None
    RectpackPackingBin = None
    RECTPACK_SORT_AREA = None
    rectpack_new_packer = None

RASTER_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
SVG_EXTS = {".svg"}
FONT_EXTS = {".ttf", ".otf"}
AUDIO_EXTS = {".ogg", ".wav", ".mp3", ".flac"}
TMX_EXTS = {".tmx"}

ANIMATION_MAX_UPSCALE = 4.0
ANIMATION_MIN_LONG_EDGE = 64
ANIMATION_CROP_PADDING = 8

GID_FLIP_H = 0x80000000
GID_FLIP_V = 0x40000000
GID_FLIP_D = 0x20000000
GID_MASK = ~(GID_FLIP_H | GID_FLIP_V | GID_FLIP_D)


def _xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_children(elem: ET.Element, name: str | None = None) -> list[ET.Element]:
    children = list(elem)
    if name is None:
        return children
    return [child for child in children if _xml_name(child.tag) == name]


def _xml_first(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem:
        if _xml_name(child.tag) == name:
            return child
    return None


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


def _wrap_text_pixels(text: str, font: ImageFont.ImageFont, max_width: int, max_lines: int) -> list[str]:
    probe = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(probe)
    lines: list[str] = []
    current = ""
    index = 0
    while index < len(text):
        candidate = current + text[index]
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = candidate
            index += 1
            continue
        lines.append(current.rstrip())
        current = ""
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current.rstrip())
    if index < len(text) and lines:
        suffix = "..."
        line = lines[-1].rstrip()
        while line:
            candidate = line + suffix
            bbox = draw.textbbox((0, 0), candidate, font=font)
            if bbox[2] - bbox[0] <= max_width:
                lines[-1] = candidate
                break
            line = line[:-1].rstrip()
        if not line:
            lines[-1] = suffix
    return lines


def _default_font(size: int = 18):
    candidates = []
    if os.name == "nt":
        fonts_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates.extend(
            [
                fonts_dir / "msyh.ttc",
                fonts_dir / "simhei.ttf",
                fonts_dir / "simsun.ttc",
                fonts_dir / "arial.ttf",
            ]
        )
    candidates.append(Path("arial.ttf"))
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
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
    left = 44
    right = size - 44
    bottom = size - 44
    max_width = right - left

    def draw_lines(
        text_lines: list[str],
        y: int,
        font: ImageFont.ImageFont,
        fill: tuple[int, int, int],
        line_height: int,
        max_lines: int,
    ) -> int:
        drawn = 0
        for text in text_lines:
            for wrapped in _wrap_text_pixels(text, font, max_width, max_lines - drawn):
                if drawn >= max_lines or y + line_height > bottom:
                    return y
                draw.text((left, y), wrapped, fill=fill, font=font)
                y += line_height
                drawn += 1
        return y

    y = 36
    draw.rounded_rectangle((24, 24, size - 24, size - 24), radius=18, outline=(119, 141, 169), width=2)
    y = draw_lines([title], y, title_font, (244, 247, 250), 34, 2)
    y += 12
    if subtitle:
        y = draw_lines([subtitle], y, body_font, (194, 203, 216), 24, 3)
        y += 8
    for line in lines[:10]:
        next_y = draw_lines([line], y, body_font, (222, 230, 240), 24, 2)
        if next_y == y:
            break
        y = next_y
    image.save(output_path, format="WEBP")


def _contact_sheet_background(size: int) -> Image.Image:
    return Image.new("RGB", (size, size), (246, 247, 249))


def _animation_strip_for_sheet(image: Image.Image, max_frames: int = 4) -> Image.Image:
    frame_count = max(1, int(getattr(image, "n_frames", 1) or 1))
    indices = [int(value) for value in _sample_paths([str(idx) for idx in range(frame_count)], min(max_frames, frame_count))]
    frames: list[Image.Image] = []
    for index in indices:
        try:
            image.seek(index)
        except EOFError:
            continue
        rgba = image.convert("RGBA")
        frames.append((_crop_visible_region(rgba) or rgba).copy())

    if not frames:
        image.seek(0)
        return image.convert("RGB")

    gap = 6
    cols = min(2, len(frames))
    rows = math.ceil(len(frames) / cols)
    cell_w = max(frame.width for frame in frames)
    cell_h = max(frame.height for frame in frames)
    width = cols * cell_w + gap * (cols - 1)
    height = rows * cell_h + gap * (rows - 1)
    strip = Image.new("RGBA", (max(1, width), max(1, height)), (0, 0, 0, 0))
    for idx, frame in enumerate(frames):
        row = idx // cols
        col = idx % cols
        x = col * (cell_w + gap) + (cell_w - frame.width) // 2
        y = row * (cell_h + gap) + (cell_h - frame.height) // 2
        strip.alpha_composite(frame, (x, y))

    return strip


def _open_for_sheet(path: str) -> Image.Image:
    with Image.open(path) as img:
        if getattr(img, "is_animated", False):
            return _animation_strip_for_sheet(img)
        if img.mode in ("RGBA", "LA") or "transparency" in img.info:
            rgba = img.convert("RGBA")
            return (_crop_visible_region(rgba) or rgba).copy()
        return img.convert("RGB")


def _save_contact_sheet(image_paths: list[str], output_path: Path, size: int = 512) -> None:
    images: list[Image.Image] = []
    for path in sorted(image_paths, key=_natural_sort_key):
        try:
            images.append(_open_for_sheet(path))
        except Exception:
            continue
    if not images:
        raise ValueError("no renderable images for contact sheet")
    try:
        _save_dense_atlas_preview(
            images,
            output_path,
            size,
            background=(246, 247, 249),
        )
    finally:
        for image in images:
            image.close()


def _atlas_image_candidates(xml_path: str, raster_paths: list[str], include_fallbacks: bool = True) -> list[Path]:
    xml_file = Path(xml_path)
    try:
        root = ET.parse(xml_file).getroot()
    except Exception:
        root = None

    candidates: list[Path] = []
    if root is not None:
        for key in ("imagePath", "imagepath", "image", "source"):
            value = root.attrib.get(key, "")
            if value:
                candidates.append(xml_file.parent / value)
        for elem in root.iter():
            for key in ("imagePath", "imagepath", "image", "source"):
                value = elem.attrib.get(key, "")
                if value:
                    candidates.append(xml_file.parent / value)

    for suffix in RASTER_EXTS:
        candidates.append(xml_file.with_suffix(suffix))

    xml_stem = xml_file.stem.lower()
    for path in raster_paths:
        raster = Path(path)
        if raster.stem.lower() == xml_stem:
            candidates.append(raster)
    if include_fallbacks:
        candidates.extend(Path(path) for path in raster_paths)
    return candidates


def _existing_rasters(candidates: Iterable[Path]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.exists() and candidate.suffix.lower() in RASTER_EXTS:
            try:
                key = str(candidate.resolve()).lower()
            except OSError:
                key = str(candidate.absolute()).lower()
            if key not in seen:
                seen.add(key)
                paths.append(str(candidate))
    return paths


def _first_existing_raster(candidates: Iterable[Path]) -> str:
    paths = _existing_rasters(candidates)
    return paths[0] if paths else ""


def _atlas_declared_image_paths(xml_path: str, raster_paths: list[str]) -> list[str]:
    return _existing_rasters(_atlas_image_candidates(xml_path, raster_paths, include_fallbacks=False))


def _atlas_declared_image_path(xml_path: str, raster_paths: list[str]) -> str:
    return _first_existing_raster(_atlas_image_candidates(xml_path, raster_paths, include_fallbacks=False))


def _atlas_source_image_path(xml_path: str, raster_paths: list[str]) -> str:
    return _first_existing_raster(_atlas_image_candidates(xml_path, raster_paths, include_fallbacks=True))


def _atlas_solid_check_source_path(xml_paths: list[str], raster_paths: list[str]) -> str:
    declared_paths: list[str] = []
    seen: set[str] = set()
    for xml_path in sorted(xml_paths, key=_natural_sort_key):
        for image_path in _atlas_declared_image_paths(xml_path, raster_paths):
            try:
                key = str(Path(image_path).resolve()).lower()
            except OSError:
                key = str(Path(image_path).absolute()).lower()
            if key not in seen:
                seen.add(key)
                declared_paths.append(image_path)
    if len(declared_paths) == 1:
        return declared_paths[0]
    if len(raster_paths) == 1:
        return raster_paths[0]
    return ""


def _atlas_regions(xml_path: str) -> list[dict[str, int]]:
    try:
        root = ET.parse(xml_path).getroot()
    except Exception:
        return []

    regions = []
    for elem in root.iter():
        attrs = elem.attrib
        width = attrs.get("width") or attrs.get("w")
        height = attrs.get("height") or attrs.get("h")
        if "x" not in attrs or "y" not in attrs or not width or not height:
            continue
        try:
            x = int(float(attrs["x"]))
            y = int(float(attrs["y"]))
            w = int(float(width))
            h = int(float(height))
        except ValueError:
            continue
        if w > 0 and h > 0:
            regions.append({"x": x, "y": y, "w": w, "h": h})
    return regions


def _is_spine_atlas_path(path: str | Path) -> bool:
    path = Path(path)
    name = path.name.lower()
    return path.suffix.lower() == ".atlas" or name.endswith(".atlas.txt")


def _parse_spine_int_pair(value: str, count: int = 2) -> tuple[int, ...] | None:
    parts = [part.strip() for part in str(value or "").split(",")]
    if len(parts) < count:
        return None
    try:
        return tuple(int(float(part)) for part in parts[:count])
    except ValueError:
        return None


def _parse_spine_atlas(atlas_path: str) -> list[dict]:
    pages: list[dict] = []
    current_page: dict | None = None
    current_region: dict | None = None

    try:
        lines = Path(atlas_path).read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        lines = Path(atlas_path).read_text(encoding="utf-8", errors="ignore").splitlines()

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            current_page = None
            current_region = None
            continue

        if ":" not in stripped:
            if current_page is None:
                current_page = {"image": stripped, "props": {}, "regions": []}
                pages.append(current_page)
                current_region = None
            else:
                current_region = {"name": stripped, "page": current_page["image"]}
                current_page["regions"].append(current_region)
            continue

        if current_page is None:
            continue

        key, value = stripped.split(":", 1)
        target = current_region if current_region is not None else current_page["props"]
        target[key.strip().lower()] = value.strip()

    for page in pages:
        for region in page["regions"]:
            bounds = _parse_spine_int_pair(region.get("bounds", ""), 4)
            if bounds:
                x, y, w, h = bounds
            else:
                xy = _parse_spine_int_pair(region.get("xy", ""), 2)
                size = _parse_spine_int_pair(region.get("size", ""), 2)
                if not xy or not size:
                    continue
                x, y = xy
                w, h = size
            if w <= 0 or h <= 0:
                continue
            region["x"] = x
            region["y"] = y
            region["w"] = w
            region["h"] = h
            rotate = str(region.get("rotate", "")).strip().lower()
            region["rotated"] = rotate in {"true", "90", "1"}

    return pages


def _spine_page_image_paths(atlas_path: str, page_name: str, raster_paths: list[str]) -> list[str]:
    atlas_file = Path(atlas_path)
    page_file = Path(page_name.replace("\\", "/"))
    candidates = [
        atlas_file.parent / page_file,
        atlas_file.parent / page_file.name,
    ]
    for raster_path in raster_paths:
        raster = Path(raster_path)
        if raster.name.lower() == page_file.name.lower() or raster.stem.lower() == page_file.stem.lower():
            candidates.append(raster)
    if len(raster_paths) == 1:
        candidates.append(Path(raster_paths[0]))
    return _existing_rasters(candidates)


def _spine_region_image_path(region: dict, page_images: dict[tuple[str, str], list[str]]) -> str:
    key = (region.get("_atlas_path", ""), region.get("page", ""))
    paths = page_images.get(key, [])
    return paths[0] if paths else ""


def _spine_region_area(region: dict) -> int:
    return int(region.get("w", 0)) * int(region.get("h", 0))


def _crop_spine_region(source: Image.Image, region: dict) -> Image.Image | None:
    x = int(region.get("x", 0))
    y = int(region.get("y", 0))
    w = int(region.get("w", 0))
    h = int(region.get("h", 0))
    if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > source.width or y + h > source.height:
        return None
    crop = source.crop((x, y, x + w, y + h)).convert("RGBA")
    if region.get("rotated"):
        crop = crop.transpose(Image.Transpose.ROTATE_90)
    return _crop_visible_region(crop) or crop


SPINE_FULL_REGION_NAMES = {
    "_full",
    "full",
    "preview",
    "thumbnail",
    "thumb",
    "complete",
    "character",
}


def _select_spine_full_region(regions: list[dict]) -> dict | None:
    candidates = []
    for region in regions:
        name = str(region.get("name", "")).strip().lower()
        compact_name = re.sub(r"[\s_\-]+", "", name)
        if name in SPINE_FULL_REGION_NAMES or compact_name in SPINE_FULL_REGION_NAMES:
            candidates.append(region)
            continue
        if "preview" in name or name.endswith("_full") or name.endswith("-full"):
            candidates.append(region)
    if not candidates:
        return None
    return max(candidates, key=_spine_region_area)


def _save_spine_region_sheet(
    regions: list[dict],
    page_images: dict[tuple[str, str], list[str]],
    output_path: Path,
    size: int = 512,
) -> str:
    crops: list[Image.Image] = []
    for region in sorted(regions, key=_spine_region_area, reverse=True):
        image_path = _spine_region_image_path(region, page_images)
        if not image_path:
            continue
        try:
            with Image.open(image_path) as source:
                crop = _crop_spine_region(source.convert("RGBA"), region)
        except Exception:
            continue
        if crop is not None:
            crops.append(crop)

    if not crops:
        raise ValueError("no renderable spine atlas regions")

    _save_dense_atlas_preview(crops, output_path, size, background=(246, 247, 249))
    return ""


def _save_spine_skeleton_preview(
    atlas_paths: list[str],
    raster_paths: list[str],
    output_path: Path,
    size: int = 512,
) -> dict:
    page_images: dict[tuple[str, str], list[str]] = {}
    regions: list[dict] = []
    declared_pages: list[str] = []

    for atlas_path in sorted(atlas_paths, key=_natural_sort_key):
        for page in _parse_spine_atlas(atlas_path):
            page_name = page.get("image", "")
            image_paths = _spine_page_image_paths(atlas_path, page_name, raster_paths)
            page_images[(atlas_path, page_name)] = image_paths
            declared_pages.extend(image_paths)
            for region in page.get("regions", []):
                if {"x", "y", "w", "h"} <= set(region):
                    item = dict(region)
                    item["_atlas_path"] = atlas_path
                    regions.append(item)

    full_region = _select_spine_full_region(regions)
    if full_region is not None:
        image_path = _spine_region_image_path(full_region, page_images)
        if image_path:
            with Image.open(image_path) as source:
                crop = _crop_spine_region(source.convert("RGBA"), full_region)
            if crop is not None:
                _resize_rgba_to_preview(
                    crop,
                    output_path,
                    size,
                    (246, 247, 249),
                )
                return {
                    "strategy": PreviewStrategy.STATIC,
                    "mode": "full_region",
                    "confidence": "medium",
                    "source_path": image_path,
                }

    if regions:
        _save_spine_region_sheet(regions, page_images, output_path, size)
        return {
            "strategy": PreviewStrategy.CONTACT_SHEET,
            "mode": "atlas_regions",
            "confidence": "medium",
            "source_path": declared_pages[0] if declared_pages else "",
        }

    declared_pages = sorted(set(declared_pages), key=_natural_sort_key)
    if declared_pages:
        _save_atlas_image_grid(declared_pages, output_path, size)
        return {
            "strategy": PreviewStrategy.STATIC if len(declared_pages) == 1 else PreviewStrategy.CONTACT_SHEET,
            "mode": "atlas_pages",
            "confidence": "low",
            "source_path": declared_pages[0],
        }

    fallback_paths = sorted(raster_paths, key=_natural_sort_key)
    if fallback_paths:
        if len(fallback_paths) == 1:
            _save_existing_raster_preview(fallback_paths[0], output_path, size)
            strategy = PreviewStrategy.STATIC
        else:
            _save_contact_sheet(fallback_paths, output_path, size)
            strategy = PreviewStrategy.CONTACT_SHEET
        return {
            "strategy": strategy,
            "mode": "companion_rasters",
            "confidence": "low",
            "source_path": fallback_paths[0],
        }

    raise ValueError("no renderable spine files")


def _crop_visible_region(image: Image.Image) -> Image.Image | None:
    rgba = image.convert("RGBA")
    alpha_bbox = rgba.getchannel("A").getbbox()
    if alpha_bbox:
        left = max(0, alpha_bbox[0] - 2)
        top = max(0, alpha_bbox[1] - 2)
        right = min(rgba.width, alpha_bbox[2] + 2)
        bottom = min(rgba.height, alpha_bbox[3] + 2)
        return rgba.crop((left, top, right, bottom))

    content_bbox = rgba.convert("RGB").getbbox()
    if content_bbox:
        return rgba.crop(content_bbox)
    return None


def _resize_rgba_to_preview(
    image: Image.Image,
    output_path: Path,
    size: int = 512,
    background: tuple[int, int, int] = (246, 247, 249),
    preserve_alpha: bool = False,
    crop_visible: bool = True,
) -> None:
    visible = (_crop_visible_region(image) if crop_visible else None) or image.convert("RGBA")
    long_edge = max(visible.width, visible.height)
    if long_edge <= 0:
        raise ValueError("empty image")
    target_long_edge = min(size, max(128, long_edge))
    if target_long_edge != long_edge:
        scale = target_long_edge / long_edge
        resized_size = (
            max(1, int(round(visible.width * scale))),
            max(1, int(round(visible.height * scale))),
        )
        resample = Image.Resampling.NEAREST if scale > 1 else Image.Resampling.LANCZOS
        visible = visible.resize(resized_size, resample)
    elif long_edge > size:
        visible.thumbnail((size, size), Image.Resampling.LANCZOS)
    if preserve_alpha and visible.getchannel("A").getextrema()[0] < 255:
        visible.save(output_path, format="WEBP")
        return
    canvas = Image.new("RGB", visible.size, background)
    canvas.paste(visible, (0, 0), visible.getchannel("A"))
    canvas.save(output_path, format="WEBP")


def _transparent_pixel_matte(image: Image.Image, fallback: tuple[int, int, int] = (72, 84, 96)) -> tuple[int, int, int]:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    samples: list[tuple[int, int, int]] = []
    step_x = max(1, width // 32)
    step_y = max(1, height // 32)
    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            r, g, b, a = rgba.getpixel((x, y))
            if a <= 8:
                samples.append((r, g, b))
    if not samples:
        return fallback
    color, _count = Counter(samples).most_common(1)[0]
    if sum(color) > 690:
        return fallback
    return color


def _luminance(color: tuple[int, int, int]) -> float:
    return color[0] * 0.299 + color[1] * 0.587 + color[2] * 0.114


def _visible_pixel_luminance(image: Image.Image) -> float | None:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    total = 0.0
    count = 0
    step_x = max(1, width // 48)
    step_y = max(1, height // 48)
    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            r, g, b, a = rgba.getpixel((x, y))
            if a > 24:
                total += _luminance((r, g, b))
                count += 1
    if count == 0:
        return None
    return total / count


def _transparent_preview_background(
    image: Image.Image,
    fallback: tuple[int, int, int] = (246, 247, 249),
) -> tuple[int, int, int]:
    rgba = image.convert("RGBA")
    if rgba.getchannel("A").getextrema()[0] >= 255:
        return fallback
    matte = _transparent_pixel_matte(rgba, fallback)
    visible_luma = _visible_pixel_luminance(rgba)
    if visible_luma is None:
        return matte
    matte_luma = _luminance(matte)
    if visible_luma > 215 and matte_luma > 210:
        return (72, 84, 96)
    if visible_luma < 55 and matte_luma < 75:
        return fallback
    return matte


def _visible_content_preview_background(
    image: Image.Image,
    fallback: tuple[int, int, int] = (246, 247, 249),
) -> tuple[int, int, int]:
    rgba = image.convert("RGBA")
    if rgba.getchannel("A").getextrema()[0] >= 255:
        return fallback
    visible_luma = _visible_pixel_luminance(rgba)
    if visible_luma is None:
        return fallback
    fallback_luma = _luminance(fallback)
    if visible_luma > 215 and fallback_luma > 210:
        return (72, 84, 96)
    if visible_luma < 55 and fallback_luma < 75:
        return (246, 247, 249)
    return fallback


def _crop_significant_alpha_region(image: Image.Image, min_coverage_ratio: float = 0.02, pad: int = 4) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    alpha_min, alpha_max = alpha.getextrema()
    if alpha_min < alpha_max or alpha_min < 255:
        mask_source = alpha.point(lambda value: 1 if value > 8 else 0)
    else:
        rgb = rgba.convert("RGB")
        corners = [
            rgb.getpixel((0, 0)),
            rgb.getpixel((rgb.width - 1, 0)),
            rgb.getpixel((0, rgb.height - 1)),
            rgb.getpixel((rgb.width - 1, rgb.height - 1)),
        ]
        bg = max(set(corners), key=corners.count)
        diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg)).convert("L")
        mask_source = diff.point(lambda value: 1 if value > 8 else 0)

    bbox = mask_source.getbbox()
    if not bbox:
        return rgba

    mask = mask_source.crop(bbox)
    width, height = mask.size
    data = mask.tobytes()
    min_row = max(3, int(width * min_coverage_ratio))
    min_col = max(3, int(height * min_coverage_ratio))

    row_counts = [sum(data[y * width : (y + 1) * width]) for y in range(height)]
    col_counts = [sum(data[x + y * width] for y in range(height)) for x in range(width)]
    rows = [idx for idx, count in enumerate(row_counts) if count >= min_row]
    cols = [idx for idx, count in enumerate(col_counts) if count >= min_col]
    if not rows or not cols:
        return rgba.crop(bbox)

    left = max(0, bbox[0] + min(cols) - pad)
    top = max(0, bbox[1] + min(rows) - pad)
    right = min(rgba.width, bbox[0] + max(cols) + 1 + pad)
    bottom = min(rgba.height, bbox[1] + max(rows) + 1 + pad)
    if right <= left or bottom <= top:
        return rgba.crop(bbox)
    return rgba.crop((left, top, right, bottom))


def _save_existing_raster_preview(image_path: str, output_path: Path, size: int = 512) -> None:
    with Image.open(image_path) as image:
        rgba = image.convert("RGBA")
        _resize_rgba_to_preview(
            rgba,
            output_path,
            size,
            _transparent_preview_background(rgba),
            preserve_alpha=True,
            crop_visible=False,
        )


def _save_atlas_image_grid(image_paths: list[str], output_path: Path, size: int = 512) -> None:
    usable = [path for path in sorted(image_paths, key=_natural_sort_key) if Path(path).is_file()]
    if not usable:
        raise ValueError("no atlas source images")
    if len(usable) == 1:
        _save_existing_raster_preview(usable[0], output_path, size)
        return

    images: list[Image.Image] = []
    for path in usable:
        try:
            with Image.open(path) as image:
                images.append(image.convert("RGBA"))
        except Exception:
            continue
    if not images:
        raise ValueError("no readable atlas source images")
    _save_dense_atlas_preview(images, output_path, size)


def _source_solid_state(image_path: str) -> str:
    try:
        with Image.open(image_path) as image:
            rgba = image.convert("RGBA")
            alpha = rgba.getchannel("A")
            if alpha.getextrema()[1] == 0:
                return "transparent"

            visible_mask = alpha.point(lambda value: 255 if value > 0 else 0)
            rgb = rgba.convert("RGB")
            extrema = ImageStat.Stat(rgb, visible_mask).extrema
    except Exception:
        return ""

    if max(channel[1] for channel in extrema) == 0:
        return "black"
    if min(channel[0] for channel in extrema) == 255:
        return "white"
    return ""


def _solid_source_matches_preview_failure(image_path: str, reason: str) -> bool:
    state = _source_solid_state(image_path)
    if state == "transparent":
        return "all black" in reason or "all white" in reason
    if state == "black":
        return "all black" in reason
    if state == "white":
        return "all white" in reason
    return False


TILESET_OVERVIEW_DIR_NAMES = {
    "tilemap",
    "tilemaps",
    "tilesheet",
    "tilesheets",
    "spritesheet",
    "spritesheets",
    "sprite sheet",
    "sprite sheets",
}

TILESET_OVERVIEW_FILE_PRIORITY = (
    "tilemap",
    "tilemap_packed",
    "tilesheet",
    "tilesheet_packed",
    "spritesheet",
    "spritesheet_packed",
    "tileset",
    "sheet",
)
TILESET_SHEET_BACKGROUND = (246, 247, 249)
TILED_TILESET_REFLOW_MAX_DIRECT_ASPECT = 4.0
TILED_TILESET_REFLOW_MIN_SHORT_EDGE_RATIO = 0.25


def _tileset_name_tokens(name: str) -> list[str]:
    return [token for token in re.split(r"[\s_\-]+", name.lower().strip()) if token]


def _is_tileset_tile_dir_name(name: str) -> bool:
    text = " ".join(_tileset_name_tokens(name))
    if text in {"tile", "tiles", "small tiles"}:
        return True

    tokens = _tileset_name_tokens(name)
    if not tokens:
        return False

    def is_modifier(token: str) -> bool:
        return token.isdigit() or token in {"small", "tiny"} or bool(re.fullmatch(r"\d+x\d+", token))

    if tokens[-1] in {"tile", "tiles"} and all(is_modifier(token) for token in tokens[:-1]):
        return True
    if tokens[0] in {"tile", "tiles"} and all(is_modifier(token) for token in tokens[1:]):
        return True
    return False


def _is_overview_file_name(path: Path) -> bool:
    stem = path.stem.lower()
    if stem in TILESET_OVERVIEW_FILE_PRIORITY:
        return True
    parent = path.parent.name.lower()
    if parent in TILESET_OVERVIEW_DIR_NAMES:
        return any(token in stem for token in ("tilemap", "tilesheet", "spritesheet"))
    return False


def _tileset_overview_score(path: Path, tile_dirs: set[Path]) -> tuple[int, int, list[tuple[int, object]]]:
    stem = path.stem.lower()
    parent = path.parent
    exact_rank = next((idx for idx, name in enumerate(TILESET_OVERVIEW_FILE_PRIORITY) if stem == name), 99)
    contains_rank = next((idx for idx, name in enumerate(TILESET_OVERVIEW_FILE_PRIORITY) if name in stem), 99)
    name_rank = min(exact_rank, contains_rank + 20)
    distance = min(
        (len(set(parent.parents) ^ set(tile_dir.parents)) for tile_dir in tile_dirs),
        default=99,
    )
    return name_rank, distance, _natural_sort_key(str(path))


def _read_tileset_raster_sizes(image_paths: list[str]) -> list[tuple[str, tuple[int, int]]]:
    readable: list[tuple[str, tuple[int, int]]] = []
    for path in sorted(image_paths, key=_natural_sort_key):
        if not Path(path).exists():
            continue
        try:
            with Image.open(path) as image:
                readable.append((path, (image.width, image.height)))
        except Exception:
            continue
    return readable


def _tileset_sheet_image_paths(image_paths: list[str]) -> list[str]:
    readable = _read_tileset_raster_sizes(image_paths)
    if not readable:
        return []

    size_counts = Counter(size for _path, size in readable)
    common_size, common_count = size_counts.most_common(1)[0]
    if common_count >= 2 and common_count >= len(readable) / 2:
        return [path for path, size in readable if size == common_size]
    return [path for path, _size in readable if not _is_overview_file_name(Path(path))] or [
        path for path, _size in readable
    ]


def _find_tileset_overview_image(image_paths: list[str]) -> str | None:
    """Find an author-provided tile overview near a directory of individual tile images."""
    tile_paths = _tileset_sheet_image_paths(image_paths)
    tile_dirs = {Path(path).parent for path in tile_paths if Path(path).exists()}
    if not tile_dirs:
        tile_dirs = {Path(path).parent for path in image_paths if Path(path).exists()}
    if not tile_dirs:
        return None

    search_dirs: list[Path] = []
    for tile_dir in sorted(tile_dirs, key=lambda item: str(item).lower()):
        search_dirs.append(tile_dir)
        for directory in [tile_dir, *list(tile_dir.parents)[:3]]:
            if not _is_tileset_tile_dir_name(directory.name):
                continue
            base = directory.parent
            for name in TILESET_OVERVIEW_DIR_NAMES:
                search_dirs.append(base / name)
                search_dirs.append(base / name.title())

    existing_dirs = []
    seen_dirs = set()
    for directory in search_dirs:
        key = str(directory).lower()
        if key in seen_dirs or not directory.is_dir():
            continue
        seen_dirs.add(key)
        existing_dirs.append(directory.resolve())

    candidates: list[Path] = []
    seen_candidates: set[str] = set()
    for directory in existing_dirs:
        for child in directory.iterdir():
            if child.is_file() and child.suffix.lower() in RASTER_EXTS and _is_overview_file_name(child):
                key = str(child.resolve()).lower()
                if key not in seen_candidates:
                    seen_candidates.add(key)
                    candidates.append(child)

    if not candidates:
        return None

    verified = []
    for path in candidates:
        stats = _match_tileset_overview_image(path, image_paths)
        if stats["valid"]:
            verified.append((path, stats))
    if not verified:
        return None

    verified = sorted(
        verified,
        key=lambda item: (
            -int(item[1]["matched_tiles"]),
            _tileset_overview_score(item[0], tile_dirs),
        ),
    )
    return str(verified[0][0])


def _parse_tiled_trans_color(value: str | None) -> tuple[int, int, int] | None:
    raw = str(value or "").strip().lower().lstrip("#")
    if len(raw) == 3:
        raw = "".join(char * 2 for char in raw)
    if len(raw) != 6 or not re.fullmatch(r"[0-9a-f]{6}", raw):
        return None
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def _tiled_tileset_transparency_keys(tsx_paths: list[str]) -> dict[str, tuple[int, int, int]]:
    keys: dict[str, tuple[int, int, int]] = {}
    for tsx_path in tsx_paths:
        try:
            root = ET.parse(tsx_path).getroot()
        except Exception:
            continue
        base_path = Path(tsx_path)
        image = _xml_first(root, "image")
        if image is not None:
            color = _parse_tiled_trans_color(image.attrib.get("trans"))
            source = image.attrib.get("source", "")
            if color is not None and source:
                path = _resolve_tiled_path(base_path, source)
                keys[str(path).lower()] = color
        for tile_elem in _xml_children(root, "tile"):
            image = _xml_first(tile_elem, "image")
            if image is None:
                continue
            color = _parse_tiled_trans_color(image.attrib.get("trans"))
            source = image.attrib.get("source", "")
            if color is not None and source:
                path = _resolve_tiled_path(base_path, source)
                keys[str(path).lower()] = color
    return keys


def _apply_color_key_transparency(image: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    rgba = image.convert("RGBA")
    rgb = rgba.convert("RGB")
    diff = ImageChops.difference(rgb, Image.new("RGB", rgba.size, color))
    mask = diff.convert("L").point(lambda value: 255 if value == 0 else 0)
    alpha = rgba.getchannel("A")
    alpha.paste(0, mask=mask)
    rgba.putalpha(alpha)
    return rgba


def _prepare_tiled_tileset_images(
    image_paths: list[str],
    tsx_paths: list[str],
    temp_dir: Path,
) -> list[str]:
    keys = _tiled_tileset_transparency_keys(tsx_paths)
    if not keys:
        return image_paths

    prepared: list[str] = []
    temp_dir.mkdir(parents=True, exist_ok=True)
    for path_text in image_paths:
        path = Path(path_text)
        color = keys.get(str(path.resolve()).lower() if path.exists() else str(path.absolute()).lower())
        if color is None:
            prepared.append(path_text)
            continue
        try:
            with Image.open(path) as image:
                keyed = _apply_color_key_transparency(image, color)
                digest = hashlib.md5((str(path.resolve()).lower() + str(color)).encode("utf-8")).hexdigest()[:16]
                target = temp_dir / f"{digest}_{path.stem}.png"
                keyed.save(target)
                prepared.append(str(target))
        except Exception:
            prepared.append(path_text)
    return prepared


def _int_attr(attrs: dict, key: str, default: int = 0) -> int:
    try:
        return int(float(attrs.get(key, default) or default))
    except (TypeError, ValueError):
        return default


def _scaled_preview_size(width: int, height: int, size: int = 512) -> tuple[int, int]:
    long_edge = max(width, height)
    if long_edge <= 0:
        return 0, 0
    target_long_edge = min(size, max(128, long_edge))
    if target_long_edge == long_edge:
        return max(1, width), max(1, height)
    scale = target_long_edge / long_edge
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _raster_preview_needs_tiled_reflow(image_path: str, size: int = 512) -> bool:
    try:
        with Image.open(image_path) as image:
            preview_w, preview_h = _scaled_preview_size(image.width, image.height, size)
    except Exception:
        return False
    short_edge = min(preview_w, preview_h)
    long_edge = max(preview_w, preview_h)
    if short_edge <= 0:
        return False
    min_short_edge = max(32, int(round(size * TILED_TILESET_REFLOW_MIN_SHORT_EDGE_RATIO)))
    return short_edge < min_short_edge and (long_edge / short_edge) >= TILED_TILESET_REFLOW_MAX_DIRECT_ASPECT


def _tiled_tileset_should_reflow(
    tsx_paths: list[str],
    image_paths: list[str],
    overview_path: str | None,
    size: int = 512,
) -> bool:
    if not tsx_paths:
        return False
    if overview_path:
        return _raster_preview_needs_tiled_reflow(overview_path, size)
    usable = [path for path in image_paths if Path(path).is_file()]
    return len(usable) == 1 and _raster_preview_needs_tiled_reflow(usable[0], size)


def _tile_has_visible_pixels(tile: Image.Image) -> bool:
    try:
        return tile.convert("RGBA").getchannel("A").getbbox() is not None
    except Exception:
        return False


def _open_tiled_tile_image(path: Path, trans: tuple[int, int, int] | None = None) -> Image.Image | None:
    try:
        with Image.open(path) as image:
            tile = image.convert("RGBA")
    except Exception:
        return None
    if trans is not None:
        tile = _apply_color_key_transparency(tile, trans)
    return tile


def _append_tiled_sheet_tiles(root: ET.Element, tsx_path: Path, image_elem: ET.Element, tiles: list[Image.Image]) -> None:
    tile_w = _int_attr(root.attrib, "tilewidth")
    tile_h = _int_attr(root.attrib, "tileheight")
    if tile_w <= 0 or tile_h <= 0:
        return

    source = image_elem.attrib.get("source", "")
    image_path = _resolve_tiled_path(tsx_path, source) if source else Path()
    source_image = _open_tiled_tile_image(image_path, _parse_tiled_trans_color(image_elem.attrib.get("trans")))
    if source_image is None:
        return

    spacing = _int_attr(root.attrib, "spacing")
    margin = _int_attr(root.attrib, "margin")
    columns = _int_attr(root.attrib, "columns")
    if columns <= 0:
        columns = max(1, (source_image.width - margin * 2 + spacing) // max(1, tile_w + spacing))
    max_rows = max(1, (source_image.height - margin * 2 + spacing) // max(1, tile_h + spacing))
    max_tiles = max(0, columns * max_rows)
    tilecount = _int_attr(root.attrib, "tilecount", max_tiles)
    if tilecount <= 0 or tilecount > max_tiles:
        tilecount = max_tiles

    try:
        for local_id in range(tilecount):
            col = local_id % columns
            row = local_id // columns
            x = margin + col * (tile_w + spacing)
            y = margin + row * (tile_h + spacing)
            if x < 0 or y < 0 or x + tile_w > source_image.width or y + tile_h > source_image.height:
                continue
            tile = source_image.crop((x, y, x + tile_w, y + tile_h))
            if _tile_has_visible_pixels(tile):
                tiles.append(tile)
            else:
                tile.close()
    finally:
        source_image.close()


def _append_tiled_collection_tiles(root: ET.Element, tsx_path: Path, tiles: list[Image.Image]) -> None:
    items: list[tuple[int, ET.Element]] = []
    for tile_elem in _xml_children(root, "tile"):
        image_elem = _xml_first(tile_elem, "image")
        if image_elem is None:
            continue
        items.append((_int_attr(tile_elem.attrib, "id"), image_elem))

    for _tile_id, image_elem in sorted(items, key=lambda item: item[0]):
        source = image_elem.attrib.get("source", "")
        image_path = _resolve_tiled_path(tsx_path, source) if source else Path()
        tile = _open_tiled_tile_image(image_path, _parse_tiled_trans_color(image_elem.attrib.get("trans")))
        if tile is None:
            continue
        if _tile_has_visible_pixels(tile):
            tiles.append(tile)
        else:
            tile.close()


def _extract_tiled_tileset_tiles(tsx_paths: list[str]) -> list[Image.Image]:
    tiles: list[Image.Image] = []
    for tsx_path_text in sorted(tsx_paths, key=_natural_sort_key):
        tsx_path = Path(tsx_path_text)
        try:
            root = ET.parse(tsx_path).getroot()
        except Exception:
            continue
        image_elem = _xml_first(root, "image")
        if image_elem is not None:
            _append_tiled_sheet_tiles(root, tsx_path, image_elem, tiles)
        _append_tiled_collection_tiles(root, tsx_path, tiles)
    return tiles


def _save_tiled_tileset_reflow_preview(tsx_paths: list[str], output_path: Path, size: int = 512) -> None:
    tiles = _extract_tiled_tileset_tiles(tsx_paths)
    if not tiles:
        raise ValueError("no tiles extracted from tiled tileset")

    try:
        cell_w = max(tile.width for tile in tiles)
        cell_h = max(tile.height for tile in tiles)
        aspect = max(1, cell_w) / max(1, cell_h)
        cols = max(1, math.ceil(math.sqrt(len(tiles) / aspect)))
        rows = math.ceil(len(tiles) / cols)
        sheet = Image.new("RGBA", (cols * cell_w, rows * cell_h), (*TILESET_SHEET_BACKGROUND, 0))

        for idx, tile in enumerate(tiles):
            if tile.width > cell_w or tile.height > cell_h:
                tile = tile.copy()
                tile.thumbnail((cell_w, cell_h), Image.Resampling.NEAREST)
            row = idx // cols
            col = idx % cols
            x = col * cell_w + (cell_w - tile.width) // 2
            y = row * cell_h + (cell_h - tile.height) // 2
            sheet.alpha_composite(tile, (x, y))

        scale = min(size / max(1, sheet.width), size / max(1, sheet.height))
        if scale != 1:
            sheet = sheet.resize(
                (
                    max(1, int(round(sheet.width * scale))),
                    max(1, int(round(sheet.height * scale))),
                ),
                Image.Resampling.NEAREST,
            )

        canvas = Image.new("RGB", (size, size), TILESET_SHEET_BACKGROUND)
        x = (size - sheet.width) // 2
        y = (size - sheet.height) // 2
        canvas.paste(sheet, (x, y), sheet.getchannel("A"))
        canvas.save(output_path, format="WEBP")
    finally:
        for tile in tiles:
            tile.close()


def _median_int(values: list[int]) -> int:
    values = sorted(values)
    return values[len(values) // 2] if values else 0


def _common_tile_size(image_paths: list[str]) -> tuple[int, int] | None:
    sample = _sample_paths(sorted(image_paths, key=_natural_sort_key), min(96, len(image_paths)))
    sizes: list[tuple[int, int]] = []
    for path in sample:
        try:
            with Image.open(path) as image:
                sizes.append((image.width, image.height))
        except Exception:
            continue
    if not sizes:
        return None
    return Counter(sizes).most_common(1)[0][0]


def _tile_digest(image: Image.Image) -> bytes | None:
    rgba = image.convert("RGBA")
    if not rgba.getchannel("A").getbbox():
        return None
    return hashlib.blake2b(rgba.tobytes(), digest_size=12).digest()


def _build_tile_digests(tile_paths: list[str], tile_size: tuple[int, int]) -> set[bytes]:
    digests: set[bytes] = set()
    for path in sorted(tile_paths, key=_natural_sort_key):
        try:
            with Image.open(path) as image:
                if image.size != tile_size:
                    continue
                digest = _tile_digest(image)
        except Exception:
            continue
        if digest is not None:
            digests.add(digest)
    return digests


def _required_overview_matches(tile_count: int) -> int:
    if tile_count <= 0:
        return 0
    if tile_count < 8:
        return max(1, math.ceil(tile_count * 0.75))
    if tile_count < 32:
        return math.ceil(tile_count * 0.6)
    return min(32, max(12, math.ceil(tile_count * 0.35)))


def _grid_offsets(length: int, cell: int) -> list[int]:
    if cell <= 0 or length < cell:
        return []
    if length % cell == 0:
        return [0]
    return list(range(min(cell, length - cell + 1)))


def _grid_layouts(length: int, cell: int) -> list[tuple[int, int, int]]:
    if cell <= 0 or length < cell:
        return []
    max_spacing = min(8, max(0, cell // 2), max(0, length - cell))
    exact: list[tuple[int, int, int]] = []
    for spacing in range(max_spacing + 1):
        step = cell + spacing
        for offset in range(0, min(step, length - cell + 1)):
            remaining = length - offset
            if remaining >= cell and (remaining + spacing) % step == 0:
                exact.append((offset, step, spacing))
    if exact:
        return exact
    return [(0, cell + spacing, spacing) for spacing in range(min(3, max_spacing) + 1)]


def _match_tileset_overview_image(path: Path, tile_paths: list[str]) -> dict[str, object]:
    tile_size = _common_tile_size(tile_paths)
    if tile_size is None:
        return {"valid": False, "matched_tiles": 0, "total_tiles": 0, "required_matches": 0}

    tile_digests = _build_tile_digests(tile_paths, tile_size)
    required = _required_overview_matches(len(tile_digests))
    if not tile_digests or required <= 0:
        return {"valid": False, "matched_tiles": 0, "total_tiles": len(tile_digests), "required_matches": required}

    tile_w, tile_h = tile_size
    try:
        with Image.open(path) as source:
            overview = source.convert("RGBA")
    except Exception:
        return {"valid": False, "matched_tiles": 0, "total_tiles": len(tile_digests), "required_matches": required}

    if not overview.getchannel("A").getbbox():
        return {"valid": False, "matched_tiles": 0, "total_tiles": len(tile_digests), "required_matches": required}

    best_matches: set[bytes] = set()
    best_offset = (0, 0)
    best_spacing = (0, 0)
    best_cells = 0
    for offset_y, step_y, spacing_y in _grid_layouts(overview.height, tile_h):
        for offset_x, step_x, spacing_x in _grid_layouts(overview.width, tile_w):
            matches: set[bytes] = set()
            nonempty_cells = 0
            for top in range(offset_y, overview.height - tile_h + 1, step_y):
                for left in range(offset_x, overview.width - tile_w + 1, step_x):
                    digest = _tile_digest(overview.crop((left, top, left + tile_w, top + tile_h)))
                    if digest is None:
                        continue
                    nonempty_cells += 1
                    if digest in tile_digests:
                        matches.add(digest)
            if len(matches) > len(best_matches):
                best_matches = matches
                best_offset = (offset_x, offset_y)
                best_spacing = (spacing_x, spacing_y)
                best_cells = nonempty_cells
            if len(best_matches) >= required:
                break
        if len(best_matches) >= required:
            break

    return {
        "valid": len(best_matches) >= required,
        "matched_tiles": len(best_matches),
        "total_tiles": len(tile_digests),
        "required_matches": required,
        "tile_width": tile_w,
        "tile_height": tile_h,
        "offset": best_offset,
        "spacing": best_spacing,
        "nonempty_cells": best_cells,
    }


def _is_tileset_overview_image(path: Path, tile_paths: list[str]) -> bool:
    return bool(_match_tileset_overview_image(path, tile_paths)["valid"])


def _tileset_should_use_dense_atlas(sizes: list[tuple[int, int]]) -> bool:
    if len(sizes) < 2 or len(set(sizes)) == 1:
        return False
    areas = [max(1, width * height) for width, height in sizes]
    max_area = max(areas)
    min_area = min(areas)
    cell_area = max(width for width, _ in sizes) * max(height for _, height in sizes) * len(sizes)
    total_area = sum(areas)
    area_ratio = max_area / float(max(1, min_area))
    cell_waste_ratio = cell_area / float(max(1, total_area))
    return area_ratio >= 4.0 or cell_waste_ratio >= 2.5


def _save_tileset_sheet(image_paths: list[str], output_path: Path, size: int = 512, use_all: bool = False) -> None:
    if use_all:
        usable = [path for path in sorted(image_paths, key=_natural_sort_key) if Path(path).exists()]
    else:
        usable = _tileset_sheet_image_paths(image_paths)
        if not usable:
            usable = [path for path in sorted(image_paths, key=_natural_sort_key) if Path(path).exists()]
    if not usable:
        raise ValueError("no raster files for tileset sheet")

    sizes: list[tuple[int, int]] = []
    for path in usable:
        try:
            with Image.open(path) as image:
                sizes.append((image.width, image.height))
        except Exception:
            continue
    if not sizes:
        raise ValueError("no readable raster files for tileset sheet")

    if _tileset_should_use_dense_atlas(sizes):
        images: list[Image.Image] = []
        for path in usable:
            try:
                with Image.open(path) as image:
                    images.append(image.convert("RGBA"))
            except Exception:
                continue
        if images:
            _save_dense_atlas_preview(images, output_path, size, background=TILESET_SHEET_BACKGROUND)
            return

    cell_w = max(width for width, _ in sizes)
    cell_h = max(height for _, height in sizes)
    aspect = max(1, cell_w) / max(1, cell_h)
    cols = max(1, math.ceil(math.sqrt(len(usable) / aspect)))
    rows = math.ceil(len(usable) / cols)
    sheet = Image.new("RGBA", (cols * cell_w, rows * cell_h), (*TILESET_SHEET_BACKGROUND, 0))

    for idx, path in enumerate(usable):
        try:
            with Image.open(path) as image:
                tile = image.convert("RGBA")
        except Exception:
            continue
        if tile.width > cell_w or tile.height > cell_h:
            tile.thumbnail((cell_w, cell_h), Image.Resampling.NEAREST)
        row = idx // cols
        col = idx % cols
        x = col * cell_w + (cell_w - tile.width) // 2
        y = row * cell_h + (cell_h - tile.height) // 2
        sheet.alpha_composite(tile, (x, y))

    visible = _crop_visible_region(sheet) or sheet
    long_edge = max(visible.width, visible.height)
    if long_edge <= 0:
        raise ValueError("empty tileset sheet")
    scale = size / long_edge
    if scale != 1:
        resample = Image.Resampling.NEAREST if scale > 1 else Image.Resampling.LANCZOS
        visible = visible.resize(
            (
                max(1, int(round(visible.width * scale))),
                max(1, int(round(visible.height * scale))),
            ),
            resample,
        )
    canvas = Image.new("RGB", visible.size, TILESET_SHEET_BACKGROUND)
    canvas.paste(visible, (0, 0), visible.getchannel("A"))
    canvas.save(output_path, format="WEBP")


def _save_atlas_sheet(xml_paths: list[str], raster_paths: list[str], output_path: Path, size: int = 512) -> None:
    declared_image_paths: list[str] = []
    seen_declared: set[str] = set()
    for xml_path in sorted(xml_paths, key=_natural_sort_key):
        for image_path in _atlas_declared_image_paths(xml_path, raster_paths):
            try:
                key = str(Path(image_path).resolve()).lower()
            except OSError:
                key = str(Path(image_path).absolute()).lower()
            if key not in seen_declared:
                seen_declared.add(key)
                declared_image_paths.append(image_path)
    if declared_image_paths:
        _save_atlas_image_grid(declared_image_paths, output_path, size)
        return

    for xml_path in sorted(xml_paths, key=_natural_sort_key):
        image_path = _atlas_source_image_path(xml_path, raster_paths)
        regions = _atlas_regions(xml_path)
        if not image_path or not regions:
            continue

        crops: list[Image.Image] = []
        with Image.open(image_path) as source:
            source = source.convert("RGBA")
            for region in regions:
                x, y, w, h = region["x"], region["y"], region["w"], region["h"]
                if x < 0 or y < 0 or x + w > source.width or y + h > source.height:
                    continue
                crop = _crop_visible_region(source.crop((x, y, x + w, y + h)))
                if crop is not None:
                    crops.append(crop)

        if not crops:
            continue

        _save_dense_atlas_preview(crops, output_path, size, background=(246, 247, 249))
        return

    fallback_paths = sorted(raster_paths, key=_natural_sort_key)
    if not fallback_paths:
        raise ValueError("no renderable atlas image files")
    _save_contact_sheet(fallback_paths, output_path, size)


def _resolve_entity_resource_path(entity: ResourceProcessingEntity, suffixes: set[str]) -> list[str]:
    found = [
        f.file_path
        for f in entity.files
        if Path(f.file_path).suffix.lower() in suffixes and Path(f.file_path).exists()
    ]
    if found:
        return found

    rel = entity.resource_path.replace("/", os.sep).strip()
    if not rel:
        return []
    candidates = []
    if entity.source_directory:
        source_dir = Path(entity.source_directory)
        candidates.extend(
            [
                source_dir / rel,
                source_dir / Path(rel).name,
                source_dir.parent / rel,
            ]
        )
        for candidate in candidates:
            if candidate.exists() and candidate.suffix.lower() in suffixes:
                return [str(candidate)]
    for file_info in entity.files:
        file_path = Path(file_info.file_path)
        for parent in file_path.parents:
            candidate = parent / rel
            if candidate.exists() and candidate.suffix.lower() in suffixes:
                return [str(candidate)]
    return []


def _decode_tiled_data(
    data_elem: ET.Element,
    expected_count: int,
    encoding: str | None = None,
    compression: str | None = None,
) -> list[int]:
    encoding = (encoding or data_elem.attrib.get("encoding", "")).lower()
    compression = (compression or data_elem.attrib.get("compression", "")).lower()
    if encoding == "csv":
        text = "".join(data_elem.itertext())
        values = [int(part.strip()) for part in text.replace("\n", "").split(",") if part.strip()]
        return values[:expected_count]
    if encoding == "base64":
        raw = base64.b64decode("".join(data_elem.itertext()).strip())
        if compression == "gzip":
            raw = gzip.decompress(raw)
        elif compression == "zlib":
            raw = zlib.decompress(raw)
        values = [int.from_bytes(raw[i : i + 4], "little") for i in range(0, min(len(raw), expected_count * 4), 4)]
        return values

    values = []
    for tile in data_elem.findall("tile"):
        values.append(int(tile.attrib.get("gid", "0") or 0))
    return values[:expected_count]


def _load_tiled_tilesets(tmx_path: str) -> tuple[ET.Element, list[dict]]:
    root = ET.parse(tmx_path).getroot()
    tilesets = []
    for elem in _xml_children(root, "tileset"):
        firstgid = int(elem.attrib.get("firstgid", "1") or 1)
        source = elem.attrib.get("source", "")
        tsx_path = Path(tmx_path).parent / source if source else Path(tmx_path)
        ts_root = ET.parse(tsx_path).getroot() if source else elem
        tile_w = int(ts_root.attrib.get("tilewidth", root.attrib.get("tilewidth", "0")) or 0)
        tile_h = int(ts_root.attrib.get("tileheight", root.attrib.get("tileheight", "0")) or 0)
        columns = int(ts_root.attrib.get("columns", "0") or 0)
        tilecount = int(ts_root.attrib.get("tilecount", "0") or 0)
        spacing = int(ts_root.attrib.get("spacing", "0") or 0)
        margin = int(ts_root.attrib.get("margin", "0") or 0)
        if tile_w <= 0 or tile_h <= 0:
            continue
        tileoffset = _xml_first(ts_root, "tileoffset")
        offset_x = int(float(tileoffset.attrib.get("x", "0") or 0)) if tileoffset is not None else 0
        offset_y = int(float(tileoffset.attrib.get("y", "0") or 0)) if tileoffset is not None else 0

        image = _xml_first(ts_root, "image")
        if image is not None:
            image_source = image.attrib.get("source", "")
            image_path = (tsx_path.parent / image_source).resolve() if image_source else Path()
            if not image_path.exists():
                continue
            source_img = Image.open(image_path).convert("RGBA")
            if columns <= 0:
                columns = max(1, (source_img.width - margin * 2 + spacing) // (tile_w + spacing))
            if tilecount <= 0:
                rows = max(1, (source_img.height - margin * 2 + spacing) // (tile_h + spacing))
                tilecount = columns * rows
            tilesets.append(
                {
                    "firstgid": firstgid,
                    "tilewidth": tile_w,
                    "tileheight": tile_h,
                    "columns": columns,
                    "tilecount": tilecount,
                    "spacing": spacing,
                    "margin": margin,
                    "offset_x": offset_x,
                    "offset_y": offset_y,
                    "image": source_img,
                }
            )
            continue

        tile_images: dict[int, Image.Image] = {}
        for tile_elem in _xml_children(ts_root, "tile"):
            try:
                tile_id = int(tile_elem.attrib.get("id", "0") or 0)
            except ValueError:
                continue
            image_elem = _xml_first(tile_elem, "image")
            if image_elem is None:
                continue
            image_source = image_elem.attrib.get("source", "")
            image_path = (tsx_path.parent / image_source).resolve() if image_source else Path()
            if not image_path.exists() or image_path.suffix.lower() not in RASTER_EXTS:
                continue
            try:
                tile_images[tile_id] = Image.open(image_path).convert("RGBA")
            except Exception:
                continue
        if tile_images:
            tilesets.append(
                {
                    "firstgid": firstgid,
                    "tilewidth": tile_w,
                    "tileheight": tile_h,
                    "columns": columns,
                    "tilecount": max(tilecount, max(tile_images) + 1),
                    "spacing": spacing,
                    "margin": margin,
                    "offset_x": offset_x,
                    "offset_y": offset_y,
                    "tile_images": tile_images,
                }
            )
    tilesets.sort(key=lambda item: item["firstgid"])
    return root, tilesets


def _tile_for_gid(tilesets: list[dict], raw_gid: int) -> Image.Image | None:
    gid = raw_gid & GID_MASK
    if gid == 0:
        return None
    tileset = None
    for item in tilesets:
        if item["firstgid"] <= gid:
            tileset = item
        else:
            break
    if tileset is None:
        return None
    local_id = gid - tileset["firstgid"]
    if local_id < 0 or local_id >= tileset["tilecount"]:
        return None
    if "tile_images" in tileset:
        source = tileset["tile_images"].get(local_id)
        if source is None:
            return None
        image = source.copy()
    else:
        col = local_id % tileset["columns"]
        row = local_id // tileset["columns"]
        x = tileset["margin"] + col * (tileset["tilewidth"] + tileset["spacing"])
        y = tileset["margin"] + row * (tileset["tileheight"] + tileset["spacing"])
        image = tileset["image"].crop((x, y, x + tileset["tilewidth"], y + tileset["tileheight"]))
    if raw_gid & GID_FLIP_D:
        image = image.transpose(Image.Transpose.TRANSPOSE)
    if raw_gid & GID_FLIP_H:
        image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if raw_gid & GID_FLIP_V:
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return image


def _iter_tiled_layer_cells(data: ET.Element, width: int, height: int) -> list[tuple[int, int, int]]:
    encoding = data.attrib.get("encoding", "")
    compression = data.attrib.get("compression", "")
    cells: list[tuple[int, int, int]] = []
    chunks = _xml_children(data, "chunk")
    if chunks:
        for chunk in chunks:
            chunk_x = int(chunk.attrib.get("x", "0") or 0)
            chunk_y = int(chunk.attrib.get("y", "0") or 0)
            chunk_w = int(chunk.attrib.get("width", "0") or 0)
            chunk_h = int(chunk.attrib.get("height", "0") or 0)
            values = _decode_tiled_data(chunk, chunk_w * chunk_h, encoding, compression)
            for idx, raw_gid in enumerate(values):
                if raw_gid:
                    cells.append((chunk_x + idx % chunk_w, chunk_y + idx // chunk_w, raw_gid))
        return cells

    values = _decode_tiled_data(data, width * height)
    for idx, raw_gid in enumerate(values):
        if raw_gid:
            cells.append((idx % width, idx // width, raw_gid))
    return cells


def _resolve_tiled_path(base_path: Path, source: str) -> Path:
    source_path = Path(source)
    if source_path.is_absolute():
        return source_path
    return (base_path.parent / source_path).resolve()


def _tiled_tileset_image_candidates(tileset_root: ET.Element, base_path: Path) -> list[Path]:
    candidates: list[Path] = []
    image = _xml_first(tileset_root, "image")
    if image is not None:
        source = image.attrib.get("source", "")
        if source:
            candidates.append(_resolve_tiled_path(base_path, source))

    for tile_elem in _xml_children(tileset_root, "tile"):
        image_elem = _xml_first(tile_elem, "image")
        if image_elem is None:
            continue
        source = image_elem.attrib.get("source", "")
        if source:
            candidates.append(_resolve_tiled_path(base_path, source))
    return candidates


def _tiled_tileset_image_paths(tmx_path: str) -> list[str]:
    try:
        root = ET.parse(tmx_path).getroot()
    except Exception:
        return []

    candidates: list[Path] = []
    map_path = Path(tmx_path)
    for elem in _xml_children(root, "tileset"):
        source = elem.attrib.get("source", "")
        if source:
            tsx_path = _resolve_tiled_path(map_path, source)
            try:
                ts_root = ET.parse(tsx_path).getroot()
            except Exception:
                continue
            candidates.extend(_tiled_tileset_image_candidates(ts_root, tsx_path))
        else:
            candidates.extend(_tiled_tileset_image_candidates(elem, map_path))
    return _existing_rasters(candidates)


def _tmx_has_renderable_content(tmx_path: str) -> bool:
    try:
        root = ET.parse(tmx_path).getroot()
    except Exception:
        return True

    map_w = int(root.attrib.get("width", "0") or 0)
    map_h = int(root.attrib.get("height", "0") or 0)
    def visit(parent: ET.Element) -> bool:
        for elem in _xml_children(parent):
            if elem.attrib.get("visible", "1") == "0":
                continue
            tag = _xml_name(elem.tag)
            if tag == "group" and visit(elem):
                return True
            if tag == "imagelayer":
                image = _xml_first(elem, "image")
                if image is not None and image.attrib.get("source", ""):
                    return True
            if tag == "objectgroup" and _xml_children(elem):
                return True
            if tag != "layer":
                continue
            data = _xml_first(elem, "data")
            if data is None:
                continue
            width = int(elem.attrib.get("width", map_w) or map_w)
            height = int(elem.attrib.get("height", map_h) or map_h)
            try:
                if _iter_tiled_layer_cells(data, width, height):
                    return True
            except Exception:
                return True
        return False

    return visit(root)


def _tile_pixel_position(
    orientation: str,
    cell_x: int,
    cell_y: int,
    tile: Image.Image,
    tile_w: int,
    tile_h: int,
    offset_x: int = 0,
    offset_y: int = 0,
) -> tuple[int, int]:
    if orientation == "isometric":
        base_x = (cell_x - cell_y) * tile_w / 2
        base_y = (cell_x + cell_y) * tile_h / 2
        x = int(round(base_x + (tile_w - tile.width) / 2 + offset_x))
        y = int(round(base_y + tile_h - tile.height + offset_y))
        return x, y
    x = cell_x * tile_w + offset_x
    y = cell_y * tile_h + tile_h - tile.height + offset_y
    return x, y


def _render_tmx_map(tmx_path: str) -> Image.Image | None:
    root, tilesets = _load_tiled_tilesets(tmx_path)
    if not tilesets:
        return None
    orientation = root.attrib.get("orientation", "orthogonal")
    if orientation not in {"orthogonal", "isometric"}:
        return None

    map_w = int(root.attrib.get("width", "0") or 0)
    map_h = int(root.attrib.get("height", "0") or 0)
    tile_w = int(root.attrib.get("tilewidth", "0") or 0)
    tile_h = int(root.attrib.get("tileheight", "0") or 0)
    if map_w <= 0 or map_h <= 0 or tile_w <= 0 or tile_h <= 0:
        return None

    layers: list[dict] = []
    pixel_bounds: list[tuple[int, int, int, int]] = []
    tile_cache: dict[int, Image.Image | None] = {}

    for elem in root:
        tag = _xml_name(elem.tag)
        if elem.attrib.get("visible", "1") == "0":
            continue
        if tag == "imagelayer":
            image_elem = _xml_first(elem, "image")
            if image_elem is None:
                continue
            source = image_elem.attrib.get("source", "")
            image_path = Path(tmx_path).parent / source
            if not image_path.exists():
                continue
            with Image.open(image_path) as layer_img:
                layer = layer_img.convert("RGBA")
            offset_x = int(float(elem.attrib.get("offsetx", "0") or 0))
            offset_y = int(float(elem.attrib.get("offsety", "0") or 0))
            layers.append({"type": "image", "image": layer, "offset_x": offset_x, "offset_y": offset_y, "opacity": float(elem.attrib.get("opacity", "1") or 1)})
            pixel_bounds.append((offset_x, offset_y, offset_x + layer.width, offset_y + layer.height))
            continue
        if tag != "layer":
            continue
        width = int(elem.attrib.get("width", map_w) or map_w)
        height = int(elem.attrib.get("height", map_h) or map_h)
        data = _xml_first(elem, "data")
        if data is None:
            continue
        cells = _iter_tiled_layer_cells(data, width, height)
        if not cells:
            continue
        offset_x = int(float(elem.attrib.get("offsetx", "0") or 0))
        offset_y = int(float(elem.attrib.get("offsety", "0") or 0))
        layers.append({"type": "tile", "cells": cells, "offset_x": offset_x, "offset_y": offset_y, "opacity": float(elem.attrib.get("opacity", "1") or 1)})
        for cell_x, cell_y, raw_gid in cells:
            if raw_gid not in tile_cache:
                tile_cache[raw_gid] = _tile_for_gid(tilesets, raw_gid)
            tile = tile_cache[raw_gid]
            if tile is None:
                continue
            x, y = _tile_pixel_position(orientation, cell_x, cell_y, tile, tile_w, tile_h, offset_x, offset_y)
            pixel_bounds.append((x, y, x + tile.width, y + tile.height))

    if not pixel_bounds:
        return None

    min_x = min(box[0] for box in pixel_bounds)
    min_y = min(box[1] for box in pixel_bounds)
    max_x = max(box[2] for box in pixel_bounds)
    max_y = max(box[3] for box in pixel_bounds)
    if max_x <= min_x or max_y <= min_y:
        return None

    canvas = Image.new("RGBA", (max_x - min_x, max_y - min_y), (0, 0, 0, 0))
    for layer_info in layers:
        layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        if layer_info["type"] == "image":
            image = layer_info["image"]
            layer.alpha_composite(image, (layer_info["offset_x"] - min_x, layer_info["offset_y"] - min_y))
        else:
            for cell_x, cell_y, raw_gid in layer_info["cells"]:
                tile = tile_cache.get(raw_gid)
                if tile is None:
                    continue
                x, y = _tile_pixel_position(
                    orientation,
                    cell_x,
                    cell_y,
                    tile,
                    tile_w,
                    tile_h,
                    layer_info["offset_x"],
                    layer_info["offset_y"],
                )
                layer.alpha_composite(tile, (x - min_x, y - min_y))
        opacity = layer_info["opacity"]
        if opacity < 1:
            alpha = layer.getchannel("A").point(lambda value: int(value * opacity))
            layer.putalpha(alpha)
        canvas.alpha_composite(layer)
    return canvas


def _tmxrasterizer_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_value = os.environ.get("TMXRASTERIZER", "")
    if env_value:
        candidates.append(Path(env_value))
    which = shutil.which("tmxrasterizer")
    if which:
        candidates.append(Path(which))
    if os.name == "nt":
        for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if not root:
                continue
            candidates.extend(
                [
                    Path(root) / "Tiled" / "tmxrasterizer.exe",
                    Path(root) / "Tiled Map Editor" / "tmxrasterizer.exe",
                ]
            )
    return candidates


def _render_tmx_with_tmxrasterizer(tmx_path: str, size: int = 512) -> Image.Image | None:
    executable = next((candidate for candidate in _tmxrasterizer_candidates() if candidate.is_file()), None)
    if executable is None:
        return None

    try:
        with tempfile.TemporaryDirectory(prefix="tmx_preview_") as tmp:
            output_path = Path(tmp) / "preview.png"
            result = subprocess.run(
                [str(executable), "--size", str(size), tmx_path, str(output_path)],
                capture_output=True,
                text=True,
                timeout=45,
            )
            if result.returncode != 0 or not output_path.is_file():
                return None
            with Image.open(output_path) as image:
                rendered = image.convert("RGBA")
                return rendered.crop(_frame_visible_bbox(rendered))
    except Exception:
        return None


def _save_tiled_map_preview(
    tmx_paths: list[str],
    raster_paths: list[str],
    output_path: Path,
    size: int = 512,
) -> tuple[PreviewStrategy, str]:
    empty_tileset_paths: list[str] = []
    for tmx_path in sorted(tmx_paths, key=_natural_sort_key):
        if not _tmx_has_renderable_content(tmx_path):
            empty_tileset_paths.extend(_tiled_tileset_image_paths(tmx_path))
            continue

        rendered = _render_tmx_with_tmxrasterizer(tmx_path, size)
        if rendered is not None:
            rendered = _crop_significant_alpha_region(rendered)
            _resize_rgba_to_preview(rendered, output_path, size)
            return PreviewStrategy.STATIC, "composed"

        try:
            rendered = _render_tmx_map(tmx_path)
        except Exception:
            rendered = None
        if rendered is not None:
            rendered = _crop_significant_alpha_region(rendered)
            _resize_rgba_to_preview(rendered, output_path, size)
            return PreviewStrategy.STATIC, "composed"

    if empty_tileset_paths:
        _save_tileset_sheet(_existing_rasters(Path(path) for path in empty_tileset_paths), output_path, size, use_all=True)
        return PreviewStrategy.CONTACT_SHEET, "empty_map_tileset"

    if tmx_paths:
        raise ValueError("no renderable tiled map data")

    usable = [p for p in raster_paths if Path(p).exists()]
    if usable:
        with Image.open(sorted(usable, key=_natural_sort_key)[0]) as image:
            _resize_rgba_to_preview(image.convert("RGBA"), output_path, size)
            return PreviewStrategy.STATIC, "source_raster"
    raise ValueError("no renderable tiled map files")


def _tsx_image_info(tsx_path: str) -> dict:
    try:
        root = ET.parse(tsx_path).getroot()
    except Exception:
        return {}
    image = _xml_first(root, "image")
    if image is None:
        return {}
    source = image.attrib.get("source", "")
    image_path = (Path(tsx_path).parent / source).resolve()
    if not image_path.exists():
        return {}
    return {
        "image_path": str(image_path),
        "tilewidth": int(root.attrib.get("tilewidth", "0") or 0),
        "tileheight": int(root.attrib.get("tileheight", "0") or 0),
        "columns": int(root.attrib.get("columns", "0") or 0),
        "tilecount": int(root.attrib.get("tilecount", "0") or 0),
    }


def _tsx_collection_image_paths(tsx_path: str) -> list[str]:
    try:
        root = ET.parse(tsx_path).getroot()
    except Exception:
        return []
    items: list[tuple[int, str]] = []
    for tile_elem in _xml_children(root, "tile"):
        try:
            tile_id = int(tile_elem.attrib.get("id", "0") or 0)
        except ValueError:
            continue
        image = _xml_first(tile_elem, "image")
        if image is None:
            continue
        source = image.attrib.get("source", "")
        path = (Path(tsx_path).parent / source).resolve() if source else Path()
        if path.exists() and path.suffix.lower() in RASTER_EXTS:
            items.append((tile_id, str(path)))
    return [path for _, path in sorted(items)]


def _pack_item_preview_path(item: str | dict) -> str:
    if isinstance(item, dict):
        return str(item.get("preview_path") or item.get("path") or "")
    return str(item)


def _pack_item_source_path(item: str | dict) -> str:
    if not isinstance(item, dict):
        return str(item)
    if str(item.get("resource_type") or "") == SINGLE_IMAGE_RESOURCE_TYPE:
        source_paths = item.get("source_paths") or []
        if isinstance(source_paths, list):
            for path in source_paths:
                path_text = str(path or "")
                if path_text and Path(path_text).suffix.lower() in RASTER_EXTS and Path(path_text).is_file():
                    return path_text
    return _pack_item_preview_path(item)


def _parse_svg_length(value: str) -> float | None:
    text = str(value or "").strip()
    if not text or text.endswith("%"):
        return None
    match = re.match(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))", text)
    if not match:
        return None
    try:
        size = float(match.group(1))
    except ValueError:
        return None
    if not math.isfinite(size) or size <= 0:
        return None
    return size


def _svg_declared_size(path: str) -> tuple[int, int] | None:
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return None

    width = _parse_svg_length(root.attrib.get("width", ""))
    height = _parse_svg_length(root.attrib.get("height", ""))
    if width and height:
        return max(1, int(round(width))), max(1, int(round(height)))

    view_box = root.attrib.get("viewBox") or root.attrib.get("viewbox") or ""
    values = re.split(r"[\s,]+", view_box.strip())
    if len(values) == 4:
        try:
            view_width = float(values[2])
            view_height = float(values[3])
        except ValueError:
            return None
        if math.isfinite(view_width) and math.isfinite(view_height) and view_width > 0 and view_height > 0:
            return max(1, int(round(view_width))), max(1, int(round(view_height)))
    return None


PACK_STRIP_ASPECT_RATIO = 4.0
PACK_STRIP_MAX_FRAMES = 6
PACK_STRIP_GAP = 4


def _pack_declared_frame_size(item: str | dict, image_size: tuple[int, int]) -> tuple[int, int] | None:
    text = _pack_item_source_path(item)
    if isinstance(item, dict):
        text = " ".join(
            [
                text,
                str(item.get("resource_path") or ""),
                str(item.get("title") or ""),
            ]
        )
    match = re.search(r"\((\d{1,4})\s*[xX]\s*(\d{1,4})\)", text)
    if not match:
        return None
    frame_w = int(match.group(1))
    frame_h = int(match.group(2))
    image_w, image_h = image_size
    if frame_w <= 0 or frame_h <= 0:
        return None
    if image_w >= frame_w and image_h >= frame_h:
        return frame_w, frame_h
    return None


def _pack_strip_axis_and_frame_size(item: str | dict, image_size: tuple[int, int]) -> tuple[str, int, int] | None:
    image_w, image_h = image_size
    if image_w <= 0 or image_h <= 0:
        return None
    aspect = image_w / image_h
    if aspect >= PACK_STRIP_ASPECT_RATIO:
        frame_size = _pack_declared_frame_size(item, image_size) or (image_h, image_h)
        frame_w, frame_h = frame_size
        if frame_w <= 0 or frame_h <= 0:
            return None
        frame_count = image_w // frame_w
        if frame_count >= 3 and frame_h <= image_h:
            return "horizontal", frame_w, frame_h
    if aspect <= 1 / PACK_STRIP_ASPECT_RATIO:
        frame_size = _pack_declared_frame_size(item, image_size) or (image_w, image_w)
        frame_w, frame_h = frame_size
        if frame_w <= 0 or frame_h <= 0:
            return None
        frame_count = image_h // frame_h
        if frame_count >= 3 and frame_w <= image_w:
            return "vertical", frame_w, frame_h
    return None


def _pack_strip_keyframe_preview_size(item: str | dict, image_size: tuple[int, int]) -> tuple[int, int] | None:
    strip = _pack_strip_axis_and_frame_size(item, image_size)
    if not strip:
        return None
    axis, frame_w, frame_h = strip
    image_w, image_h = image_size
    frame_count = image_w // frame_w if axis == "horizontal" else image_h // frame_h
    if frame_count <= PACK_STRIP_MAX_FRAMES:
        return None

    item_count = min(PACK_STRIP_MAX_FRAMES, frame_count)
    layout_options = []
    for cols in range(1, min(3, item_count) + 1):
        rows = math.ceil(item_count / cols)
        width = cols * frame_w + (cols - 1) * PACK_STRIP_GAP
        height = rows * frame_h + (rows - 1) * PACK_STRIP_GAP
        layout_options.append((abs(width - height), rows * cols - item_count, width * height, cols, rows))
    _, _, _, cols, rows = min(layout_options)
    return (
        max(1, cols * frame_w + (cols - 1) * PACK_STRIP_GAP),
        max(1, rows * frame_h + (rows - 1) * PACK_STRIP_GAP),
    )


def _pack_strip_keyframe_preview(item: str | dict, path: str) -> Image.Image | None:
    try:
        with Image.open(path) as image:
            rgba = image.convert("RGBA")
    except Exception:
        return None

    strip = _pack_strip_axis_and_frame_size(item, rgba.size)
    if not strip:
        return None
    axis, frame_w, frame_h = strip
    frame_count = rgba.width // frame_w if axis == "horizontal" else rgba.height // frame_h
    if frame_count <= PACK_STRIP_MAX_FRAMES:
        return None

    indices = [int(value) for value in _sample_paths([str(idx) for idx in range(frame_count)], min(PACK_STRIP_MAX_FRAMES, frame_count))]
    frames: list[Image.Image] = []
    for index in indices:
        if axis == "horizontal":
            box = (index * frame_w, 0, min(rgba.width, (index + 1) * frame_w), min(rgba.height, frame_h))
        else:
            box = (0, index * frame_h, min(rgba.width, frame_w), min(rgba.height, (index + 1) * frame_h))
        frame = _crop_visible_region(rgba.crop(box))
        if frame is not None:
            frames.append(frame)
    if not frames:
        return None

    item_count = len(frames)
    layout_options = []
    for cols in range(1, min(3, item_count) + 1):
        rows = math.ceil(item_count / cols)
        cell_w = max(frame.width for frame in frames)
        cell_h = max(frame.height for frame in frames)
        width = cols * cell_w + (cols - 1) * PACK_STRIP_GAP
        height = rows * cell_h + (rows - 1) * PACK_STRIP_GAP
        layout_options.append((abs(width - height), rows * cols - item_count, width * height, cols, rows))
    _, _, _, cols, rows = min(layout_options)
    cell_w = max(frame.width for frame in frames)
    cell_h = max(frame.height for frame in frames)
    preview = Image.new(
        "RGBA",
        (
            cols * cell_w + (cols - 1) * PACK_STRIP_GAP,
            rows * cell_h + (rows - 1) * PACK_STRIP_GAP,
        ),
        (0, 0, 0, 0),
    )
    for idx, frame in enumerate(frames):
        row = idx // cols
        col = idx % cols
        x = col * (cell_w + PACK_STRIP_GAP) + (cell_w - frame.width) // 2
        y = row * (cell_h + PACK_STRIP_GAP) + (cell_h - frame.height) // 2
        preview.alpha_composite(frame, (x, y))
    return preview


def _crop_pack_collage_raster_image(image: Image.Image) -> Image.Image:
    has_alpha = image.mode in ("RGBA", "LA") or "transparency" in image.info
    if not has_alpha:
        return image.convert("RGB")

    rgba = image.convert("RGBA")
    bbox = rgba.getchannel("A").getbbox()
    if not bbox:
        return rgba.copy()

    full_area = max(1, rgba.width * rgba.height)
    crop_area = max(1, bbox[2] - bbox[0]) * max(1, bbox[3] - bbox[1])
    if crop_area / float(full_area) >= PACK_COLLAGE_CROP_FULL_RATIO:
        return rgba.copy()

    left = max(0, bbox[0] - PACK_COLLAGE_CROP_PADDING)
    top = max(0, bbox[1] - PACK_COLLAGE_CROP_PADDING)
    right = min(rgba.width, bbox[2] + PACK_COLLAGE_CROP_PADDING)
    bottom = min(rgba.height, bbox[3] + PACK_COLLAGE_CROP_PADDING)
    return rgba.crop((left, top, right, bottom))


def _open_pack_collage_raster_image(path: str) -> Image.Image:
    with Image.open(path) as image:
        if getattr(image, "is_animated", False):
            return _animation_strip_for_sheet(image)
        return _crop_pack_collage_raster_image(image)


def _open_pack_collage_image(item: str | dict, *, prefer_source: bool) -> Image.Image:
    primary = _pack_item_source_path(item) if prefer_source else _pack_item_preview_path(item)
    fallback = _pack_item_preview_path(item)
    for path in (primary, fallback):
        if not path or not Path(path).is_file():
            continue
        try:
            ext = Path(path).suffix.lower()
            if ext in SVG_EXTS:
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp:
                    temp_path = temp.name
                try:
                    if not _try_rasterize_svg(path, temp_path):
                        continue
                    return _open_pack_collage_raster_image(temp_path)
                finally:
                    Path(temp_path).unlink(missing_ok=True)
            if prefer_source and ext in RASTER_EXTS:
                strip_preview = _pack_strip_keyframe_preview(item, path)
                if strip_preview is not None:
                    return strip_preview
            if ext in RASTER_EXTS:
                return _open_pack_collage_raster_image(path)
            return _open_for_sheet(path)
        except Exception:
            continue
    return Image.new("RGB", (1, 1), (246, 247, 249))


def _paste_image(sheet: Image.Image, image: Image.Image, xy: tuple[int, int]) -> None:
    if image.mode == "RGBA":
        sheet.paste(image, xy, image.getchannel("A"))
    else:
        sheet.paste(image.convert("RGB"), xy)


@dataclass(frozen=True)
class _AtlasRect:
    index: int
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class _AtlasPage:
    rects: tuple[_AtlasRect, ...]
    used_width: int
    used_height: int


def _scaled_atlas_sizes(sizes: list[tuple[int, int]], scale: float) -> list[tuple[int, int]]:
    return [
        (max(1, int(round(source_w * scale))), max(1, int(round(source_h * scale))))
        for source_w, source_h in sizes
    ]


def _atlas_page_gap(max_width: int) -> int:
    return 8 if max_width >= 768 else 6 if max_width >= 384 else 4


def _layout_atlas_page_from_positions(
    positions: dict[int, tuple[int, int, int, int]],
    used_width: int,
    used_height: int,
) -> _AtlasPage:
    rects = tuple(
        _AtlasRect(index=idx, x=x, y=y, width=width, height=height)
        for idx, (x, y, width, height) in sorted(positions.items())
    )
    return _AtlasPage(rects=rects, used_width=used_width, used_height=used_height)


def _layout_atlas_pages_with_rectpack(
    sizes: list[tuple[int, int]],
    max_width: int,
    max_height: int,
    gap: int,
    scale: float,
    *,
    multiple_pages: bool,
) -> list[_AtlasPage] | None:
    if (
        rectpack_new_packer is None
        or RectpackPackingBin is None
        or RectpackMaxRectsBssf is None
        or RECTPACK_SORT_AREA is None
    ):
        return None
    if not sizes:
        return []

    scaled_sizes = _scaled_atlas_sizes(sizes, scale)
    if any(width > max_width or height > max_height for width, height in scaled_sizes):
        return None

    try:
        packer = rectpack_new_packer(
            bin_algo=RectpackPackingBin.Global,
            pack_algo=RectpackMaxRectsBssf,
            sort_algo=RECTPACK_SORT_AREA,
            rotation=False,
        )
        packer.add_bin(max_width + gap, max_height + gap, count=float("inf") if multiple_pages else 1)
        for idx, (width, height) in enumerate(scaled_sizes):
            packer.add_rect(width + gap, height + gap, rid=idx)
        packer.pack()
        packed_rects = packer.rect_list()
    except Exception:
        return None

    if len(packed_rects) != len(scaled_sizes):
        return None

    page_rects: dict[int, list[_AtlasRect]] = {}
    packed_indexes: set[int] = set()
    for bin_index, x, y, _packed_width, _packed_height, rid in packed_rects:
        try:
            idx = int(rid)
            page_index = int(bin_index)
            x = int(x)
            y = int(y)
        except (TypeError, ValueError):
            return None
        if idx < 0 or idx >= len(scaled_sizes) or idx in packed_indexes:
            return None
        width, height = scaled_sizes[idx]
        if x < 0 or y < 0 or x + width > max_width or y + height > max_height:
            return None
        packed_indexes.add(idx)
        page_rects.setdefault(page_index, []).append(
            _AtlasRect(index=idx, x=x, y=y, width=width, height=height)
        )

    if len(packed_indexes) != len(scaled_sizes):
        return None

    pages: list[_AtlasPage] = []
    for page_index in sorted(page_rects):
        rects = tuple(sorted(page_rects[page_index], key=lambda rect: rect.index))
        used_width = max((rect.x + rect.width for rect in rects), default=0)
        used_height = max((rect.y + rect.height for rect in rects), default=0)
        pages.append(_AtlasPage(rects=rects, used_width=used_width, used_height=used_height))
    return pages


def _try_pack_atlas_layout(
    sizes: list[tuple[int, int]],
    max_width: int,
    max_height: int,
    gap: int,
    scale: float,
) -> tuple[dict[int, tuple[int, int, int, int]], int, int] | None:
    scaled_sizes = _scaled_atlas_sizes(sizes, scale)
    if any(width > max_width or height > max_height for width, height in scaled_sizes):
        return None

    if rpack is not None:
        packed_sizes = [(width + gap, height + gap) for width, height in scaled_sizes]
        packed_area = sum(width * height for width, height in packed_sizes)
        largest_width = max((width for width, _ in packed_sizes), default=1)
        balanced_width = min(max_width + gap, max(largest_width, int(math.sqrt(max(1, packed_area)) * 1.2)))
        constraint_widths = [
            max_width + gap,
            balanced_width,
            int((max_width + gap) * 0.95),
            int((max_width + gap) * 0.90),
            int((max_width + gap) * 0.85),
            int((max_width + gap) * 0.80),
            int((max_width + gap) * 0.75),
        ]
        constraints = []
        seen_constraint_widths: set[int] = set()
        for constraint_width in constraint_widths:
            constraint_width = min(max_width + gap, max(largest_width, constraint_width))
            if constraint_width in seen_constraint_widths:
                continue
            seen_constraint_widths.add(constraint_width)
            constraints.append((constraint_width, max_height + gap))
        best_rpack: tuple[tuple[float, float, float, int], dict[int, tuple[int, int, int, int]], int, int] | None = None
        scaled_area = sum(width * height for width, height in scaled_sizes)
        for constraint_width, constraint_height in constraints:
            try:
                packed_positions = rpack.pack(
                    packed_sizes,
                    max_width=constraint_width,
                    max_height=constraint_height,
                )
                positions = {
                    idx: (int(x), int(y), scaled_sizes[idx][0], scaled_sizes[idx][1])
                    for idx, (x, y) in enumerate(packed_positions)
                }
                if any(x < 0 or y < 0 or x + width > max_width or y + height > max_height for x, y, width, height in positions.values()):
                    continue
                used_width = max((x + width for x, _, width, _ in positions.values()), default=0)
                used_height = max((y + height for _, y, _, height in positions.values()), default=0)
                max_used_edge = max(1, used_width, used_height)
                min_used_edge = max(1, min(used_width, used_height))
                preview_content_fill = scaled_area / float(max_used_edge * max_used_edge)
                preview_bbox_fill = min_used_edge / float(max_used_edge)
                density = scaled_area / float(max(1, used_width * used_height))
                score = (preview_content_fill, preview_bbox_fill, density, -max_used_edge)
                if best_rpack is None or score > best_rpack[0]:
                    best_rpack = (score, positions, used_width, used_height)
            except Exception:
                continue
        if best_rpack is not None:
            _, positions, used_width, used_height = best_rpack
            return positions, used_width, used_height

    order = sorted(range(len(sizes)), key=lambda idx: (-(sizes[idx][0] * sizes[idx][1]), -max(sizes[idx]), idx))
    positions: dict[int, tuple[int, int, int, int]] = {}
    free_rects: list[tuple[int, int, int, int]] = [(0, 0, max_width, max_height)]

    for idx in order:
        width, height = scaled_sizes[idx]

        placement = _find_pack_atlas_placement(free_rects, width, height)
        if placement is None:
            return None
        x, y, _, _ = placement
        positions[idx] = (x, y, width, height)
        occupied = (x, y, width + gap, height + gap)
        free_rects = _split_pack_atlas_free_rects(free_rects, occupied)

    used_width = max((x + width for x, _, width, _ in positions.values()), default=0)
    used_height = max((y + height for _, y, _, height in positions.values()), default=0)
    return positions, used_width, used_height


def _find_pack_atlas_placement(
    free_rects: list[tuple[int, int, int, int]],
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    best = None
    for rect in free_rects:
        x, y, free_w, free_h = rect
        if width > free_w or height > free_h:
            continue
        short_side = min(free_w - width, free_h - height)
        long_side = max(free_w - width, free_h - height)
        score = (short_side, long_side, y, x)
        if best is None or score < best[0]:
            best = (score, rect)
    return best[1] if best else None


def _rect_contains_pack_atlas_rect(
    outer: tuple[int, int, int, int],
    inner: tuple[int, int, int, int],
) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ix >= ox and iy >= oy and ix + iw <= ox + ow and iy + ih <= oy + oh


def _prune_pack_atlas_free_rects(rects: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    pruned: list[tuple[int, int, int, int]] = []
    for idx, rect in enumerate(rects):
        if rect[2] <= 0 or rect[3] <= 0:
            continue
        if any(
            idx != other_idx and _rect_contains_pack_atlas_rect(other, rect)
            for other_idx, other in enumerate(rects)
        ):
            continue
        pruned.append(rect)
    return pruned


def _split_pack_atlas_free_rects(
    free_rects: list[tuple[int, int, int, int]],
    occupied: tuple[int, int, int, int],
) -> list[tuple[int, int, int, int]]:
    ox, oy, ow, oh = occupied
    result: list[tuple[int, int, int, int]] = []
    for fx, fy, fw, fh in free_rects:
        if ox >= fx + fw or ox + ow <= fx or oy >= fy + fh or oy + oh <= fy:
            result.append((fx, fy, fw, fh))
            continue

        if ox > fx:
            result.append((fx, fy, ox - fx, fh))
        if ox + ow < fx + fw:
            result.append((ox + ow, fy, fx + fw - (ox + ow), fh))
        if oy > fy:
            result.append((fx, fy, fw, oy - fy))
        if oy + oh < fy + fh:
            result.append((fx, oy + oh, fw, fy + fh - (oy + oh)))
    return _prune_pack_atlas_free_rects(result)


def _pack_atlas_max_scale(sizes: list[tuple[int, int]], max_width: int, max_height: int) -> float:
    max_edge = max(max(width, height) for width, height in sizes)
    if max_edge <= 96:
        return min(4.0, max_width / max_edge, max_height / max_edge)
    return 1.0


def _layout_atlas_single_page_at_scale(
    sizes: list[tuple[int, int]],
    max_width: int,
    max_height: int,
    gap: int,
    scale: float,
) -> _AtlasPage | None:
    pages = _layout_atlas_pages_with_rectpack(
        sizes,
        max_width,
        max_height,
        gap,
        scale,
        multiple_pages=False,
    )
    if pages:
        return pages[0]

    fallback = _try_pack_atlas_layout(sizes, max_width, max_height, gap, scale)
    if fallback is None:
        return None
    positions, used_width, used_height = fallback
    return _layout_atlas_page_from_positions(positions, used_width, used_height)


def _multi_page_atlas_scale(sizes: list[tuple[int, int]], max_width: int, max_height: int) -> float:
    total_area = max(1, sum(width * height for width, height in sizes))
    max_source_w = max(width for width, _ in sizes)
    max_source_h = max(height for _, height in sizes)
    max_scale = _pack_atlas_max_scale(sizes, max_width, max_height)
    fit_scale = min(max_width / max_source_w, max_height / max_source_h)
    max_allowed = max(0.01, min(max_scale, fit_scale))
    page_area = max(1, max_width * max_height)
    target_pages = max(
        1,
        min(
            PACK_ATLAS_MAX_PREVIEW_PAGES,
            math.ceil(total_area / float(page_area * PACK_ATLAS_TARGET_FILL)),
        ),
    )
    target_area = page_area * PACK_ATLAS_TARGET_FILL * target_pages

    return min(max_allowed, math.sqrt(target_area / total_area))


def _layout_dense_atlas_pages_for_sizes(
    sizes: list[tuple[int, int]],
    max_width: int,
    max_height: int,
    gap: int,
    *,
    multiple_pages: bool,
) -> list[_AtlasPage]:
    if not sizes:
        return []

    if multiple_pages:
        scale = _multi_page_atlas_scale(sizes, max_width, max_height)
        pages = _layout_atlas_pages_with_rectpack(
            sizes,
            max_width,
            max_height,
            gap,
            scale,
            multiple_pages=True,
        )
        if pages and len(pages) <= PACK_ATLAS_MAX_PREVIEW_PAGES:
            return pages
        if pages and len(pages) > PACK_ATLAS_MAX_PREVIEW_PAGES:
            best_pages: list[_AtlasPage] | None = None
            low = 0.01
            high = scale
            for _ in range(18):
                mid = (low + high) / 2
                candidate = _layout_atlas_pages_with_rectpack(
                    sizes,
                    max_width,
                    max_height,
                    gap,
                    mid,
                    multiple_pages=True,
                )
                if candidate and len(candidate) <= PACK_ATLAS_MAX_PREVIEW_PAGES:
                    best_pages = candidate
                    low = mid
                else:
                    high = mid
            if best_pages:
                return best_pages

    total_area = max(1, sum(width * height for width, height in sizes))
    max_source_w = max(width for width, _ in sizes)
    max_source_h = max(height for _, height in sizes)
    max_scale = _pack_atlas_max_scale(sizes, max_width, max_height)
    upper = min(max_width / max_source_w, max_height / max_source_h)
    upper = min(upper, math.sqrt((max_width * max_height) / (total_area * 1.08)) * 1.5)
    upper = min(upper, max_scale)
    upper = max(0.01, upper)

    best: _AtlasPage | None = None
    low = 0.0
    high = upper
    for _ in range(24):
        mid = (low + high) / 2
        candidate = _layout_atlas_single_page_at_scale(sizes, max_width, max_height, gap, mid)
        if candidate:
            best = candidate
            low = mid
        else:
            high = mid
    if best:
        return [best]

    scale = min(max_width / max_source_w, max_height / max_source_h, max_scale)
    fallback = _layout_atlas_single_page_at_scale(sizes, max_width, max_height, gap, max(scale, 0.01))
    if fallback:
        return [fallback]
    return [
        _AtlasPage(
            rects=(
                _AtlasRect(
                    index=0,
                    x=0,
                    y=0,
                    width=max_width,
                    height=max(1, int(max_source_h * max_width / max_source_w)),
                ),
            ),
            used_width=max_width,
            used_height=max_height,
        )
    ]


def _best_pack_atlas_layout(
    sizes: list[tuple[int, int]],
    max_width: int,
    max_height: int,
    gap: int,
) -> tuple[dict[int, tuple[int, int, int, int]], int, int]:
    page = _layout_dense_atlas_pages_for_sizes(
        sizes,
        max_width,
        max_height,
        gap,
        multiple_pages=False,
    )[0]
    positions = {rect.index: (rect.x, rect.y, rect.width, rect.height) for rect in page.rects}
    return positions, page.used_width, page.used_height


def _compose_dense_atlas_page(
    prepared: list[Image.Image],
    page: _AtlasPage,
    max_width: int,
    max_height: int,
    output_size: int,
    background: tuple[int, int, int] | None,
    *,
    pad_to_output: bool = True,
) -> Image.Image:
    used_width = page.used_width
    used_height = page.used_height
    offset_x = max(0, (max_width - used_width) // 2)
    offset_y = max(0, (max_height - used_height) // 2)
    atlas = Image.new("RGBA", (max_width, max_height), (0, 0, 0, 0))

    for rect in page.rects:
        if rect.index < 0 or rect.index >= len(prepared):
            continue
        image = prepared[rect.index]
        x, y, width, height = rect.x, rect.y, rect.width, rect.height
        tile = image
        if tile.width != width or tile.height != height:
            resample = Image.Resampling.NEAREST if width >= tile.width and height >= tile.height else Image.Resampling.LANCZOS
            tile = tile.resize((width, height), resample)
        atlas.alpha_composite(tile, (offset_x + x, offset_y + y))

    crop_box = (
        offset_x,
        offset_y,
        min(max_width, offset_x + max(1, used_width)),
        min(max_height, offset_y + max(1, used_height)),
    )
    atlas = atlas.crop(crop_box)
    atlas.thumbnail((output_size, output_size), Image.Resampling.LANCZOS)

    if not pad_to_output:
        if background is None:
            return atlas
        fitted = Image.new("RGB", atlas.size, background)
        fitted.paste(atlas, (0, 0), atlas.getchannel("A"))
        return fitted

    if background is None:
        fitted = Image.new("RGBA", (output_size, output_size), (0, 0, 0, 0))
        fitted.alpha_composite(atlas, ((output_size - atlas.width) // 2, (output_size - atlas.height) // 2))
        return fitted

    fitted = Image.new("RGB", (output_size, output_size), background)
    fitted.paste(atlas, ((output_size - atlas.width) // 2, (output_size - atlas.height) // 2), atlas.getchannel("A"))
    return fitted


def _render_dense_atlas_pages(
    images: list[Image.Image],
    output_size: int,
    *,
    layout_size: int | None = None,
    background: tuple[int, int, int] | None = None,
    multiple_pages: bool = True,
    pad_to_output: bool = True,
) -> list[Image.Image]:
    prepared = [
        image.convert("RGBA")
        for image in images
        if image.width > 0 and image.height > 0
    ]
    if not prepared:
        raise ValueError("no images for dense atlas")

    max_width = max(1, layout_size or DENSE_ATLAS_LAYOUT_SIZE, output_size)
    max_height = max_width
    gap = _atlas_page_gap(max_width)
    sizes = [(max(1, image.width), max(1, image.height)) for image in prepared]
    pages = _layout_dense_atlas_pages_for_sizes(
        sizes,
        max_width,
        max_height,
        gap,
        multiple_pages=multiple_pages,
    )
    if not pages:
        raise ValueError("no atlas pages generated")
    return [
        _compose_dense_atlas_page(
            prepared,
            page,
            max_width,
            max_height,
            output_size,
            background,
            pad_to_output=pad_to_output,
        )
        for page in pages
        if page.rects
    ]


def _render_dense_atlas_image(
    images: list[Image.Image],
    output_size: int,
    *,
    layout_size: int | None = None,
    background: tuple[int, int, int] | None = None,
    pad_to_output: bool = True,
) -> Image.Image:
    return _render_dense_atlas_pages(
        images,
        output_size,
        layout_size=layout_size,
        background=background,
        multiple_pages=False,
        pad_to_output=pad_to_output,
    )[0]


def _save_dense_atlas_preview(
    images: list[Image.Image],
    output_path: Path,
    size: int = 512,
    *,
    background: tuple[int, int, int] | None = None,
    layout_size: int | None = None,
) -> None:
    atlas = _render_dense_atlas_image(
        images,
        size,
        layout_size=layout_size,
        background=background,
    )
    atlas.save(output_path, format="WEBP")


def _paste_pack_atlas_collage(
    sheet: Image.Image,
    images: list[Image.Image],
    collage_left: int,
    collage_top: int,
    collage_size: int,
    layout_size: int | None = None,
) -> None:
    if not images:
        return
    atlas = _render_dense_atlas_image(
        images,
        collage_size,
        layout_size=layout_size or collage_size,
    )
    _paste_image(sheet, atlas, (collage_left, collage_top))


def _save_pack_collage(
    image_paths: list[str | dict],
    output_path: Path,
    title: str,
    size: int = 512,
    max_items: int = 9,
    grid_cols: int | None = None,
) -> None:
    sheet = _contact_sheet_background(size)
    draw = ImageDraw.Draw(sheet)
    body_font = _default_font(16)
    draw.rounded_rectangle((20, 20, size - 20, size - 20), radius=14, outline=(180, 186, 196), width=2)

    collage_left = 32
    collage_top = 32
    collage_size = size - collage_top * 2
    item_count = min(len(image_paths), max_items)
    items = list(image_paths[:item_count])
    if items:
        layout_images = [_open_pack_collage_image(item, prefer_source=True) for item in items]
        try:
            layout_size = PACK_ATLAS_LAYOUT_SIZE if len(layout_images) > 1 else collage_size
            atlas = _render_dense_atlas_image(
                layout_images,
                size,
                layout_size=layout_size,
                background=(246, 247, 249),
                pad_to_output=False,
            )
            atlas.save(output_path, format="WEBP")
            return
        finally:
            for image in layout_images:
                image.close()
    if not image_paths:
        draw.text((36, collage_top + 12), "No child previews available", fill=(96, 103, 112), font=body_font)
    sheet.save(output_path, format="WEBP")


def _preview_has_pack_content(path: str) -> bool:
    try:
        with Image.open(path) as image:
            preview = image.convert("RGB")
            preview.thumbnail((64, 64))
            extrema = preview.getextrema()
    except Exception:
        return False
    return any((hi - lo) > 6 for lo, hi in extrema)


PACK_AUDIO_RESOURCE_TYPE = AUDIO_FILE_RESOURCE_TYPE
PACK_SOURCE_FORMAT_DIRS = {"vector", "vectors", "svg", "svgs", "source", "sources", "swf"}
PACK_ALTERNATE_FORMAT_COVERAGE_RATIO = 0.85
PACK_GENERATED_COLLAGE_SIZE = 1024
PACK_DIRECT_IMAGE_PAGE_SIZE = 48
PACK_DIRECT_IMAGE_SAMPLE_LIMIT = PACK_DIRECT_IMAGE_PAGE_SIZE * 2
PACK_DIRECT_IMAGE_ICON_SAMPLE_LIMIT = PACK_DIRECT_IMAGE_SAMPLE_LIMIT
PACK_MIXED_IMAGE_PAGE_SIZE = PACK_DIRECT_IMAGE_PAGE_SIZE
PACK_MIXED_IMAGE_SAMPLE_LIMIT = PACK_MIXED_IMAGE_PAGE_SIZE * 2
PACK_AUDIO_SAMPLE_GROUP_LIMIT = 8
DENSE_ATLAS_LAYOUT_SIZE = 1024
PACK_ATLAS_LAYOUT_SIZE = DENSE_ATLAS_LAYOUT_SIZE
PACK_ATLAS_TARGET_FILL = 0.86
PACK_ATLAS_MAX_PREVIEW_PAGES = 4
PACK_COLLAGE_CROP_PADDING = 4
PACK_COLLAGE_CROP_FULL_RATIO = 0.95
PACK_SHOWCASE_MIN_RECORDS = 4
PACK_SHOWCASE_BACKGROUND_LIMIT = 1
PACK_SHOWCASE_TERRAIN_LIMIT = 8
PACK_SHOWCASE_CHARACTER_LIMIT = 2
PACK_SHOWCASE_ENEMY_LIMIT = 5
PACK_SHOWCASE_ITEM_LIMIT = 7
PACK_SHOWCASE_HAZARD_LIMIT = 4
PACK_SHOWCASE_PROP_LIMIT = 4
PACK_SHOWCASE_UI_LIMIT = 5
PACK_SHOWCASE_CATEGORY_KEYWORDS = {
    "background": {
        "background",
        "backgrounds",
        "backdrop",
        "cloud",
        "clouds",
        "environment",
        "parallax",
        "sky",
    },
    "terrain": {
        "block",
        "blocks",
        "dirt",
        "floor",
        "floors",
        "grass",
        "ground",
        "grounds",
        "platform",
        "platforms",
        "terrain",
        "tile",
        "tiles",
    },
    "character": {
        "adventurer",
        "boy",
        "character",
        "characters",
        "girl",
        "hero",
        "heroes",
        "knight",
        "mage",
        "ninja",
        "player",
        "robot",
        "warrior",
    },
    "enemy": {
        "alien",
        "aliens",
        "bat",
        "bee",
        "boss",
        "enemies",
        "enemy",
        "ghost",
        "monster",
        "monsters",
        "slime",
    },
    "item": {
        "chest",
        "coin",
        "coins",
        "collectible",
        "collectibles",
        "fruit",
        "fruits",
        "gem",
        "gems",
        "item",
        "items",
        "key",
        "keys",
        "potion",
    },
    "hazard": {
        "bomb",
        "cannon",
        "fire",
        "hazard",
        "hazards",
        "lava",
        "saw",
        "saws",
        "spike",
        "spikes",
        "trap",
        "traps",
    },
    "prop": {
        "box",
        "boxes",
        "bush",
        "checkpoint",
        "crate",
        "crates",
        "decor",
        "decoration",
        "door",
        "doors",
        "flag",
        "prop",
        "props",
        "sign",
        "tree",
        "trees",
    },
    "ui": {
        "button",
        "buttons",
        "cursor",
        "dialog",
        "font",
        "fonts",
        "gui",
        "hud",
        "icon",
        "icons",
        "menu",
        "menus",
        "ui",
    },
}
PACK_SHOWCASE_LABEL_HINTS = {
    "adventure",
    "arcade",
    "game",
    "jump",
    "pixel",
    "platform",
    "platformer",
    "rpg",
    "sprite",
    "sprites",
}


def _pack_top_dir(value: str) -> str:
    parts = [part for part in str(value or "").replace("\\", "/").split("/") if part]
    if not parts:
        return ""
    return re.sub(r"[\s_\-]+", "", parts[0].lower())


def _pack_record_stem(record: dict) -> str:
    text = str(record.get("resource_path") or record.get("title") or "")
    name = text.replace("\\", "/").rsplit("/", 1)[-1]
    stem = Path(name).stem.lower()
    return re.sub(r"[\s_\-]+", "", stem)


def _filter_pack_alternate_format_records(
    candidates: list[tuple[int, str, str, dict]],
) -> list[tuple[int, str, str, dict]]:
    non_source_stems: set[str] = set()
    source_records_by_top: dict[str, list[tuple[dict, str]]] = {}
    for _, _, _, record in candidates:
        if str(record.get("resource_type") or "") != SINGLE_IMAGE_RESOURCE_TYPE:
            continue
        stem = _pack_record_stem(record)
        if not stem:
            continue
        top_dir = str(record.get("top_dir") or "")
        if top_dir in PACK_SOURCE_FORMAT_DIRS:
            source_records_by_top.setdefault(top_dir, []).append((record, stem))
        else:
            non_source_stems.add(stem)

    if not non_source_stems or not source_records_by_top:
        return candidates

    skip_records: set[int] = set()
    for records in source_records_by_top.values():
        stems = {stem for _, stem in records}
        covered_stems = stems & non_source_stems
        if not covered_stems:
            continue
        coverage_ratio = len(covered_stems) / float(len(stems))
        if covered_stems != stems and coverage_ratio < PACK_ALTERNATE_FORMAT_COVERAGE_RATIO:
            continue
        for record, stem in records:
            if stem in covered_stems:
                skip_records.add(id(record))

    if not skip_records:
        return candidates
    return [candidate for candidate in candidates if id(candidate[3]) not in skip_records]


def _audio_group_key(path: str) -> str:
    stem = Path(path).stem.lower()
    stem = re.sub(r"[\s_\-]+(?:\d{1,4}|[a-z])$", "", stem)
    stem = re.sub(r"[\s_\-]+(?:\d{1,4}|[a-z])$", "", stem)
    stem = re.sub(r"[\s_\-]+", " ", stem).strip()
    return stem or Path(path).stem.lower()


def _pack_audio_paths(entity: ResourceProcessingEntity) -> list[str]:
    paths = [
        file_info.file_path
        for file_info in entity.files
        if file_info.file_path and Path(file_info.file_path).suffix.lower() in AUDIO_EXTS
    ]
    return sorted(paths, key=_natural_sort_key)


def _is_pure_audio_pack(entity: ResourceProcessingEntity) -> bool:
    if entity.resource_type != PACK_RESOURCE_TYPE:
        return False
    if set(entity.contains_resource_types or []) == {PACK_AUDIO_RESOURCE_TYPE}:
        return True
    raw_items = entity.auxiliary_metadata.get("child_resources", [])
    if not raw_items:
        raw_items = entity.auxiliary_metadata.get("child_previews", [])
    resource_types = {
        str(item.get("resource_type") or "")
        for item in raw_items
        if isinstance(item, dict) and item.get("resource_type")
    }
    return bool(resource_types) and resource_types == {PACK_AUDIO_RESOURCE_TYPE}


def _save_audio_pack_summary_card(entity: ResourceProcessingEntity, output_path: Path, size: int = 512) -> None:
    audio_paths = _pack_audio_paths(entity)
    format_counts = Counter(Path(path).suffix.lower().lstrip(".") or "audio" for path in audio_paths)
    group_counts = Counter(_audio_group_key(path) for path in audio_paths)
    top_groups = sorted(group_counts.items(), key=lambda item: (-item[1], _natural_sort_key(item[0])))[:PACK_AUDIO_SAMPLE_GROUP_LIMIT]

    lines = [
        f"Audio files: {len(audio_paths) or entity.child_resource_count or entity.member_count}",
    ]
    if format_counts:
        lines.append("Formats: " + ", ".join(f"{fmt} x{count}" for fmt, count in sorted(format_counts.items())))
    if top_groups:
        lines.append("Sound groups:")
        lines.extend(f"{name} x{count}" for name, count in top_groups)
    elif entity.child_resource_count:
        lines.append(f"Audio child resources: {entity.child_resource_count}")
    if entity.member_count:
        lines.append(f"Package files: {entity.member_count}")

    _save_metadata_card(
        output_path,
        f"Audio Pack: {entity.title or entity.pack_name or 'Pack'}",
        entity.source_description or entity.category or "Pure audio resource pack",
        lines,
        size,
    )


def _pack_child_resource_file_paths(item: dict, suffixes: set[str]) -> list[str]:
    files = item.get("files") or []
    if not isinstance(files, list):
        return []
    primary: list[str] = []
    others: list[str] = []
    seen: set[str] = set()
    for file_info in files:
        if not isinstance(file_info, dict):
            continue
        file_path = str(file_info.get("file_path") or "")
        if not file_path or Path(file_path).suffix.lower() not in suffixes or not Path(file_path).is_file():
            continue
        absolute = os.path.abspath(file_path)
        key = os.path.normcase(absolute)
        if key in seen:
            continue
        seen.add(key)
        if file_info.get("is_primary"):
            primary.append(absolute)
        else:
            others.append(absolute)
    return sorted(primary, key=_natural_sort_key) + sorted(others, key=_natural_sort_key)


def _pack_child_resource_file_md5(item: dict, file_path: str) -> str:
    files = item.get("files") or []
    if not isinstance(files, list):
        return ""
    key = os.path.normcase(os.path.abspath(file_path))
    for file_info in files:
        if not isinstance(file_info, dict):
            continue
        candidate = str(file_info.get("file_path") or "")
        if candidate and os.path.normcase(os.path.abspath(candidate)) == key:
            return str(file_info.get("content_md5") or "")
    return ""


def _pack_child_resource_visual_paths(item: dict) -> list[str]:
    resource_type = str(item.get("resource_type") or "")
    raster_paths = _pack_child_resource_file_paths(item, RASTER_EXTS)
    image_paths = _pack_child_resource_file_paths(item, RASTER_EXTS | SVG_EXTS)

    if resource_type == ATLAS_RESOURCE_TYPE:
        xml_paths = _pack_child_resource_file_paths(item, {".xml"})
        declared_paths: list[str] = []
        seen_declared: set[str] = set()
        for xml_path in sorted(xml_paths, key=_natural_sort_key):
            for image_path in _atlas_declared_image_paths(xml_path, raster_paths):
                key = os.path.normcase(os.path.abspath(image_path))
                if key in seen_declared:
                    continue
                seen_declared.add(key)
                declared_paths.append(image_path)
        return declared_paths or raster_paths

    if resource_type == TILESET_RESOURCE_TYPE:
        overview_path = _find_tileset_overview_image(raster_paths)
        if overview_path:
            return [overview_path]
        return _tileset_sheet_image_paths(raster_paths) or raster_paths

    if resource_type == TILED_MAP_RESOURCE_TYPE:
        overview_path = _find_tileset_overview_image(raster_paths)
        if overview_path:
            return [overview_path]
        tmx_paths = _pack_child_resource_file_paths(item, TMX_EXTS)
        tileset_paths: list[str] = []
        seen_tileset: set[str] = set()
        for tmx_path in sorted(tmx_paths, key=_natural_sort_key):
            for image_path in _tiled_tileset_image_paths(tmx_path):
                key = os.path.normcase(os.path.abspath(image_path))
                if key in seen_tileset:
                    continue
                seen_tileset.add(key)
                tileset_paths.append(image_path)
        return raster_paths or tileset_paths

    if resource_type == ANIMATION_SEQUENCE_RESOURCE_TYPE:
        return _sample_paths(sorted(raster_paths, key=_natural_sort_key), min(3, len(raster_paths)))

    return image_paths


def _source_image_has_pack_content(path: str) -> bool:
    if Path(path).suffix.lower() in SVG_EXTS:
        try:
            return Path(path).stat().st_size > 0
        except OSError:
            return False
    try:
        with Image.open(path) as image:
            image.seek(0)
            if image.width <= 0 or image.height <= 0:
                return False
            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                alpha = image.convert("RGBA").getchannel("A")
                _lo, hi = alpha.getextrema()
                return hi > 0
            return True
    except Exception:
        return False


def _pack_child_resource_text(item: dict) -> str:
    parts = [
        str(item.get("resource_path") or ""),
        str(item.get("title") or ""),
        str(item.get("resource_type") or ""),
    ]
    files = item.get("files") or []
    if isinstance(files, list):
        for file_info in files[:8]:
            if not isinstance(file_info, dict):
                continue
            parts.append(str(file_info.get("file_path") or ""))
            parts.append(str(file_info.get("file_name") or ""))
    return " ".join(parts).replace("\\", "/").lower()


def _pack_child_resource_category(item: dict) -> str:
    text = _pack_child_resource_text(item)
    tokens, compact = _pack_showcase_text_tokens(text)
    for category in ("official", "sheet", "effect"):
        if _pack_showcase_matches_keywords(tokens, compact, PACK_DIRECT_IMAGE_IMPORTANCE_KEYWORDS[category]):
            return category
    for category in ("character", "enemy", "background", "terrain", "ui", "item", "prop", "hazard"):
        if _pack_showcase_matches_keywords(tokens, compact, PACK_SHOWCASE_CATEGORY_KEYWORDS[category]):
            return category
    return "other"


def _pack_child_resource_score(item: dict) -> int:
    category = _pack_child_resource_category(item)
    score = PACK_DIRECT_IMAGE_CATEGORY_SCORE.get(category, 20)
    resource_type = str(item.get("resource_type") or "")
    if resource_type in {TILED_MAP_RESOURCE_TYPE, ATLAS_RESOURCE_TYPE, TILESET_RESOURCE_TYPE}:
        score += 28
    elif resource_type == ANIMATION_SEQUENCE_RESOURCE_TYPE:
        score += 10
    try:
        priority = int(item.get("priority", 999))
    except (TypeError, ValueError):
        priority = 999
    score += max(0, 80 - min(priority, 80))
    top_dir = _pack_top_dir(str(item.get("resource_path") or item.get("title") or ""))
    if top_dir in PACK_SOURCE_FORMAT_DIRS:
        score -= 30
    if _pack_direct_image_is_frame_like(str(item.get("resource_path") or item.get("title") or "")):
        score -= 12
    return score


def _pack_child_resource_group_key(item: dict) -> str:
    text = str(item.get("resource_path") or item.get("title") or "")
    if text:
        return _pack_direct_image_group_key(text)
    files = item.get("files") or []
    if isinstance(files, list):
        for file_info in files:
            if isinstance(file_info, dict) and file_info.get("file_path"):
                return _pack_direct_image_group_key(str(file_info.get("file_path") or ""))
    return str(item.get("source_resource_id") or item.get("task_id") or id(item))


def _pack_child_resource_bucket(item: dict) -> str:
    text = str(item.get("resource_path") or item.get("title") or "")
    if text:
        return _pack_direct_image_bucket(text)
    files = item.get("files") or []
    if isinstance(files, list):
        for file_info in files:
            if isinstance(file_info, dict) and file_info.get("file_path"):
                return _pack_direct_image_bucket(str(file_info.get("file_path") or ""))
    return str(item.get("resource_type") or OTHER_RESOURCE_TYPE)


def _pack_child_resource_is_icon_collection(items: list[dict]) -> bool:
    if not items:
        return False
    sampled = items if len(items) <= 1024 else _sample_paths(items, 256)
    matches = 0
    for item in sampled:
        tokens, compact = _pack_showcase_text_tokens(_pack_child_resource_text(item))
        if _pack_showcase_matches_keywords(tokens, compact, PACK_DIRECT_IMAGE_ICON_HINTS):
            matches += 1
    return matches / float(len(sampled)) >= 0.35


def _sample_pack_child_resource_items(items: list[dict]) -> list[dict]:
    if not items:
        return []
    icon_collection = _pack_child_resource_is_icon_collection(items)
    limit = PACK_DIRECT_IMAGE_ICON_SAMPLE_LIMIT if icon_collection else PACK_MIXED_IMAGE_SAMPLE_LIMIT
    if len(items) <= limit:
        return items

    best_by_group: dict[str, dict] = {}
    for index, item in enumerate(items):
        group_key = _pack_child_resource_group_key(item)
        candidate = {
            "path": item,
            "score": _pack_child_resource_score(item),
            "bucket": _pack_child_resource_bucket(item),
            "natural": _natural_sort_key(str(item.get("resource_path") or item.get("title") or index)),
        }
        current = best_by_group.get(group_key)
        if (
            current is None
            or candidate["score"] > current["score"]
            or (candidate["score"] == current["score"] and candidate["natural"] < current["natural"])
        ):
            best_by_group[group_key] = candidate

    sampled = _round_robin_pack_direct_image_sample(list(best_by_group.values()), limit)
    return [item for item in sampled if isinstance(item, dict)]


def _pack_child_resource_records(entity: ResourceProcessingEntity) -> list[dict]:
    raw_items = entity.auxiliary_metadata.get("child_resources", [])
    if not isinstance(raw_items, list):
        return []

    resource_types = {
        str(item.get("resource_type") or "")
        for item in raw_items
        if isinstance(item, dict) and item.get("resource_type")
    }
    include_audio = resource_types == {PACK_AUDIO_RESOURCE_TYPE}
    has_visual_sheet = any(
        isinstance(item, dict)
        and (
            str(item.get("resource_type") or "") in {ATLAS_RESOURCE_TYPE, TILESET_RESOURCE_TYPE, TILED_MAP_RESOURCE_TYPE}
            or "sheet" in _pack_top_dir(str(item.get("resource_path") or ""))
            or "atlas" in _pack_top_dir(str(item.get("resource_path") or ""))
            or "tile" in _pack_top_dir(str(item.get("resource_path") or ""))
        )
        for item in raw_items
    )

    candidates: list[tuple[int, str, str, dict]] = []
    eligible_items: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        resource_type = str(item.get("resource_type") or "")
        if resource_type == PACK_AUDIO_RESOURCE_TYPE and not include_audio:
            continue
        top_dir = _pack_top_dir(str(item.get("resource_path") or item.get("title") or ""))
        if has_visual_sheet and top_dir in PACK_SOURCE_FORMAT_DIRS:
            continue
        eligible_items.append(item)

    for index, item in enumerate(_sample_pack_child_resource_items(eligible_items)):
        resource_type = str(item.get("resource_type") or "")
        top_dir = _pack_top_dir(str(item.get("resource_path") or item.get("title") or ""))
        visual_paths = _pack_child_resource_visual_paths(item)
        if not visual_paths:
            continue
        preview_path = visual_paths[0]
        if not preview_path or not Path(preview_path).is_file():
            continue
        if not _source_image_has_pack_content(preview_path):
            continue
        priority = item.get("priority", 999)
        try:
            priority_value = int(priority)
        except (TypeError, ValueError):
            priority_value = 999
        label = str(item.get("resource_path") or item.get("title") or item.get("task_id") or index)
        record = dict(item)
        record["preview_path"] = os.path.abspath(preview_path)
        record["source_paths"] = [os.path.abspath(path) for path in visual_paths[:16] if Path(path).is_file()]
        record["top_dir"] = top_dir
        record["_source_content_md5"] = _pack_child_resource_file_md5(item, preview_path)
        candidates.append((priority_value, label, record["preview_path"], record))

    if not candidates:
        return []

    candidates = _filter_pack_alternate_format_records(candidates)

    records: list[dict] = []
    seen_files: set[str] = set()
    seen_content: set[str] = set()
    for _, _, preview_path, record in sorted(
        candidates,
        key=lambda item: (item[0], _natural_sort_key(item[1]), item[2].lower()),
    ):
        absolute = os.path.abspath(preview_path)
        key = os.path.normcase(absolute)
        if key in seen_files:
            continue
        seen_files.add(key)
        digest = str(record.pop("_source_content_md5", "") or "")
        if digest and digest in seen_content:
            continue
        if digest:
            seen_content.add(digest)
        records.append(record)

    return records


def _pack_child_preview_records(entity: ResourceProcessingEntity) -> list[dict]:
    raw_items = entity.auxiliary_metadata.get("child_previews", [])
    if not isinstance(raw_items, list):
        return []

    resource_types = {
        str(item.get("resource_type") or "")
        for item in raw_items
        if isinstance(item, dict) and item.get("resource_type")
    }
    include_audio = resource_types == {PACK_AUDIO_RESOURCE_TYPE}
    has_visual_sheet = any(
        isinstance(item, dict)
        and (
            str(item.get("resource_type") or "") in {ATLAS_RESOURCE_TYPE, TILESET_RESOURCE_TYPE, TILED_MAP_RESOURCE_TYPE}
            or "sheet" in _pack_top_dir(str(item.get("resource_path") or ""))
            or "atlas" in _pack_top_dir(str(item.get("resource_path") or ""))
            or "tile" in _pack_top_dir(str(item.get("resource_path") or ""))
        )
        for item in raw_items
    )

    candidates: list[tuple[int, str, str, dict]] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        resource_type = str(item.get("resource_type") or "")
        if resource_type == PACK_AUDIO_RESOURCE_TYPE and not include_audio:
            continue
        top_dir = _pack_top_dir(str(item.get("resource_path") or item.get("title") or ""))
        if has_visual_sheet and top_dir in PACK_SOURCE_FORMAT_DIRS:
            continue
        preview_path = str(item.get("preview_path") or item.get("path") or "")
        if not preview_path or not Path(preview_path).is_file():
            continue
        if not _preview_has_pack_content(preview_path):
            continue
        priority = item.get("priority", 999)
        try:
            priority_value = int(priority)
        except (TypeError, ValueError):
            priority_value = 999
        label = str(item.get("resource_path") or item.get("title") or item.get("task_id") or index)
        record = dict(item)
        record["preview_path"] = os.path.abspath(preview_path)
        record["top_dir"] = top_dir
        candidates.append((priority_value, label, record["preview_path"], record))

    if not candidates:
        return []

    candidates = _filter_pack_alternate_format_records(candidates)

    records: list[dict] = []
    seen_files: set[str] = set()
    seen_content: set[str] = set()
    for _, _, preview_path, record in sorted(
        candidates,
        key=lambda item: (item[0], _natural_sort_key(item[1]), item[2].lower()),
    ):
        absolute = os.path.abspath(preview_path)
        if absolute in seen_files:
            continue
        seen_files.add(absolute)
        try:
            digest = hashlib.md5(Path(absolute).read_bytes()).hexdigest()
        except OSError:
            digest = ""
        if digest and digest in seen_content:
            continue
        if digest:
            seen_content.add(digest)
        records.append(record)

    return records


def _pack_label_text(entity: ResourceProcessingEntity) -> str:
    return " ".join(
        str(value or "")
        for value in (
            entity.title,
            entity.pack_name,
            entity.resource_path,
            entity.source_directory,
            entity.category,
            " ".join(entity.tags or []),
            entity.source_description,
        )
    ).replace("\\", "/").lower()


def _pack_is_single_image_collection(records: list[dict]) -> bool:
    return bool(records) and {
        str(record.get("resource_type") or "") for record in records
    } == {SINGLE_IMAGE_RESOURCE_TYPE}


def _pack_showcase_record_identity(record: dict) -> str:
    source_path = _pack_item_source_path(record)
    preview_path = _pack_item_preview_path(record)
    return os.path.abspath(source_path or preview_path or str(record.get("task_id") or id(record)))


def _pack_showcase_record_text(record: dict) -> str:
    parts = [
        str(record.get("resource_path") or ""),
        str(record.get("title") or ""),
        str(record.get("top_dir") or ""),
        str(record.get("resource_type") or ""),
    ]
    source_paths = record.get("source_paths") or []
    if isinstance(source_paths, list):
        parts.extend(str(path or "") for path in source_paths)
    return " ".join(parts).replace("\\", "/").lower()


def _pack_showcase_text_tokens(text: str) -> tuple[set[str], str]:
    tokens = {token for token in re.split(r"[^a-z0-9]+", text.lower()) if token}
    compact = re.sub(r"[^a-z0-9]+", "", text.lower())
    return tokens, compact


def _pack_showcase_matches_keywords(tokens: set[str], compact: str, keywords: set[str]) -> bool:
    for keyword in keywords:
        normalized = re.sub(r"[^a-z0-9]+", "", keyword.lower())
        if not normalized:
            continue
        if normalized in tokens:
            return True
        if len(normalized) >= 5 and normalized in compact:
            return True
    return False


def _pack_showcase_categories(record: dict) -> set[str]:
    tokens, compact = _pack_showcase_text_tokens(_pack_showcase_record_text(record))
    categories = {
        category
        for category, keywords in PACK_SHOWCASE_CATEGORY_KEYWORDS.items()
        if _pack_showcase_matches_keywords(tokens, compact, keywords)
    }
    if "ui" in categories and len(categories) > 1:
        categories.remove("ui")
    return categories


def _pack_record_is_map_like(record: dict) -> bool:
    if str(record.get("resource_type") or "") == TILED_MAP_RESOURCE_TYPE:
        return True
    values = [
        record.get("resource_path"),
        record.get("title"),
        record.get("preview_path"),
        record.get("path"),
    ]
    source_paths = record.get("source_paths") or []
    if isinstance(source_paths, list):
        values.extend(source_paths)
    return any(Path(str(value or "")).suffix.lower() in TMX_EXTS for value in values)


def _pack_allows_generated_showcase(entity: ResourceProcessingEntity, records: list[dict]) -> bool:
    if TILED_MAP_RESOURCE_TYPE in set(entity.contains_resource_types or []):
        return True
    if any(Path(file_info.file_path).suffix.lower() in TMX_EXTS for file_info in entity.files):
        return True
    return any(_pack_record_is_map_like(record) for record in records)


def _pack_showcase_buckets(entity: ResourceProcessingEntity, records: list[dict]) -> dict[str, list[dict]]:
    if len(records) < PACK_SHOWCASE_MIN_RECORDS:
        return {}
    if _pack_is_single_image_collection(records):
        return {}

    buckets = {category: [] for category in PACK_SHOWCASE_CATEGORY_KEYWORDS}
    for record in records:
        for category in _pack_showcase_categories(record):
            buckets[category].append(record)

    has_environment = bool(buckets["background"] or buckets["terrain"])
    has_actor = bool(buckets["character"] or buckets["enemy"])
    supporting_categories = sum(bool(buckets[category]) for category in ("item", "hazard", "prop", "ui"))
    label_tokens, label_compact = _pack_showcase_text_tokens(_pack_label_text(entity))
    has_pack_hint = _pack_showcase_matches_keywords(label_tokens, label_compact, PACK_SHOWCASE_LABEL_HINTS)
    if not has_environment or not has_actor:
        return {}
    if supporting_categories == 0 and not has_pack_hint and len(records) < 8:
        return {}

    deduped: dict[str, list[dict]] = {}
    for category, category_records in buckets.items():
        seen: set[str] = set()
        deduped_records = []
        for record in category_records:
            identity = _pack_showcase_record_identity(record)
            if identity in seen:
                continue
            seen.add(identity)
            deduped_records.append(record)
        deduped[category] = deduped_records
    return deduped


def _pack_showcase_strip_frame(item: str | dict, image: Image.Image) -> Image.Image:
    strip = _pack_strip_axis_and_frame_size(item, image.size)
    if not strip:
        return image
    axis, frame_w, frame_h = strip
    frame_count = image.width // frame_w if axis == "horizontal" else image.height // frame_h
    if frame_count < 3:
        return image
    index = min(frame_count - 1, max(0, frame_count // 2))
    if axis == "horizontal":
        box = (index * frame_w, 0, min(image.width, (index + 1) * frame_w), min(image.height, frame_h))
    else:
        box = (0, index * frame_h, min(image.width, frame_w), min(image.height, (index + 1) * frame_h))
    return image.crop(box)


def _open_pack_showcase_image(item: str | dict) -> Image.Image | None:
    candidates = [_pack_item_source_path(item), _pack_item_preview_path(item)]
    seen: set[str] = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if Path(path).suffix.lower() not in RASTER_EXTS or not Path(path).is_file():
            continue
        try:
            with Image.open(path) as image:
                rgba = image.convert("RGBA")
                rgba = _pack_showcase_strip_frame(item, rgba)
                visible = _crop_visible_region(rgba) or rgba
                if visible.width <= 0 or visible.height <= 0:
                    continue
                return visible.copy()
        except Exception:
            continue
    try:
        fallback = _open_pack_collage_image(item, prefer_source=True).convert("RGBA")
    except Exception:
        return None
    visible = _crop_visible_region(fallback) or fallback
    return visible.copy()


def _pack_showcase_resample(image: Image.Image, scale: float) -> Image.Resampling:
    if max(image.size) <= 256:
        return Image.Resampling.NEAREST
    return Image.Resampling.LANCZOS if scale < 1 else Image.Resampling.NEAREST


def _pack_showcase_fit_image(image: Image.Image, max_width: int, max_height: int) -> Image.Image:
    if image.width <= 0 or image.height <= 0:
        return image
    scale = min(max_width / float(image.width), max_height / float(image.height))
    scale = max(0.01, scale)
    target = (
        max(1, int(round(image.width * scale))),
        max(1, int(round(image.height * scale))),
    )
    if target == image.size:
        return image.copy()
    return image.resize(target, _pack_showcase_resample(image, scale))


def _pack_showcase_alpha_composite(canvas: Image.Image, image: Image.Image, xy: tuple[int, int]) -> bool:
    x, y = xy
    left = max(0, x)
    top = max(0, y)
    right = min(canvas.width, x + image.width)
    bottom = min(canvas.height, y + image.height)
    if right <= left or bottom <= top:
        return False
    crop = image.crop((left - x, top - y, right - x, bottom - y))
    canvas.alpha_composite(crop, (left, top))
    return True


def _pack_showcase_paste_center_bottom(
    canvas: Image.Image,
    image: Image.Image,
    center_x: int,
    bottom_y: int,
    max_width: int,
    max_height: int,
) -> bool:
    fitted = _pack_showcase_fit_image(image, max_width, max_height)
    x = center_x - fitted.width // 2
    y = bottom_y - fitted.height
    return _pack_showcase_alpha_composite(canvas, fitted, (x, y))


def _pack_showcase_take_images(
    buckets: dict[str, list[dict]],
    category: str,
    limit: int,
    used: set[str],
) -> list[Image.Image]:
    images: list[Image.Image] = []
    for record in buckets.get(category, []):
        identity = _pack_showcase_record_identity(record)
        if identity in used:
            continue
        image = _open_pack_showcase_image(record)
        if image is None:
            continue
        used.add(identity)
        images.append(image)
        if len(images) >= limit:
            break
    return images


def _pack_showcase_fill_background(canvas: Image.Image, image: Image.Image) -> bool:
    scale = max(canvas.width / float(image.width), canvas.height / float(image.height))
    fitted = image.resize(
        (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale)))),
        _pack_showcase_resample(image, scale),
    )
    left = max(0, (fitted.width - canvas.width) // 2)
    top = max(0, (fitted.height - canvas.height) // 2)
    crop = fitted.crop((left, top, left + canvas.width, top + canvas.height))
    canvas.alpha_composite(crop, (0, 0))
    return True


def _pack_showcase_paste_terrain(canvas: Image.Image, images: list[Image.Image]) -> int:
    if not images:
        return 0
    pasted = 0
    x = 24
    bottom = canvas.height - 42
    index = 0
    while x < canvas.width - 24 and index < len(images) * 4:
        image = images[index % len(images)]
        fitted = _pack_showcase_fit_image(image, 112, 58)
        if _pack_showcase_alpha_composite(canvas, fitted, (x, bottom - fitted.height)):
            pasted += 1
        x += max(20, fitted.width + 6)
        index += 1
    return pasted


def _save_pack_showcase_preview(
    entity: ResourceProcessingEntity,
    records: list[dict],
    output_path: Path,
    size: int = 512,
) -> bool:
    buckets = _pack_showcase_buckets(entity, records)
    if not buckets:
        return False

    used: set[str] = set()
    canvas = Image.new("RGBA", (size, size), (246, 247, 249, 255))
    pasted = 0

    background_images = _pack_showcase_take_images(buckets, "background", PACK_SHOWCASE_BACKGROUND_LIMIT, used)
    if background_images:
        pasted += 1 if _pack_showcase_fill_background(canvas, background_images[0]) else 0

    terrain_images = _pack_showcase_take_images(buckets, "terrain", PACK_SHOWCASE_TERRAIN_LIMIT, used)
    baseline = size - 76
    if terrain_images:
        pasted += _pack_showcase_paste_terrain(canvas, terrain_images)
        baseline = size - 86

    character_images = _pack_showcase_take_images(buckets, "character", PACK_SHOWCASE_CHARACTER_LIMIT, used)
    character_positions = [int(size * 0.30), int(size * 0.43)]
    for index, image in enumerate(character_images):
        pasted += 1 if _pack_showcase_paste_center_bottom(canvas, image, character_positions[index], baseline, 104, 136) else 0

    enemy_images = _pack_showcase_take_images(buckets, "enemy", PACK_SHOWCASE_ENEMY_LIMIT, used)
    enemy_positions = [
        (int(size * 0.58), baseline - 2),
        (int(size * 0.70), baseline - 14),
        (int(size * 0.82), baseline - 2),
        (int(size * 0.64), baseline - 70),
        (int(size * 0.78), baseline - 82),
    ]
    for image, (x, bottom) in zip(enemy_images, enemy_positions):
        pasted += 1 if _pack_showcase_paste_center_bottom(canvas, image, x, bottom, 72, 86) else 0

    hazard_images = _pack_showcase_take_images(buckets, "hazard", PACK_SHOWCASE_HAZARD_LIMIT, used)
    hazard_positions = [
        (int(size * 0.50), baseline + 4),
        (int(size * 0.88), baseline + 4),
        (int(size * 0.20), baseline + 4),
        (int(size * 0.62), baseline + 4),
    ]
    for image, (x, bottom) in zip(hazard_images, hazard_positions):
        pasted += 1 if _pack_showcase_paste_center_bottom(canvas, image, x, bottom, 62, 58) else 0

    item_images = _pack_showcase_take_images(buckets, "item", PACK_SHOWCASE_ITEM_LIMIT, used)
    item_positions = [
        (int(size * 0.58), int(size * 0.34)),
        (int(size * 0.66), int(size * 0.28)),
        (int(size * 0.74), int(size * 0.34)),
        (int(size * 0.82), int(size * 0.28)),
        (int(size * 0.38), int(size * 0.30)),
        (int(size * 0.46), int(size * 0.24)),
        (int(size * 0.90), int(size * 0.36)),
    ]
    for image, (x, bottom) in zip(item_images, item_positions):
        pasted += 1 if _pack_showcase_paste_center_bottom(canvas, image, x, bottom, 42, 42) else 0

    prop_images = _pack_showcase_take_images(buckets, "prop", PACK_SHOWCASE_PROP_LIMIT, used)
    prop_positions = [
        (int(size * 0.18), baseline),
        (int(size * 0.88), baseline),
        (int(size * 0.12), baseline - 62),
        (int(size * 0.92), baseline - 58),
    ]
    for image, (x, bottom) in zip(prop_images, prop_positions):
        pasted += 1 if _pack_showcase_paste_center_bottom(canvas, image, x, bottom, 74, 86) else 0

    ui_images = _pack_showcase_take_images(buckets, "ui", PACK_SHOWCASE_UI_LIMIT, used)
    ui_x = 28
    for image in ui_images:
        fitted = _pack_showcase_fit_image(image, 54, 38)
        if _pack_showcase_alpha_composite(canvas, fitted, (ui_x, 28)):
            pasted += 1
        ui_x += fitted.width + 8

    if pasted < 3:
        return False
    canvas.convert("RGB").save(output_path, format="WEBP")
    return True


def _pack_preview_canvas_size(records: list[dict], entity: ResourceProcessingEntity, default_size: int) -> int:
    return max(default_size, PACK_GENERATED_COLLAGE_SIZE)


def _pack_collage_item_size(item: str | dict) -> tuple[int, int]:
    primary = _pack_item_source_path(item)
    fallback = _pack_item_preview_path(item)
    for path in (primary, fallback):
        if not path or not Path(path).is_file():
            continue
        ext = Path(path).suffix.lower()
        if ext in SVG_EXTS:
            size = _svg_declared_size(path)
            if size:
                return size
            continue
        if ext in RASTER_EXTS:
            try:
                with Image.open(path) as image:
                    image_size = (max(1, image.width), max(1, image.height))
                    if path == primary:
                        strip_size = _pack_strip_keyframe_preview_size(item, image_size)
                        if strip_size:
                            return strip_size
                    return image_size
            except Exception:
                continue
    return (1, 1)


def _pack_collage_item_sizes(items: list[str | dict]) -> list[tuple[int, int]]:
    return [_pack_collage_item_size(item) for item in items]


def _pack_dynamic_preview_pages(records: list[str | dict]) -> list[list[str | dict]]:
    if not records:
        return []

    sizes = _pack_collage_item_sizes(list(records))
    layout_size = max(PACK_GENERATED_COLLAGE_SIZE, PACK_ATLAS_LAYOUT_SIZE)
    atlas_pages = _layout_dense_atlas_pages_for_sizes(
        sizes,
        layout_size,
        layout_size,
        _atlas_page_gap(layout_size),
        multiple_pages=True,
    )

    pages: list[list[str | dict]] = []
    for atlas_page in atlas_pages:
        page_records = [
            records[rect.index]
            for rect in sorted(atlas_page.rects, key=lambda rect: rect.index)
            if 0 <= rect.index < len(records)
        ]
        if page_records:
            pages.append(page_records)
    return pages or [records]


def _pack_preview_record_text(record: dict) -> str:
    parts = [_pack_showcase_record_text(record)]
    source_path = _pack_item_source_path(record)
    preview_path = _pack_item_preview_path(record)
    parts.extend([source_path, preview_path])
    return " ".join(str(part or "") for part in parts).replace("\\", "/").lower()


def _pack_preview_record_category(record: dict) -> str:
    tokens, compact = _pack_showcase_text_tokens(_pack_preview_record_text(record))
    for category in ("official", "sheet", "effect"):
        if _pack_showcase_matches_keywords(tokens, compact, PACK_DIRECT_IMAGE_IMPORTANCE_KEYWORDS[category]):
            return category
    categories = _pack_showcase_categories(record)
    if categories:
        return max(categories, key=lambda category: PACK_DIRECT_IMAGE_CATEGORY_SCORE.get(category, 20))
    return "other"


def _pack_preview_record_score(record: dict) -> int:
    category = _pack_preview_record_category(record)
    score = PACK_DIRECT_IMAGE_CATEGORY_SCORE.get(category, 20)
    resource_type = str(record.get("resource_type") or "")
    if resource_type in {TILED_MAP_RESOURCE_TYPE, ATLAS_RESOURCE_TYPE, TILESET_RESOURCE_TYPE}:
        score += 28
    elif resource_type == ANIMATION_SEQUENCE_RESOURCE_TYPE:
        score += 10
    try:
        priority = int(record.get("priority", 999))
    except (TypeError, ValueError):
        priority = 999
    score += max(0, 80 - min(priority, 80))
    top_dir = _pack_top_dir(str(record.get("resource_path") or record.get("title") or ""))
    if top_dir in PACK_SOURCE_FORMAT_DIRS:
        score -= 30
    source_path = _pack_item_source_path(record) or _pack_item_preview_path(record)
    if source_path and _pack_direct_image_is_frame_like(source_path):
        score -= 12
    return score


def _pack_preview_record_group_key(record: dict) -> str:
    source_path = _pack_item_source_path(record) or _pack_item_preview_path(record)
    if source_path:
        return _pack_direct_image_group_key(source_path)
    text = str(record.get("resource_path") or record.get("title") or "")
    if text:
        return _pack_direct_image_group_key(text)
    return str(record.get("source_resource_id") or record.get("task_id") or id(record))


def _pack_preview_record_bucket(record: dict) -> str:
    source_path = _pack_item_source_path(record) or _pack_item_preview_path(record)
    if source_path:
        return _pack_direct_image_bucket(source_path)
    text = str(record.get("resource_path") or record.get("title") or "")
    if text:
        return _pack_direct_image_bucket(text)
    return str(record.get("resource_type") or OTHER_RESOURCE_TYPE)


def _pack_preview_records_are_icon_collection(records: list[dict]) -> bool:
    if not records:
        return False
    sampled = records if len(records) <= 1024 else _sample_paths(records, 256)
    matches = 0
    for record in sampled:
        tokens, compact = _pack_showcase_text_tokens(_pack_preview_record_text(record))
        if _pack_showcase_matches_keywords(tokens, compact, PACK_DIRECT_IMAGE_ICON_HINTS):
            matches += 1
    return matches / float(len(sampled)) >= 0.35


def _sample_pack_preview_records(records: list[dict]) -> list[dict]:
    if not records:
        return []
    icon_collection = _pack_preview_records_are_icon_collection(records)
    limit = PACK_DIRECT_IMAGE_ICON_SAMPLE_LIMIT if icon_collection else PACK_MIXED_IMAGE_SAMPLE_LIMIT
    if len(records) <= limit:
        return records

    best_by_group: dict[str, dict] = {}
    for index, record in enumerate(records):
        group_key = _pack_preview_record_group_key(record)
        candidate = {
            "path": record,
            "score": _pack_preview_record_score(record),
            "bucket": _pack_preview_record_bucket(record),
            "natural": _natural_sort_key(str(record.get("resource_path") or record.get("title") or index)),
        }
        current = best_by_group.get(group_key)
        if (
            current is None
            or candidate["score"] > current["score"]
            or (candidate["score"] == current["score"] and candidate["natural"] < current["natural"])
        ):
            best_by_group[group_key] = candidate

    sampled = _round_robin_pack_direct_image_sample(list(best_by_group.values()), limit)
    return [record for record in sampled if isinstance(record, dict)]


def _pack_preview_pages(records: list[dict], entity: ResourceProcessingEntity | None = None) -> list[list[dict]]:
    if not records:
        return []
    records = _sample_pack_preview_records(records)
    return _pack_dynamic_preview_pages(records)


PACK_DIRECT_IMAGE_IMPORTANCE_KEYWORDS = {
    "official": {
        "cover",
        "demo",
        "overview",
        "preview",
        "sample",
        "showcase",
    },
    "sheet": {
        "atlas",
        "sheet",
        "spritesheet",
        "sprite_sheet",
        "tilemap",
        "tileset",
        "tilesheet",
    },
    "effect": {
        "effect",
        "effects",
        "explosion",
        "impact",
        "particles",
        "slash",
        "vfx",
    },
}

PACK_DIRECT_IMAGE_CATEGORY_SCORE = {
    "official": 100,
    "sheet": 82,
    "character": 72,
    "enemy": 66,
    "background": 62,
    "terrain": 58,
    "effect": 54,
    "ui": 50,
    "item": 48,
    "prop": 46,
    "hazard": 44,
}

PACK_DIRECT_IMAGE_ICON_HINTS = {
    "cursor",
    "cursors",
    "gui",
    "hud",
    "icon",
    "icons",
    "interface",
    "ui",
}


def _pack_direct_path_parts(path: str) -> list[str]:
    return [part for part in str(path or "").replace("\\", "/").split("/") if part]


def _pack_direct_path_text(path: str) -> str:
    return "/".join(_pack_direct_path_parts(path)).lower()


PACK_DIRECT_IMAGE_ANIMATION_HINTS = {
    "attack",
    "death",
    "fall",
    "frame",
    "hit",
    "hurt",
    "idle",
    "jump",
    "run",
    "walk",
}


def _pack_direct_image_stem(path: str) -> str:
    stem = Path(str(path)).stem.lower()
    stem = re.sub(r"\(\s*\d{1,4}\s*x\s*\d{1,4}\s*\)", "", stem)
    tokens, compact = _pack_showcase_text_tokens(_pack_direct_path_text(path))
    is_animation = (
        bool(PACK_DIRECT_IMAGE_ANIMATION_HINTS & tokens)
        or any(hint in compact for hint in ("animation", "animated"))
        or bool(re.search(r"(?:^|[\s_\-])frame[\s_\-]*\d{1,5}$", stem))
    )
    if is_animation:
        stem = re.sub(r"[\s_\-]+(?:frame)?\d{1,5}$", "", stem)
        stem = re.sub(r"[\s_\-]+[a-z]$", "", stem)
    stem = re.sub(r"[\s_\-]+", "", stem)
    return stem or Path(str(path)).stem.lower()


def _pack_direct_image_group_key(path: str) -> str:
    parts = _pack_direct_path_parts(path)
    parent = "/".join(part.lower() for part in parts[-4:-1])
    return f"{parent}/{_pack_direct_image_stem(path)}"


def _pack_direct_image_bucket(path: str) -> str:
    parts = _pack_direct_path_parts(path)
    if len(parts) >= 2:
        return "/".join(part.lower() for part in parts[-3:-1])
    return _pack_top_dir(path) or "root"


def _pack_direct_image_category(path: str) -> str:
    text = _pack_direct_path_text(path)
    tokens, compact = _pack_showcase_text_tokens(text)
    for category in ("official", "sheet", "effect"):
        if _pack_showcase_matches_keywords(tokens, compact, PACK_DIRECT_IMAGE_IMPORTANCE_KEYWORDS[category]):
            return category
    for category in ("character", "enemy", "background", "terrain", "ui", "item", "prop", "hazard"):
        if _pack_showcase_matches_keywords(tokens, compact, PACK_SHOWCASE_CATEGORY_KEYWORDS[category]):
            return category
    return "other"


def _pack_direct_image_is_frame_like(path: str) -> bool:
    stem = Path(str(path)).stem.lower()
    return bool(
        re.search(r"[\s_\-](?:frame)?\d{1,5}$", stem)
        or re.search(r"[\s_\-](?:idle|run|walk|attack|hit|death|jump|fall)[\s_\-]*\d{1,5}$", stem)
    )


def _pack_direct_image_score(path: str) -> int:
    category = _pack_direct_image_category(path)
    score = PACK_DIRECT_IMAGE_CATEGORY_SCORE.get(category, 20)
    parts = _pack_direct_path_parts(path)
    if len(parts) <= 2:
        score += 12
    top_dir = _pack_top_dir(path)
    if top_dir in PACK_SOURCE_FORMAT_DIRS:
        score -= 30
    if _pack_direct_image_is_frame_like(path):
        score -= 16
    return score


def _pack_direct_image_is_icon_collection(paths: list[str]) -> bool:
    if not paths:
        return False
    matches = 0
    sampled = paths if len(paths) <= 1024 else _sample_paths(paths, 256)
    for path in sampled:
        text = _pack_direct_path_text(path)
        tokens, compact = _pack_showcase_text_tokens(text)
        if _pack_showcase_matches_keywords(tokens, compact, PACK_DIRECT_IMAGE_ICON_HINTS):
            matches += 1
    return matches / float(len(sampled)) >= 0.35


def _dedupe_pack_direct_image_groups(paths: list[str]) -> list[dict]:
    best_by_group: dict[str, dict] = {}
    for path in paths:
        group_key = _pack_direct_image_group_key(path)
        item = {
            "path": path,
            "score": _pack_direct_image_score(path),
            "bucket": _pack_direct_image_bucket(path),
            "category": _pack_direct_image_category(path),
            "natural": _natural_sort_key(path),
        }
        current = best_by_group.get(group_key)
        if (
            current is None
            or item["score"] > current["score"]
            or (item["score"] == current["score"] and item["natural"] < current["natural"])
        ):
            best_by_group[group_key] = item
    return list(best_by_group.values())


def _round_robin_pack_direct_image_sample(candidates: list[dict], limit: int) -> list[str]:
    if len(candidates) <= limit:
        return [
            item["path"]
            for item in sorted(candidates, key=lambda item: (-item["score"], item["bucket"], item["natural"]))
        ]

    buckets: dict[str, list[dict]] = {}
    for item in candidates:
        buckets.setdefault(str(item["bucket"]), []).append(item)
    for items in buckets.values():
        items.sort(key=lambda item: (-item["score"], item["natural"]))

    bucket_order = sorted(
        buckets,
        key=lambda bucket: (
            -max(item["score"] for item in buckets[bucket]),
            bucket,
        ),
    )
    selected: list[dict] = []
    while len(selected) < limit and bucket_order:
        next_order: list[str] = []
        for bucket in bucket_order:
            items = buckets[bucket]
            if not items:
                continue
            selected.append(items.pop(0))
            if items:
                next_order.append(bucket)
            if len(selected) >= limit:
                break
        bucket_order = next_order
    selected.sort(key=lambda item: (-item["score"], item["bucket"], item["natural"]))
    return [item["path"] for item in selected]


def _sample_pack_direct_image_paths(paths: list[str], *, icon_collection: bool | None = None) -> list[str]:
    sorted_paths = sorted(paths, key=_natural_sort_key)
    if icon_collection is None:
        icon_collection = _pack_direct_image_is_icon_collection(sorted_paths)
    limit = (
        PACK_DIRECT_IMAGE_ICON_SAMPLE_LIMIT
        if icon_collection
        else PACK_DIRECT_IMAGE_SAMPLE_LIMIT
    )
    if len(sorted_paths) <= limit:
        return sorted_paths
    candidates = _dedupe_pack_direct_image_groups(sorted_paths)
    return _round_robin_pack_direct_image_sample(candidates, limit)


def _pack_direct_image_pages(image_paths: list[str]) -> list[list[str]]:
    sorted_paths = sorted(image_paths, key=_natural_sort_key)
    icon_collection = _pack_direct_image_is_icon_collection(sorted_paths)
    paths = _sample_pack_direct_image_paths(sorted_paths, icon_collection=icon_collection)
    if not paths:
        return []
    return _pack_dynamic_preview_pages(paths)


PACK_OFFICIAL_PREVIEW_STEMS = {"sample", "preview", "cover", "overview"}
PACK_OFFICIAL_PREVIEW_DIRS = {"sample", "samples", "preview", "previews", "cover", "covers", "overview", "screenshots"}
PACK_ROOT_SHOWCASE_MAX_IMAGES = 4
PACK_ROOT_SHOWCASE_SCAN_LIMIT = 160
PACK_ROOT_SHOWCASE_MIN_NESTED_IMAGES = 8
PACK_ROOT_SHOWCASE_MIN_LONG_EDGE = 256
PACK_ROOT_SHOWCASE_MIN_AREA_RATIO = 8.0
PACK_ROOT_SHOWCASE_EXCLUDED_STEMS = {
    "atlas",
    "sheet",
    "spritesheet",
    "sprite_sheet",
    "tilesheet",
    "tilemap",
    "tileset",
}
PACK_ROOT_SHOWCASE_HINTS = {
    "all",
    "demo",
    "enemies",
    "enemy",
    "hello",
    "overview",
    "preview",
    "readme",
    "sample",
    "showcase",
}


def _pack_relative_parts(entity: ResourceProcessingEntity, file_path: str) -> tuple[str, ...]:
    try:
        relative = os.path.relpath(file_path, entity.source_directory)
    except (OSError, ValueError):
        return (Path(file_path).name,)
    if relative.startswith(".."):
        return (Path(file_path).name,)
    return Path(relative).parts


def _official_pack_preview_score(entity: ResourceProcessingEntity, file_path: str) -> tuple[int, list[tuple[int, object]]] | None:
    ext = Path(file_path).suffix.lower()
    if ext not in RASTER_EXTS and ext not in SVG_EXTS:
        return None
    parts = _pack_relative_parts(entity, file_path)
    if not parts:
        return None
    stem = Path(parts[-1]).stem.lower()
    normalized_stem = re.sub(r"[\s_\-]+", "", stem)
    token_stem = stem.replace("-", "_").replace(" ", "_")
    depth = len(parts) - 1

    if depth == 0 and (stem in PACK_OFFICIAL_PREVIEW_STEMS or normalized_stem in PACK_OFFICIAL_PREVIEW_STEMS):
        return (0, _natural_sort_key(file_path))
    if depth == 0 and any(token_stem.startswith(token) for token in PACK_OFFICIAL_PREVIEW_STEMS):
        return (1, _natural_sort_key(file_path))
    if depth == 1 and parts[0].lower() in PACK_OFFICIAL_PREVIEW_DIRS:
        return (2, _natural_sort_key(file_path))
    return None


def _pack_raster_file_info(entity: ResourceProcessingEntity) -> list[tuple[object, tuple[str, ...]]]:
    items = []
    for file_info in entity.files:
        file_path = getattr(file_info, "file_path", "")
        if not file_path or Path(file_path).suffix.lower() not in RASTER_EXTS or not Path(file_path).is_file():
            continue
        items.append((file_info, _pack_relative_parts(entity, file_path)))
    return items


def _image_area(path: str) -> int:
    try:
        with Image.open(path) as image:
            return max(1, image.width * image.height)
    except Exception:
        return 0


def _image_size(path: str) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            return image.width, image.height
    except Exception:
        return (0, 0)


def _pack_root_showcase_preview_score(
    file_info,
    nested_median_area: float,
) -> tuple[int, int, list[tuple[int, object]]] | None:
    file_path = getattr(file_info, "file_path", "")
    if not file_path:
        return None
    path = Path(file_path)
    stem = path.stem.lower()
    compact_stem = re.sub(r"[\s_\-]+", "", stem)
    token_stem = stem.replace("-", "_").replace(" ", "_")
    if compact_stem in PACK_ROOT_SHOWCASE_EXCLUDED_STEMS:
        return None
    if any(token in token_stem for token in PACK_ROOT_SHOWCASE_EXCLUDED_STEMS):
        return None

    width, height = _image_size(file_path)
    if max(width, height) < PACK_ROOT_SHOWCASE_MIN_LONG_EDGE:
        return None
    area = max(1, width * height)
    if nested_median_area <= 0 or area < nested_median_area * PACK_ROOT_SHOWCASE_MIN_AREA_RATIO:
        return None

    hint_score = 0
    if any(hint in token_stem or hint in compact_stem for hint in PACK_ROOT_SHOWCASE_HINTS):
        hint_score = -1
    primary_score = -1 if getattr(file_info, "is_primary", False) else 0
    return (hint_score, primary_score, _natural_sort_key(file_path))


def _pack_root_showcase_preview_paths(entity: ResourceProcessingEntity) -> list[str]:
    raster_items = _pack_raster_file_info(entity)
    if len(raster_items) > PACK_ROOT_SHOWCASE_SCAN_LIMIT:
        return []
    root_items = [(file_info, parts) for file_info, parts in raster_items if len(parts) == 1]
    nested_items = [(file_info, parts) for file_info, parts in raster_items if len(parts) > 1]
    if not root_items or len(root_items) > PACK_ROOT_SHOWCASE_MAX_IMAGES:
        return []
    if len(nested_items) < PACK_ROOT_SHOWCASE_MIN_NESTED_IMAGES:
        return []

    nested_areas = [
        _image_area(getattr(file_info, "file_path", ""))
        for file_info, _parts in nested_items
    ]
    nested_areas = sorted(area for area in nested_areas if area > 0)
    if not nested_areas:
        return []
    nested_median_area = float(nested_areas[len(nested_areas) // 2])

    candidates: list[tuple[tuple[int, int, list[tuple[int, object]]], str]] = []
    for file_info, _parts in root_items:
        score = _pack_root_showcase_preview_score(file_info, nested_median_area)
        if score is not None:
            candidates.append((score, getattr(file_info, "file_path", "")))
    return [path for _score, path in sorted(candidates, key=lambda item: item[0]) if path]


def _pack_official_preview_paths(entity: ResourceProcessingEntity) -> list[str]:
    candidates: list[tuple[tuple[int, list[tuple[int, object]]], str]] = []
    for file_info in entity.files:
        if not file_info.file_path or not Path(file_info.file_path).is_file():
            continue
        score = _official_pack_preview_score(entity, file_info.file_path)
        if score is not None:
            candidates.append((score, file_info.file_path))
    if candidates:
        return [path for _score, path in sorted(candidates, key=lambda item: item[0])]
    return _pack_root_showcase_preview_paths(entity)


def _save_official_pack_preview(image_path: str, output_path: Path, size: int = 512) -> None:
    ext = Path(image_path).suffix.lower()
    if ext in SVG_EXTS:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp:
            temp_path = temp.name
        try:
            if not _try_rasterize_svg(image_path, temp_path):
                raise ValueError("official SVG preview rasterization failed")
            _save_existing_raster_preview(temp_path, output_path, size)
        finally:
            Path(temp_path).unlink(missing_ok=True)
        return
    _save_existing_raster_preview(image_path, output_path, size)


def _save_pack_single_item_preview(item: str | dict, output_path: Path, size: int = 512) -> None:
    preview_path = _pack_item_preview_path(item)
    if not preview_path:
        raise ValueError("pack item has no preview path")
    _save_official_pack_preview(preview_path, output_path, size)


def _frame_visible_bbox(image: Image.Image) -> tuple[int, int, int, int]:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    alpha_min, alpha_max = alpha.getextrema()
    if alpha_min < alpha_max or alpha_min < 255:
        bbox = alpha.getbbox()
    else:
        rgb = rgba.convert("RGB")
        corners = [
            rgb.getpixel((0, 0)),
            rgb.getpixel((rgb.width - 1, 0)),
            rgb.getpixel((0, rgb.height - 1)),
            rgb.getpixel((rgb.width - 1, rgb.height - 1)),
        ]
        bg = max(set(corners), key=corners.count)
        diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg)).convert("L")
        mask = diff.point(lambda value: 255 if value > 8 else 0)
        bbox = mask.getbbox()
    if not bbox:
        return (0, 0, rgba.width, rgba.height)
    pad = 2
    return (
        max(0, bbox[0] - pad),
        max(0, bbox[1] - pad),
        min(rgba.width, bbox[2] + pad),
        min(rgba.height, bbox[3] + pad),
    )


def _union_bbox(boxes: list[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _pad_bbox(
    box: tuple[int, int, int, int],
    image_size: tuple[int, int],
    padding: int,
) -> tuple[int, int, int, int]:
    width, height = image_size
    left, top, right, bottom = box
    return (
        max(0, left - padding),
        max(0, top - padding),
        min(width, right + padding),
        min(height, bottom + padding),
    )


def _resize_frame(image: Image.Image, scale: float) -> Image.Image:
    width = max(1, int(round(image.width * scale)))
    height = max(1, int(round(image.height * scale)))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _save_gif(image_paths: list[str], output_path: Path, size: int = 512) -> None:
    rgba_frames: list[Image.Image] = []
    for path in image_paths:
        with Image.open(path) as image:
            rgba_frames.append(image.convert("RGBA").copy())
    if not rgba_frames:
        raise ValueError("no frames for sequence gif")

    source_sizes = {frame.size for frame in rgba_frames}
    if len(source_sizes) == 1:
        crop_box = _pad_bbox(
            _union_bbox([_frame_visible_bbox(frame) for frame in rgba_frames]),
            rgba_frames[0].size,
            ANIMATION_CROP_PADDING,
        )
        cropped = [frame.crop(crop_box) for frame in rgba_frames]
    else:
        cropped = [
            frame.crop(_pad_bbox(_frame_visible_bbox(frame), frame.size, ANIMATION_CROP_PADDING))
            for frame in rgba_frames
        ]

    max_width = max(frame.width for frame in cropped)
    max_height = max(frame.height for frame in cropped)
    fit_scale = min(size / max(1, max_width), size / max(1, max_height))
    min_scale = ANIMATION_MIN_LONG_EDGE / max(1, max(max_width, max_height))
    max_scale = max(ANIMATION_MAX_UPSCALE, min_scale)
    scale = min(fit_scale, max_scale)
    canvas_size = (max(1, int(round(max_width * scale))), max(1, int(round(max_height * scale))))
    backgrounds = [_visible_content_preview_background(frame, (245, 245, 245)) for frame in cropped]
    background = Counter(backgrounds).most_common(1)[0][0] if backgrounds else (245, 245, 245)

    frames: list[Image.Image] = []
    for frame in cropped:
        resized = _resize_frame(frame, scale)
        canvas = Image.new("RGB", canvas_size, background)
        canvas.paste(resized, ((canvas.width - resized.width) // 2, (canvas.height - resized.height) // 2), resized.getchannel("A"))
        frames.append(canvas)
    if len(frames) == 1:
        frames.append(frames[0].copy())
    first, *rest = frames
    first.save(output_path, save_all=True, append_images=rest, duration=120, loop=0, optimize=True)


def _resvg_module_path() -> str:
    candidates: list[Path] = []
    env_path = os.environ.get("RESVG_JS_MODULE")
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend(
        [
            Path(r"C:\tmp\resvg-node\node_modules\@resvg\resvg-js"),
            Path.cwd() / "node_modules" / "@resvg" / "resvg-js",
        ]
    )
    for candidate in candidates:
        if (candidate / "package.json").is_file():
            return str(candidate)
    return ""


def _try_rasterize_svg_resvg(svg_path: str, output_path: str, size: int = 1024) -> bool:
    module_path = _resvg_module_path()
    if not module_path:
        return False

    code = """
const { Resvg } = require(process.argv[1]);
const fs = require('fs');
const inputPath = process.argv[2];
const outputPath = process.argv[3];
const size = Number(process.argv[4]);
try {
  const svg = fs.readFileSync(inputPath);
  const renderer = new Resvg(svg, {
    background: '#9AA4B2',
    fitTo: { mode: 'width', value: size }
  });
  fs.writeFileSync(outputPath, renderer.render().asPng());
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
"""
    try:
        result = subprocess.run(
            ["node", "-e", code, module_path, svg_path, output_path, str(size)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0 and Path(output_path).is_file()
    except Exception:
        return False


def _try_rasterize_svg(svg_path: str, output_path: str, size: int = 1024) -> bool:
    if _try_rasterize_svg_resvg(svg_path, output_path, size):
        return True

    try:
        import cairosvg  # type: ignore

        try:
            cairosvg.svg2png(
                url=svg_path,
                write_to=output_path,
                output_width=size,
                output_height=size,
                background_color="#9AA4B2",
            )
            return True
        except Exception:
            pass
    except Exception:
        pass

    edge_candidates = []
    if os.name == "nt":
        edge_candidates.extend(
            [
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )
    edge = next((path for path in edge_candidates if path.is_file()), None)
    if edge is None:
        return False

    try:
        with tempfile.TemporaryDirectory(prefix="svg_preview_") as tmp:
            tmp_dir = Path(tmp)
            html_path = tmp_dir / "preview.html"
            user_data_dir = tmp_dir / "profile"
            svg_uri = Path(svg_path).resolve().as_uri()
            html_path.write_text(
                f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
html, body {{ margin: 0; width: {size}px; height: {size}px; overflow: hidden; background: #9aa4b2; }}
.frame {{ width: {size}px; height: {size}px; display: flex; align-items: center; justify-content: center; }}
img {{ max-width: {size}px; max-height: {size}px; width: {size}px; height: {size}px; object-fit: contain; }}
</style>
</head>
<body><div class="frame"><img src="{svg_uri}" /></div></body>
</html>""",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    str(edge),
                    "--headless=new",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--no-first-run",
                    f"--user-data-dir={user_data_dir}",
                    f"--window-size={size},{size}",
                    f"--screenshot={output_path}",
                    html_path.resolve().as_uri(),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode == 0 and Path(output_path).is_file()
    except Exception:
        return False


def _font_codepoints(font_path: str) -> list[int]:
    if TTFont is None:
        return []
    try:
        font = TTFont(font_path, lazy=True)
        codepoints = set()
        for table in font["cmap"].tables:
            codepoints.update(int(cp) for cp in table.cmap.keys())
        font.close()
        return sorted(cp for cp in codepoints if cp > 0)
    except Exception:
        return []


def _font_pack_sheet_image_path(font_path: str) -> str:
    font = Path(font_path)
    roots: list[Path] = []
    if font.parent.name.lower() == "fonts":
        roots.append(font.parent.parent)
    roots.extend([font.parent, *font.parents[:3]])

    seen: set[Path] = set()
    for root in roots:
        if root in seen or not root.exists():
            continue
        seen.add(root)
        candidates = sorted(root.glob("*_sheet_default.png"), key=lambda path: (len(path.name), path.name.lower()))
        candidates.extend(sorted(root.glob("*sheet_default.png"), key=lambda path: (len(path.name), path.name.lower())))
        candidates.extend(sorted(root.glob("*_sheet_double.png"), key=lambda path: (len(path.name), path.name.lower())))
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return ""


def _font_support_count(codepoints: list[int], sample: str) -> int:
    if not codepoints:
        return len(sample)
    available = set(codepoints)
    return sum(1 for char in sample if ord(char) in available)


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _fit_truetype_font(
    font_path: str,
    text: str,
    draw: ImageDraw.ImageDraw,
    max_width: int,
    start_size: int,
    min_size: int = 10,
) -> ImageFont.FreeTypeFont | None:
    fallback: ImageFont.FreeTypeFont | None = None
    for font_size in range(start_size, min_size - 1, -2):
        try:
            font = ImageFont.truetype(font_path, font_size)
        except OSError:
            return None
        fallback = font
        width, _ = _text_size(draw, text, font)
        if width <= max_width:
            return font
    return fallback


def _draw_text_centered(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x = box[0] + (box[2] - box[0] - width) / 2 - bbox[0]
    y = box[1] + (box[3] - box[1] - height) / 2 - bbox[1]
    draw.text((x, y), text, fill=fill, font=font)


def _draw_font_specimen_row(
    draw: ImageDraw.ImageDraw,
    font_path: str,
    text: str,
    box: tuple[int, int, int, int],
    start_size: int,
    fill: tuple[int, int, int],
) -> bool:
    font = _fit_truetype_font(font_path, text, draw, box[2] - box[0], start_size)
    if font is None:
        return False
    bbox = draw.textbbox((0, 0), text, font=font)
    y = box[1] + (box[3] - box[1] - (bbox[3] - bbox[1])) / 2 - bbox[1]
    draw.text((box[0], y), text, fill=fill, font=font)
    return True


def _font_supports_text(codepoints: list[int], text: str) -> bool:
    if not codepoints:
        return False
    available = set(codepoints)
    return all(ord(char) in available for char in text if char.strip())


def _draw_font_text_specimen(draw: ImageDraw.ImageDraw, font_path: str, codepoints: list[int], size: int) -> bool:
    width = size * 2
    try:
        charset_font = ImageFont.truetype(font_path, 24)
    except OSError:
        return False
    cjk_text = "中国智造，慧及全球 创新设计 数字内容"
    cjk_font = charset_font if _font_supports_text(codepoints, cjk_text) else _default_font(24)
    draw.text((0, 78), cjk_text, fill=(0, 0, 0), font=cjk_font)
    draw.text((0, 106), "abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ", fill=(0, 0, 0), font=charset_font)
    draw.text((0, 132), "1234567890.:,; ' \" (!?) +-*/=", fill=(0, 0, 0), font=charset_font)
    draw.line((0, 160, width, 160), fill=(0, 0, 0))

    y = 168
    for point_size in (12, 18, 24, 36, 48, 60, 72):
        if not _draw_font_viewer_sample_line(draw, font_path, codepoints, point_size, y, width):
            return False
        y += max(point_size + 8, 24)
    return True


def _fallback_font_display_name(title: str) -> str:
    stem = Path(title).stem or str(title or "Font")
    stem = stem.replace("_", " ").replace("-", " ")
    stem = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", stem)
    stem = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", stem)
    return " ".join(stem.split()) or "Font"


def _font_name_value(font, name_ids: tuple[int, ...]) -> str:
    if font is None:
        return ""
    for name_id in name_ids:
        for record in font["name"].names:
            if record.nameID != name_id:
                continue
            try:
                value = record.toUnicode().strip()
            except Exception:
                continue
            if value:
                return value
    return ""


def _font_viewer_metadata(font_path: str, title: str) -> tuple[str, str, str]:
    display_name = _fallback_font_display_name(title)
    version = "Unknown"
    suffix = Path(font_path).suffix.lower()
    outline = "OpenType Outlines" if suffix == ".otf" else "TrueType Outlines"

    if TTFont is None:
        return display_name, version, outline
    try:
        font = TTFont(font_path, lazy=True)
        display_name = _font_name_value(font, (1, 4)) or display_name
        version = _font_name_value(font, (5,)) or version
        version = re.sub(r"(?i)^version\s+", "", version).strip() or "Unknown"
        outline = "OpenType Outlines" if font.sfntVersion == "OTTO" else "TrueType Outlines"
        font.close()
    except Exception:
        return display_name, version, outline
    return display_name, version, outline


def _draw_font_viewer_header(
    draw: ImageDraw.ImageDraw,
    metadata: tuple[str, str, str],
    width: int,
) -> bool:
    display_name, version, outline = metadata
    label_font = _default_font(13)
    draw.text((0, 8), f"Font name: {display_name}", fill=(0, 0, 0), font=label_font)
    draw.text((0, 28), f"Version: {version}", fill=(0, 0, 0), font=label_font)
    draw.text((0, 48), outline, fill=(0, 0, 0), font=label_font)
    draw.line((0, 70, width, 70), fill=(0, 0, 0))
    return True


def _draw_font_viewer_sample_line(
    draw: ImageDraw.ImageDraw,
    font_path: str,
    codepoints: list[int],
    point_size: int,
    y: int,
    width: int,
) -> bool:
    try:
        sample_font = ImageFont.truetype(font_path, point_size)
    except OSError:
        return False
    label_font = _default_font(13)
    cjk_text = "中国智造，慧及全球 "
    cjk_font = sample_font if _font_supports_text(codepoints, cjk_text) else _default_font(max(12, point_size))
    draw.text((0, y + max(0, point_size // 9)), str(point_size), fill=(0, 0, 0), font=label_font)

    x = 30
    segments: tuple[tuple[str, ImageFont.ImageFont], ...] = (
        (cjk_text, cjk_font),
        ("Innovation in China ", sample_font),
        ("0123456789", sample_font),
    )
    for text, font in segments:
        draw.text((x, y), text, fill=(0, 0, 0), font=font)
        bbox = draw.textbbox((x, y), text, font=font)
        x = bbox[2]
        if x > width + 120:
            break
    return True


def _draw_font_glyph_grid(draw: ImageDraw.ImageDraw, font_path: str, codepoints: list[int], size: int) -> bool:
    glyphs = [cp for cp in codepoints if 0xE000 <= cp <= 0xF8FF]
    if not glyphs:
        glyphs = [cp for cp in codepoints if 0x20 <= cp <= 0xFFFF and cp not in {0x7F, 0xA0}]
    if not glyphs:
        return False
    glyphs = glyphs[:50]
    try:
        glyph_font = ImageFont.truetype(font_path, 36)
    except OSError:
        return False

    label_font = _default_font(10)
    width = size * 2
    _draw_font_viewer_header(draw, _font_viewer_metadata(font_path, Path(font_path).name), width)
    cols = 10
    cell_w = 86
    cell_h = 74
    x0 = 40
    y0 = 88
    for idx, cp in enumerate(glyphs):
        row, col = divmod(idx, cols)
        x = x0 + col * cell_w
        y = y0 + row * cell_h
        draw.rounded_rectangle((x, y, x + 68, y + 50), radius=7, fill=(255, 255, 255), outline=(211, 216, 224))
        _draw_text_centered(draw, (x + 6, y + 5, x + 62, y + 43), chr(cp), glyph_font, (18, 23, 31))
        draw.text((x, y + 55), f"U+{cp:04X}", fill=(92, 99, 110), font=label_font)
    return True


def _save_font_sheet(font_path: str, output_path: Path, title: str, size: int = 512) -> None:
    sheet_path = _font_pack_sheet_image_path(font_path)
    if sheet_path:
        with Image.open(sheet_path) as image:
            rgba = image.convert("RGBA")
            _resize_rgba_to_preview(rgba, output_path, size, _visible_content_preview_background(rgba, (72, 84, 96)))
            return

    codepoints = _font_codepoints(font_path)
    bg = Image.new("RGB", (size * 2, size), (255, 255, 255))
    draw = ImageDraw.Draw(bg)

    latin_score = _font_support_count(codepoints, "AaBbCc0123456789")
    drew_preview = False
    if codepoints and latin_score < 10:
        drew_preview = _draw_font_glyph_grid(draw, font_path, codepoints, size)
    if not drew_preview:
        metadata = _font_viewer_metadata(font_path, title)
        _draw_font_viewer_header(draw, metadata, size * 2)
        drew_preview = _draw_font_text_specimen(draw, font_path, codepoints, size)
    if not drew_preview:
        draw.text((46, 160), "Font preview unavailable", fill=(20, 24, 30), font=_default_font(24))
    bg.save(output_path, format="WEBP", lossless=True, quality=100)


def _preview_type_dir_name(resource_type: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(resource_type or "").strip()).strip("._-")
    return name or "unknown"


class CrawlerThumbnailPolicy:
    def __init__(self, output_dir: str, max_size: int = 512):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_size = max_size
        self.generator = ThumbnailGenerator(str(self.output_dir))

    def _output_dir_for(self, entity: ResourceProcessingEntity) -> Path:
        output_dir = self.output_dir / _preview_type_dir_name(entity.resource_type)
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def _generator_for(self, entity: ResourceProcessingEntity) -> ThumbnailGenerator:
        return ThumbnailGenerator(str(self._output_dir_for(entity)))

    async def generate_previews(self, entity: ResourceProcessingEntity) -> list[PreviewInfo]:
        resource_type = entity.resource_type
        if resource_type == PACK_RESOURCE_TYPE:
            return await self._generate_pack_previews(entity)
        if resource_type == SINGLE_IMAGE_RESOURCE_TYPE:
            return [await self._generate_single_image_preview(entity)]
        if resource_type == ATLAS_RESOURCE_TYPE:
            return [await self._generate_atlas_preview(entity)]
        if resource_type == TILED_MAP_RESOURCE_TYPE:
            return [await self._generate_tiled_map_preview(entity)]
        if resource_type in {TILESET_RESOURCE_TYPE, TILED_TILESET_RESOURCE_TYPE}:
            return await self._generate_tileset_previews(entity)
        if resource_type == ANIMATION_SEQUENCE_RESOURCE_TYPE:
            return [await self._generate_animation_preview(entity)]
        if resource_type == SPINE_SKELETON_RESOURCE_TYPE:
            return [await self._generate_spine_preview(entity)]
        if resource_type == SPRITER_RESOURCE_TYPE:
            return [await self._generate_spriter_preview(entity)]
        if resource_type == AUDIO_FILE_RESOURCE_TYPE:
            return [await self._generate_audio_preview(entity)]
        if resource_type == FONT_FILE_RESOURCE_TYPE:
            return [await self._generate_font_preview(entity)]
        return [await self._generate_metadata_preview(entity, mode="metadata_only")]

    async def _generate_single_image_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        primary = entity.primary_file or (entity.files[0] if entity.files else None)
        if primary is None:
            return await self._generate_metadata_preview(entity, mode="metadata_only")

        output_dir = self._output_dir_for(entity)
        ext = Path(primary.file_path).suffix.lower()
        if ext in RASTER_EXTS:
            preview_path = output_dir / f"{entity.content_md5}_preview.webp"
            await asyncio.get_running_loop().run_in_executor(
                None,
                _save_existing_raster_preview,
                primary.file_path,
                preview_path,
                self.max_size,
            )
            return self._preview_info(
                str(preview_path),
                PreviewStrategy.STATIC,
                "direct",
                "high",
                source_path_for_solid_check=primary.file_path,
            )
        if ext in SVG_EXTS:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp:
                temp_path = temp.name
            try:
                if _try_rasterize_svg(primary.file_path, temp_path):
                    preview_path = output_dir / f"{entity.content_md5}_svg.webp"
                    await asyncio.get_running_loop().run_in_executor(
                        None,
                        _save_existing_raster_preview,
                        temp_path,
                        preview_path,
                        self.max_size,
                    )
                    return self._preview_info(
                        str(preview_path),
                        PreviewStrategy.STATIC,
                        "direct",
                        "medium",
                        source_path_for_solid_check=temp_path,
                    )
            finally:
                Path(temp_path).unlink(missing_ok=True)
        return await self._generate_metadata_preview(entity, mode="fallback")

    async def _generate_tileset_previews(self, entity: ResourceProcessingEntity) -> list[PreviewInfo]:
        image_paths = [f.file_path for f in entity.files if Path(f.file_path).suffix.lower() in RASTER_EXTS]
        if not image_paths:
            return [await self._generate_metadata_preview(entity, mode="metadata_only")]

        output_dir = self._output_dir_for(entity)
        tsx_paths = [f.file_path for f in entity.files if Path(f.file_path).suffix.lower() == ".tsx"]
        with tempfile.TemporaryDirectory(prefix="tiled_tileset_", dir=str(output_dir)) as temp_text:
            prepared_image_paths = _prepare_tiled_tileset_images(image_paths, tsx_paths, Path(temp_text))
            overview_path = _find_tileset_overview_image(prepared_image_paths)
            primary_path = output_dir / f"{entity.content_md5}_tileset.webp"
            if (
                entity.resource_type == TILED_TILESET_RESOURCE_TYPE
                and _tiled_tileset_should_reflow(tsx_paths, prepared_image_paths, overview_path, self.max_size)
            ):
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None,
                        _save_tiled_tileset_reflow_preview,
                        tsx_paths,
                        primary_path,
                        self.max_size,
                    )
                    preview = self._preview_info(
                        str(primary_path),
                        PreviewStrategy.CONTACT_SHEET,
                        "reflowed_tilesheet",
                        "high",
                    )
                    if preview.path:
                        return [preview]
                except Exception:
                    pass

            if overview_path:
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    _save_existing_raster_preview,
                    overview_path,
                    primary_path,
                    self.max_size,
                )
                previews = [self._preview_info(str(primary_path), PreviewStrategy.STATIC, "verified_overview", "high")]
            else:
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    _save_tileset_sheet,
                    prepared_image_paths,
                    primary_path,
                    self.max_size,
                )
                previews = [self._preview_info(str(primary_path), PreviewStrategy.CONTACT_SHEET, "packed_tilesheet", "high")]

        return previews

    async def _generate_atlas_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        xml_paths = [f.file_path for f in entity.files if Path(f.file_path).suffix.lower() == ".xml"]
        raster_paths = [f.file_path for f in entity.files if Path(f.file_path).suffix.lower() in RASTER_EXTS]
        if not xml_paths:
            if len(raster_paths) == 1:
                output_path = self._output_dir_for(entity) / f"{entity.content_md5}_atlas.webp"
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    _save_existing_raster_preview,
                    raster_paths[0],
                    output_path,
                    self.max_size,
                )
                return self._preview_info(
                    str(output_path),
                    PreviewStrategy.STATIC,
                    "companion_raster",
                    "high",
                    source_path_for_solid_check=raster_paths[0],
                )
            if raster_paths:
                output_path = self._output_dir_for(entity) / f"{entity.content_md5}_atlas.webp"
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    _save_contact_sheet,
                    sorted(raster_paths, key=_natural_sort_key),
                    output_path,
                    self.max_size,
                )
                return self._preview_info(str(output_path), PreviewStrategy.CONTACT_SHEET, "companion_rasters", "high")
            return await self._generate_metadata_preview(entity, mode="metadata_only")

        output_path = self._output_dir_for(entity) / f"{entity.content_md5}_atlas.webp"
        solid_check_source_path = _atlas_solid_check_source_path(xml_paths, raster_paths)
        await asyncio.get_running_loop().run_in_executor(
            None,
            _save_atlas_sheet,
            xml_paths,
            raster_paths,
            output_path,
            self.max_size,
        )
        return self._preview_info(
            str(output_path),
            PreviewStrategy.CONTACT_SHEET,
            "composed",
            "high",
            source_path_for_solid_check=solid_check_source_path,
        )

    async def _generate_tiled_map_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        tmx_paths = [
            f.file_path
            for f in entity.files
            if Path(f.file_path).suffix.lower() in TMX_EXTS and Path(f.file_path).exists()
        ]
        raster_paths = [f.file_path for f in entity.files if Path(f.file_path).suffix.lower() in RASTER_EXTS]
        if not tmx_paths and not raster_paths:
            return await self._generate_metadata_preview(entity, mode="metadata_only")

        output_path = self._output_dir_for(entity) / f"{entity.content_md5}_tiled_map.webp"
        strategy, mode = await asyncio.get_running_loop().run_in_executor(
            None,
            _save_tiled_map_preview,
            tmx_paths,
            raster_paths,
            output_path,
            self.max_size,
        )
        return self._preview_info(str(output_path), strategy, mode, "high")

    async def _generate_animation_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        image_paths = [f.file_path for f in entity.files if Path(f.file_path).suffix.lower() in RASTER_EXTS]
        if len(image_paths) < 2:
            return await self._generate_metadata_preview(entity, mode="metadata_only")

        sampled = _sample_paths(sorted(image_paths, key=_natural_sort_key), 12)
        gif_path = self._output_dir_for(entity) / f"{entity.content_md5}_sequence.gif"
        await asyncio.get_running_loop().run_in_executor(None, _save_gif, sampled, gif_path, self.max_size)
        return self._preview_info(str(gif_path), PreviewStrategy.GIF, "composed", "high")

    async def _generate_spine_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        atlas_paths = [
            f.file_path
            for f in entity.files
            if _is_spine_atlas_path(f.file_path) and Path(f.file_path).exists()
        ]
        raster_paths = [f.file_path for f in entity.files if Path(f.file_path).suffix.lower() in RASTER_EXTS]
        if not atlas_paths and not raster_paths:
            return await self._generate_metadata_preview(entity, mode="metadata_only")

        output_path = self._output_dir_for(entity) / f"{entity.content_md5}_spine.webp"
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                _save_spine_skeleton_preview,
                atlas_paths,
                raster_paths,
                output_path,
                self.max_size,
            )
        except Exception:
            return await self._generate_metadata_preview(entity, mode="fallback", title_prefix="Spine")

        return self._preview_info(
            str(output_path),
            result["strategy"],
            result["mode"],
            result["confidence"],
            source_path_for_solid_check=result.get("source_path", ""),
        )

    async def _generate_spriter_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        scml_paths = [
            f.file_path
            for f in entity.files
            if Path(f.file_path).suffix.lower() == ".scml" and Path(f.file_path).exists()
        ]
        raster_paths = [f.file_path for f in entity.files if Path(f.file_path).suffix.lower() in RASTER_EXTS]
        if not scml_paths and not raster_paths:
            return await self._generate_metadata_preview(entity, mode="metadata_only", title_prefix="Spriter")

        output_dir = self._output_dir_for(entity)
        if scml_paths and raster_paths:
            try:
                from spriter_preview.runtime_preview import generate_spriter_runtime_previews

                previews = await generate_spriter_runtime_previews(
                    entity,
                    output_dir,
                    max_size=self.max_size,
                )
                if previews:
                    return previews[0]
            except Exception as exc:
                fail_reason = str(exc)[:300]
                if raster_paths:
                    output_path = output_dir / f"{entity.content_md5}_spriter_fallback.webp"
                    try:
                        await asyncio.get_running_loop().run_in_executor(
                            None,
                            _save_contact_sheet,
                            sorted(raster_paths, key=_natural_sort_key),
                            output_path,
                            self.max_size,
                        )
                        preview = self._preview_info(
                            str(output_path),
                            PreviewStrategy.CONTACT_SHEET,
                            "runtime_fallback_companion_rasters",
                            "low",
                        )
                        preview.fail_reason = fail_reason
                        return preview
                    except Exception:
                        pass
                preview = await self._generate_metadata_preview(entity, mode="runtime_fallback", title_prefix="Spriter")
                preview.fail_reason = fail_reason
                return preview

        if len(raster_paths) == 1:
            output_path = output_dir / f"{entity.content_md5}_spriter_fallback.webp"
            await asyncio.get_running_loop().run_in_executor(
                None,
                _save_existing_raster_preview,
                raster_paths[0],
                output_path,
                self.max_size,
            )
            return self._preview_info(
                str(output_path),
                PreviewStrategy.STATIC,
                "companion_raster",
                "medium",
                source_path_for_solid_check=raster_paths[0],
            )
        if raster_paths:
            output_path = output_dir / f"{entity.content_md5}_spriter_fallback.webp"
            await asyncio.get_running_loop().run_in_executor(
                None,
                _save_contact_sheet,
                sorted(raster_paths, key=_natural_sort_key),
                output_path,
                self.max_size,
            )
            return self._preview_info(str(output_path), PreviewStrategy.CONTACT_SHEET, "companion_rasters", "medium")
        return await self._generate_metadata_preview(entity, mode="metadata_only", title_prefix="Spriter")

    async def _generate_audio_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        return await self._generate_metadata_preview(entity, mode="metadata_only", title_prefix="Audio")

    async def _generate_font_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        primary = entity.primary_file or (entity.files[0] if entity.files else None)
        if primary and Path(primary.file_path).suffix.lower() in FONT_EXTS:
            output_path = self._output_dir_for(entity) / f"{entity.content_md5}_font.webp"
            await asyncio.get_running_loop().run_in_executor(
                None,
                _save_font_sheet,
                primary.file_path,
                output_path,
                entity.title or primary.file_name,
                self.max_size,
            )
            return self._preview_info(str(output_path), PreviewStrategy.STATIC, "composed", "medium")
        return await self._generate_metadata_preview(entity, mode="metadata_only")

    async def _generate_pack_preview(self, entity: ResourceProcessingEntity) -> PreviewInfo:
        return (await self._generate_pack_previews(entity))[0]

    async def _generate_pack_previews(self, entity: ResourceProcessingEntity) -> list[PreviewInfo]:
        if _is_pure_audio_pack(entity):
            output_path = self._output_dir_for(entity) / f"{entity.content_md5}_pack.webp"
            await asyncio.get_running_loop().run_in_executor(
                None,
                _save_audio_pack_summary_card,
                entity,
                output_path,
                self.max_size,
            )
            return [self._preview_info(str(output_path), PreviewStrategy.STATIC, "audio_summary", "medium")]

        official_previews = _pack_official_preview_paths(entity)
        if official_previews:
            previews = []
            for index, official_preview in enumerate(official_previews):
                suffix = "_pack.webp" if index == 0 else f"_pack_gallery_{index + 1:02d}.webp"
                output_path = self._output_dir_for(entity) / f"{entity.content_md5}{suffix}"
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    _save_official_pack_preview,
                    official_preview,
                    output_path,
                    self.max_size,
                )
                preview = self._preview_info(
                    str(output_path),
                    PreviewStrategy.STATIC,
                    "official_preview" if index == 0 else "official_preview_gallery",
                    "high",
                    source_path_for_solid_check=official_preview,
                )
                if index > 0:
                    preview.role = "gallery"
                previews.append(preview)
            return previews

        child_preview_records = _pack_child_resource_records(entity)
        if not child_preview_records:
            child_preview_records = _pack_child_preview_records(entity)
        child_preview_pages = _pack_preview_pages(child_preview_records, entity)
        if child_preview_pages:
            output_path = self._output_dir_for(entity) / f"{entity.content_md5}_pack.webp"
            collage_size = _pack_preview_canvas_size(child_preview_records, entity, self.max_size)
            showcase_created = False
            if _pack_allows_generated_showcase(entity, child_preview_records):
                showcase_created = await asyncio.get_running_loop().run_in_executor(
                    None,
                    _save_pack_showcase_preview,
                    entity,
                    child_preview_records,
                    output_path,
                    self.max_size,
                )
            if showcase_created:
                preview = self._preview_info(
                    str(output_path),
                    PreviewStrategy.STATIC,
                    "generated_showcase",
                    "medium",
                )
                if preview.path:
                    previews = [preview]
                    for page_index, page_paths in enumerate(child_preview_pages, start=2):
                        gallery_path = self._output_dir_for(entity) / f"{entity.content_md5}_pack_gallery_{page_index:02d}.webp"
                        await asyncio.get_running_loop().run_in_executor(
                            None,
                            _save_pack_collage,
                            page_paths,
                            gallery_path,
                            entity.title or entity.pack_name or "Pack",
                            collage_size,
                            len(page_paths),
                        )
                        gallery = self._preview_info(
                            str(gallery_path),
                            PreviewStrategy.CONTACT_SHEET,
                            "child_previews_gallery",
                            "medium",
                        )
                        gallery.role = "gallery"
                        previews.append(gallery)
                    return previews

            first_page = child_preview_pages[0]
            if len(first_page) == 1 and len(child_preview_pages) == 1:
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    _save_pack_single_item_preview,
                    first_page[0],
                    output_path,
                    self.max_size,
                )
                return [self._preview_info(str(output_path), PreviewStrategy.STATIC, "child_preview", "high")]

            await asyncio.get_running_loop().run_in_executor(
                None,
                _save_pack_collage,
                first_page,
                output_path,
                entity.title or entity.pack_name or "Pack",
                collage_size,
                len(first_page),
            )
            previews = [self._preview_info(str(output_path), PreviewStrategy.CONTACT_SHEET, "child_previews", "high")]
            for page_index, page_paths in enumerate(child_preview_pages[1:], start=2):
                gallery_path = self._output_dir_for(entity) / f"{entity.content_md5}_pack_gallery_{page_index:02d}.webp"
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    _save_pack_collage,
                    page_paths,
                    gallery_path,
                    entity.title or entity.pack_name or "Pack",
                    collage_size,
                    len(page_paths),
                )
                gallery = self._preview_info(
                    str(gallery_path),
                    PreviewStrategy.CONTACT_SHEET,
                    "child_previews_gallery",
                    "medium",
                )
                gallery.role = "gallery"
                previews.append(gallery)
            return previews

        image_paths = [f.file_path for f in entity.files if Path(f.file_path).suffix.lower() in RASTER_EXTS]
        if image_paths:
            image_pages = _pack_direct_image_pages(image_paths)
            direct_collage_size = max(self.max_size, PACK_GENERATED_COLLAGE_SIZE)
            output_path = self._output_dir_for(entity) / f"{entity.content_md5}_pack.webp"
            await asyncio.get_running_loop().run_in_executor(
                None,
                _save_pack_collage,
                image_pages[0],
                output_path,
                entity.title or entity.pack_name or "Pack",
                direct_collage_size,
                len(image_pages[0]),
            )
            previews = [self._preview_info(str(output_path), PreviewStrategy.CONTACT_SHEET, "composed", "medium")]
            for page_index, page_paths in enumerate(image_pages[1:], start=2):
                gallery_path = self._output_dir_for(entity) / f"{entity.content_md5}_pack_gallery_{page_index:02d}.webp"
                await asyncio.get_running_loop().run_in_executor(
                    None,
                    _save_pack_collage,
                    page_paths,
                    gallery_path,
                    entity.title or entity.pack_name or "Pack",
                    direct_collage_size,
                    len(page_paths),
                )
                gallery = self._preview_info(
                    str(gallery_path),
                    PreviewStrategy.CONTACT_SHEET,
                    "composed_gallery",
                    "medium",
                )
                gallery.role = "gallery"
                previews.append(gallery)
            return previews
        return [await self._generate_metadata_preview(entity, mode="metadata_only", title_prefix="Pack")]

    async def _generate_metadata_preview(
        self,
        entity: ResourceProcessingEntity,
        mode: str,
        title_prefix: str = "",
    ) -> PreviewInfo:
        output_path = self._output_dir_for(entity) / f"{entity.content_md5}_metadata.webp"
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
        source_path_for_solid_check: str = "",
    ) -> PreviewInfo:
        passed, reason = validate_preview(preview_path)
        if (
            not passed
            and source_path_for_solid_check
            and _solid_source_matches_preview_failure(source_path_for_solid_check, reason)
        ):
            passed, reason = validate_preview(preview_path, allow_solid_color=True)
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
