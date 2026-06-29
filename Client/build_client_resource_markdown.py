from __future__ import annotations

import datetime as dt
import base64
import gzip
import hashlib
import ntpath
import re
import shutil
import sqlite3
import xml.etree.ElementTree as ET
import zlib
from collections import Counter, defaultdict
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    from fontTools.ttLib import TTFont
except Exception:  # pragma: no cover - optional runtime dependency
    TTFont = None


REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "data" / "databases" / "pipeline.db"
OUT_PATH = REPO_ROOT / "data" / "reports" / "client_resource_summary.md"
ASSET_DIR = REPO_ROOT / "data" / "reports" / "client_resource_summary_assets"
WORKSPACE = REPO_ROOT
RASTER_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
FONT_EXTS = {".ttf", ".otf"}
SAMPLES_PER_TYPE = 5
EXCLUDED_RESOURCE_TYPES = {"tiled_tileset"}

DISPLAY_NAMES = {
    "single_image": "单图",
    "audio_file": "音频文件",
    "animation_sequence": "动画序列",
    "tileset": "瓦片集",
    "pack": "资源包",
    "atlas": "图集",
    "tiled_map": "Tiled 地图",
    "font_file": "字体文件",
}

STRUCTURES = {
    "single_image": "一个资源任务对应一张主图片；主文件在 resource_file 中以 is_primary=1 标记。",
    "audio_file": "一个资源任务对应一个音频主文件；预览通常是说明型卡片图。",
    "animation_sequence": "一个资源任务包含多张连续帧图片；文件角色通常是 frame/main，预览通常为抽帧 GIF。",
    "tileset": "一个资源任务包含一组瓦片图片；预览优先复用同包已有的 tilemap/tilesheet/spritesheet 总览图，找不到时回退到 contact_sheet 拼贴图。",
    "pack": "一个资源任务代表完整素材包；resource_path 常为 __pack__，下挂大量主文件和附件。",
    "atlas": "一个资源任务包含图集图片和索引/描述文件，常见组合为 PNG + XML。",
    "tiled_map": "一个资源任务以 TMX 地图为核心，并关联地图使用的图片素材；预览优先按 TMX 渲染，没有 TMX 时才使用图片文件预览。",
    "font_file": "一个资源任务对应一个字体文件，通常为 TTF 或 OTF。",
}

ROLE_NAMES = {
    "main": "主体",
    "attachment": "附件",
    "frame": "帧",
    "tile": "瓦片",
    "gallery": "图库",
}


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def fmt_int(value: int | None) -> str:
    return f"{int(value or 0):,}"


def type_title(resource_type: str) -> str:
    return f"{DISPLAY_NAMES.get(resource_type, resource_type)} (`{resource_type}`)"


def clean(value: object, limit: int = 90) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def md_escape(text: object) -> str:
    return clean(text, 300).replace("|", "\\|")


def preview_link(path_value: str) -> str:
    path = Path(path_value)
    try:
        return path.resolve().relative_to(WORKSPACE).as_posix()
    except ValueError:
        return path_value.replace("\\", "/")


def default_font(size: int = 18):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def image_to_rgb(image: Image.Image, background: tuple[int, int, int] = (245, 245, 245)) -> Image.Image:
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (*background, 255))
        bg.alpha_composite(rgba)
        return bg.convert("RGB")
    return image.convert("RGB")


def resize_to_fit(image: Image.Image, max_width: int, max_height: int, *, allow_upscale: bool = True) -> Image.Image:
    scale = min(max_width / image.width, max_height / image.height)
    if not allow_upscale and scale > 1:
        scale = 1
    new_size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
    resample = Image.Resampling.NEAREST if scale > 1 else Image.Resampling.LANCZOS
    return image.resize(new_size, resample)


def save_thumbnail(input_path: str, output_path: Path, size: int = 512) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(input_path) as image:
        image = image_to_rgb(image)
        image = resize_to_fit(image, size - 32, size - 32, allow_upscale=True)
        canvas = Image.new("RGB", (size, size), (246, 247, 249))
        canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
        canvas.save(output_path, format="WEBP")
    return str(output_path)


def copy_preview_asset(input_path: str, output_base: Path) -> str:
    source = Path(input_path)
    suffix = source.suffix or ".webp"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:10]
    output_path = output_base.with_name(f"{output_base.name}_h{digest}").with_suffix(suffix)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output_path)
    return str(output_path)


def save_svg_preview(svg_path: str, output_path: Path, size: int = 512) -> str:
    """Wrap SVG in a visible neutral background.

    Some source SVGs are white glyphs on transparent canvas, which disappear on
    white Markdown backgrounds. Keeping SVG output avoids adding a renderer
    dependency while making the preview visible.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw = Path(svg_path).read_text(encoding="utf-8", errors="ignore")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        shutil.copyfile(svg_path, output_path)
        return str(output_path)

    def _number(value: str | None, fallback: float) -> float:
        if not value:
            return fallback
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        return float(match.group(0)) if match else fallback

    view_box = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if view_box:
        parts = [float(p) for p in re.split(r"[\s,]+", view_box.strip()) if p]
        if len(parts) == 4:
            min_x, min_y, src_w, src_h = parts
        else:
            min_x, min_y, src_w, src_h = 0.0, 0.0, _number(root.attrib.get("width"), 64), _number(root.attrib.get("height"), 64)
    else:
        min_x, min_y = 0.0, 0.0
        src_w = _number(root.attrib.get("width"), 64)
        src_h = _number(root.attrib.get("height"), 64)

    raw_lower = raw.lower()
    svg_start = raw_lower.find("<svg")
    inner_start = raw.find(">", svg_start)
    inner_end = raw_lower.rfind("</svg>")
    inner = raw[inner_start + 1:inner_end] if inner_start >= 0 and inner_end > inner_start else ""
    scale = min((size - 64) / max(src_w, 1), (size - 64) / max(src_h, 1))
    tx = (size - src_w * scale) / 2 - min_x * scale
    ty = (size - src_h * scale) / 2 - min_y * scale
    wrapped = f"""<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
  <rect width="{size}" height="{size}" fill="#9AA4B2"/>
  <rect x="16" y="16" width="{size - 32}" height="{size - 32}" rx="18" fill="#EEF1F5" opacity="0.32"/>
  <g transform="translate({tx:.4f} {ty:.4f}) scale({scale:.6f})">
{inner}
  </g>
</svg>
"""
    output_path.write_text(wrapped, encoding="utf-8")
    return str(output_path)


def sample_paths(paths: list[str], limit: int) -> list[str]:
    if len(paths) <= limit:
        return paths
    step = (len(paths) - 1) / max(limit - 1, 1)
    return [paths[round(idx * step)] for idx in range(limit)]


def natural_key(path: str) -> list[tuple[int, object]]:
    name = ntpath.basename(path).lower()
    parts: list[tuple[int, object]] = []
    buf = ""
    digit = False
    for ch in name:
        if ch.isdigit():
            if buf and not digit:
                parts.append((1, buf))
                buf = ""
            buf += ch
            digit = True
        else:
            if buf and digit:
                parts.append((0, int(buf)))
                buf = ""
            buf += ch
            digit = False
    if buf:
        parts.append((0, int(buf)) if digit else (1, buf))
    return parts


def save_contact_sheet(paths: list[str], output_path: Path, size: int = 512, max_items: int = 16) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    usable = [p for p in paths if Path(p).exists() and Path(p).suffix.lower() in RASTER_EXTS]
    if not usable:
        raise ValueError("no raster files for contact sheet")
    chosen = sample_paths(sorted(usable, key=natural_key), max_items)
    grid = min(4, max(2, round(len(chosen) ** 0.5 + 0.499)))
    cell = size // grid
    padding = 8
    sheet = Image.new("RGB", (size, size), (246, 247, 249))
    for idx, path in enumerate(chosen[: grid * grid]):
        row, col = divmod(idx, grid)
        with Image.open(path) as image:
            image = image_to_rgb(image)
            image = resize_to_fit(image, cell - padding * 2, cell - padding * 2, allow_upscale=True)
            x = col * cell + (cell - image.width) // 2
            y = row * cell + (cell - image.height) // 2
            sheet.paste(image, (x, y))
    sheet.save(output_path, format="WEBP")
    return str(output_path)


def atlas_source_image_path(xml_path: str, raster_paths: list[str]) -> str:
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
    candidates.extend(Path(path) for path in raster_paths)

    for candidate in candidates:
        if candidate.exists() and candidate.suffix.lower() in RASTER_EXTS:
            return str(candidate)
    return ""


def atlas_display_files(file_rows: list[dict]) -> list[dict]:
    xml_rows = [row for row in file_rows if Path(row.get("file_path") or "").suffix.lower() == ".xml"]
    raster_paths = [
        row["file_path"]
        for row in file_rows
        if Path(row.get("file_path") or "").suffix.lower() in RASTER_EXTS
    ]
    display_rows: list[dict] = []
    seen: set[str] = set()
    for xml_row in sorted(xml_rows, key=lambda row: natural_key(row.get("file_path") or "")):
        xml_path = xml_row.get("file_path") or ""
        image_path = atlas_source_image_path(xml_path, raster_paths)
        if image_path and image_path not in seen:
            display_rows.append(
                {
                    "file_path": image_path,
                    "file_name": Path(image_path).name,
                    "file_format": Path(image_path).suffix.lstrip(".").lower(),
                    "file_role": "main",
                    "is_primary": 1,
                }
            )
            seen.add(image_path)
        if xml_path and xml_path not in seen:
            display_rows.append({**xml_row, "is_primary": 0})
            seen.add(xml_path)
    return display_rows or file_rows


def atlas_regions(xml_path: str) -> list[dict]:
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
        if w <= 0 or h <= 0:
            continue
        regions.append({"x": x, "y": y, "w": w, "h": h, "name": attrs.get("name", "")})
    return regions


def crop_visible_region(image: Image.Image) -> Image.Image | None:
    rgba = image.convert("RGBA")
    alpha_bbox = rgba.getchannel("A").getbbox()
    if alpha_bbox:
        left = max(0, alpha_bbox[0] - 2)
        top = max(0, alpha_bbox[1] - 2)
        right = min(rgba.width, alpha_bbox[2] + 2)
        bottom = min(rgba.height, alpha_bbox[3] + 2)
        return rgba.crop((left, top, right, bottom))

    rgb = rgba.convert("RGB")
    content_bbox = rgb.getbbox()
    if content_bbox:
        return rgba.crop(content_bbox)
    return None


def save_existing_raster_preview(input_path: str, output_path: Path, size: int = 512) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(input_path) as image:
        visible = crop_visible_region(image) or image.convert("RGBA")
        visible.thumbnail((size, size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", visible.size, (246, 247, 249))
        canvas.paste(visible, (0, 0), visible.getchannel("A"))
        canvas.save(output_path, format="WEBP")
    return str(output_path)


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


def is_overview_file_name(path: Path) -> bool:
    stem = path.stem.lower()
    if stem in TILESET_OVERVIEW_FILE_PRIORITY:
        return True
    parent = path.parent.name.lower()
    if parent in TILESET_OVERVIEW_DIR_NAMES:
        return any(token in stem for token in ("tilemap", "tilesheet", "spritesheet"))
    return False


def tileset_overview_score(path: Path, tile_dirs: set[Path]) -> tuple[int, int, list[tuple[int, object]]]:
    stem = path.stem.lower()
    exact_rank = next((idx for idx, name in enumerate(TILESET_OVERVIEW_FILE_PRIORITY) if stem == name), 99)
    contains_rank = next((idx for idx, name in enumerate(TILESET_OVERVIEW_FILE_PRIORITY) if name in stem), 99)
    name_rank = min(exact_rank, contains_rank + 20)
    distance = min(
        (len(set(path.parent.parents) ^ set(tile_dir.parents)) for tile_dir in tile_dirs),
        default=99,
    )
    return name_rank, distance, natural_key(str(path))


def find_tileset_overview_image(image_paths: list[str]) -> str | None:
    tile_dirs = {Path(path).parent for path in image_paths if Path(path).exists()}
    if not tile_dirs:
        return None

    search_dirs: list[Path] = []
    for tile_dir in sorted(tile_dirs, key=lambda item: str(item).lower()):
        for directory in [tile_dir, *list(tile_dir.parents)[:3]]:
            if directory.name.lower() not in {"tile", "tiles", "small tiles"}:
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
    for directory in existing_dirs:
        for child in directory.iterdir():
            if child.is_file() and child.suffix.lower() in RASTER_EXTS and is_overview_file_name(child):
                candidates.append(child)

    if not candidates:
        return None

    candidates = [path for path in candidates if is_tileset_overview_image(path, image_paths)]
    if not candidates:
        return None

    return str(sorted(candidates, key=lambda path: tileset_overview_score(path, tile_dirs))[0])


def median_int(values: list[int]) -> int:
    values = sorted(values)
    return values[len(values) // 2] if values else 0


def tileset_tile_baseline(image_paths: list[str]) -> tuple[int, int, int] | None:
    sample = sample_paths(sorted(image_paths, key=natural_key), min(24, len(image_paths)))
    sizes: list[tuple[int, int]] = []
    for path in sample:
        try:
            with Image.open(path) as image:
                sizes.append((image.width, image.height))
        except Exception:
            continue
    if not sizes:
        return None
    widths = [width for width, _ in sizes]
    heights = [height for _, height in sizes]
    areas = [width * height for width, height in sizes]
    return median_int(widths), median_int(heights), median_int(areas)


def is_tileset_overview_image(path: Path, tile_paths: list[str]) -> bool:
    baseline = tileset_tile_baseline(tile_paths)
    try:
        with Image.open(path) as image:
            width, height = image.size
            has_pixels = bool(image.convert("RGBA").getbbox())
    except Exception:
        return False

    if not has_pixels:
        return False
    if baseline is None:
        return True

    tile_width, tile_height, tile_area = baseline
    if tile_width <= 0 or tile_height <= 0 or tile_area <= 0:
        return True
    if width * height < tile_area * 4:
        return False
    if width < tile_width * 2 and height < tile_height * 2:
        return False
    return True


def save_atlas_preview(xml_paths: list[str], raster_paths: list[str], output_path: Path, size: int = 512, max_items: int = 16) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for xml_path in sorted(xml_paths, key=natural_key):
        image_path = atlas_source_image_path(xml_path, raster_paths)
        regions = atlas_regions(xml_path)
        if not image_path or not regions:
            continue

        crops: list[Image.Image] = []
        with Image.open(image_path) as source:
            source = source.convert("RGBA")
            for region in regions:
                x, y, w, h = region["x"], region["y"], region["w"], region["h"]
                if x < 0 or y < 0 or x + w > source.width or y + h > source.height:
                    continue
                crop = crop_visible_region(source.crop((x, y, x + w, y + h)))
                if crop is not None:
                    crops.append(crop)
        if not crops:
            continue

        indexes = sample_paths([str(i) for i in range(len(crops))], max_items)
        chosen = [crops[int(i)] for i in indexes]
        item_count = len(chosen)
        layout_options = []
        for cols in range(1, min(4, item_count) + 1):
            rows = (item_count + cols - 1) // cols
            layout_options.append((abs(cols - rows), rows * cols - item_count, -cols, cols, rows))
        _, _, _, cols, rows = min(layout_options)
        cell = min(size // cols, size // rows)
        sheet_width = cols * cell
        sheet_height = rows * cell
        padding = 8
        sheet = Image.new("RGB", (sheet_width, sheet_height), (246, 247, 249))
        draw = ImageDraw.Draw(sheet)
        for idx, tile in enumerate(chosen):
            row, col = divmod(idx, cols)
            cell_x = col * cell
            cell_y = row * cell
            draw.rounded_rectangle(
                (cell_x + 5, cell_y + 5, cell_x + cell - 5, cell_y + cell - 5),
                radius=8,
                fill=(174, 184, 198),
            )
            tile = resize_to_fit(tile, cell - padding * 2, cell - padding * 2, allow_upscale=True).convert("RGBA")
            x = cell_x + (cell - tile.width) // 2
            y = cell_y + (cell - tile.height) // 2
            sheet.paste(tile, (x, y), tile.getchannel("A"))
        sheet.save(output_path, format="WEBP")
        return str(output_path)

    return save_contact_sheet(raster_paths, output_path, size=size, max_items=max_items)


def save_sequence_gif(paths: list[str], output_path: Path, size: int = 512) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    usable = [p for p in sorted(paths, key=natural_key) if Path(p).exists() and Path(p).suffix.lower() in RASTER_EXTS]
    if len(usable) < 2:
        raise ValueError("not enough frames for sequence gif")
    frames = []
    for path in sample_paths(usable, 12):
        with Image.open(path) as image:
            image = image_to_rgb(image)
            image = resize_to_fit(image, size - 32, size - 32, allow_upscale=True)
            canvas = Image.new("RGB", (size, size), (246, 247, 249))
            canvas.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
            frames.append(canvas)
    frames[0].save(output_path, save_all=True, append_images=frames[1:], duration=120, loop=0, optimize=True)
    return str(output_path)


def font_support_count(codepoints: set[int], sample: str) -> int:
    if not codepoints:
        return len(sample)
    return sum(1 for char in sample if ord(char) in codepoints)


def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def fit_truetype_font(
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
        width, _ = text_size(draw, text, font)
        if width <= max_width:
            return font
    return fallback


def draw_text_centered(
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


def draw_font_specimen_row(
    draw: ImageDraw.ImageDraw,
    font_path: str,
    text: str,
    box: tuple[int, int, int, int],
    start_size: int,
    fill: tuple[int, int, int],
) -> bool:
    font = fit_truetype_font(font_path, text, draw, box[2] - box[0], start_size)
    if font is None:
        return False
    bbox = draw.textbbox((0, 0), text, font=font)
    y = box[1] + (box[3] - box[1] - (bbox[3] - bbox[1])) / 2 - bbox[1]
    draw.text((box[0], y), text, fill=fill, font=font)
    return True


def font_supports_text(codepoints: set[int], text: str) -> bool:
    if not codepoints:
        return False
    return all(ord(char) in codepoints for char in text if char.strip())


def fallback_font_display_name(title: str) -> str:
    stem = Path(title).stem or str(title or "Font")
    stem = stem.replace("_", " ").replace("-", " ")
    stem = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", stem)
    stem = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", stem)
    return " ".join(stem.split()) or "Font"


def font_name_value(font, name_ids: tuple[int, ...]) -> str:
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


def font_viewer_metadata(font_path: str, title: str) -> tuple[str, str, str]:
    display_name = fallback_font_display_name(title)
    version = "Unknown"
    suffix = Path(font_path).suffix.lower()
    outline = "OpenType Outlines" if suffix == ".otf" else "TrueType Outlines"

    if TTFont is None:
        return display_name, version, outline
    try:
        font = TTFont(font_path, lazy=True)
        display_name = font_name_value(font, (1, 4)) or display_name
        version = font_name_value(font, (5,)) or version
        version = re.sub(r"(?i)^version\s+", "", version).strip() or "Unknown"
        outline = "OpenType Outlines" if font.sfntVersion == "OTTO" else "TrueType Outlines"
        font.close()
    except Exception:
        return display_name, version, outline
    return display_name, version, outline


def draw_font_viewer_header(
    draw: ImageDraw.ImageDraw,
    metadata: tuple[str, str, str],
    width: int,
) -> bool:
    display_name, version, outline = metadata
    label_font = default_font(13)
    draw.text((0, 8), f"Font name: {display_name}", fill=(0, 0, 0), font=label_font)
    draw.text((0, 28), f"Version: {version}", fill=(0, 0, 0), font=label_font)
    draw.text((0, 48), outline, fill=(0, 0, 0), font=label_font)
    draw.line((0, 70, width, 70), fill=(0, 0, 0))
    return True


def draw_font_viewer_sample_line(
    draw: ImageDraw.ImageDraw,
    font_path: str,
    codepoints: set[int],
    point_size: int,
    y: int,
    width: int,
) -> bool:
    try:
        sample_font = ImageFont.truetype(font_path, point_size)
    except OSError:
        return False
    label_font = default_font(13)
    cjk_text = "中国智造，慧及全球 "
    cjk_font = sample_font if font_supports_text(codepoints, cjk_text) else default_font(max(12, point_size))
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


def draw_font_text_specimen(draw: ImageDraw.ImageDraw, font_path: str, codepoints: set[int], size: int) -> bool:
    width = size * 2
    try:
        charset_font = ImageFont.truetype(font_path, 24)
    except OSError:
        return False
    cjk_text = "中国智造，慧及全球 创新设计 数字内容"
    cjk_font = charset_font if font_supports_text(codepoints, cjk_text) else default_font(24)
    draw.text((0, 78), cjk_text, fill=(0, 0, 0), font=cjk_font)
    draw.text((0, 106), "abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ", fill=(0, 0, 0), font=charset_font)
    draw.text((0, 132), "1234567890.:,; ' \" (!?) +-*/=", fill=(0, 0, 0), font=charset_font)
    draw.line((0, 160, width, 160), fill=(0, 0, 0))

    y = 168
    for point_size in (12, 18, 24, 36, 48, 60, 72):
        if not draw_font_viewer_sample_line(draw, font_path, codepoints, point_size, y, width):
            return False
        y += max(point_size + 8, 24)
    return True


def draw_font_glyph_grid(draw: ImageDraw.ImageDraw, font_path: str, codepoints: set[int], size: int) -> bool:
    glyphs = sorted(cp for cp in codepoints if 0xE000 <= cp <= 0xF8FF)
    if not glyphs:
        glyphs = sorted(cp for cp in codepoints if 0x20 <= cp <= 0xFFFF and cp not in {0x7F, 0xA0})
    if not glyphs:
        return False
    glyphs = glyphs[:50]
    try:
        glyph_font = ImageFont.truetype(font_path, 36)
    except OSError:
        return False

    label_font = default_font(10)
    width = size * 2
    draw_font_viewer_header(draw, font_viewer_metadata(font_path, Path(font_path).name), width)
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
        draw_text_centered(draw, (x + 6, y + 5, x + 62, y + 43), chr(cp), glyph_font, (18, 23, 31))
        draw.text((x, y + 55), f"U+{cp:04X}", fill=(92, 99, 110), font=label_font)
    return True


def save_font_preview(font_path: str, output_path: Path, title: str, size: int = 512) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (size * 2, size), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    codepoints = font_codepoints(font_path)

    drew_preview = False
    if codepoints and font_support_count(codepoints, "AaBbCc0123456789") < 10:
        drew_preview = draw_font_glyph_grid(draw, font_path, codepoints, size)
    if not drew_preview:
        draw_font_viewer_header(draw, font_viewer_metadata(font_path, title), size * 2)
        drew_preview = draw_font_text_specimen(draw, font_path, codepoints, size)
    if not drew_preview:
        draw.text((46, 160), "Font preview unavailable", fill=(20, 24, 30), font=default_font(24))
    image.save(output_path, format="WEBP", lossless=True, quality=100)
    return str(output_path)


def font_codepoints(font_path: str) -> set[int]:
    if TTFont is None:
        return set()
    try:
        font = TTFont(font_path)
        return {cp for table in font["cmap"].tables for cp in table.cmap.keys()}
    except Exception:
        return set()


def wrap_text(text: str, width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines = []
    cur = words[0]
    for word in words[1:]:
        cand = f"{cur} {word}"
        if len(cand) <= width:
            cur = cand
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return lines


def save_metadata_card(output_path: Path, title: str, lines: list[str], size: int = 512) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (size, size), (42, 54, 74))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, size - 24, size - 24), radius=18, outline=(119, 141, 169), width=2)
    y = 40
    draw.text((40, y), clean(title, 44), fill=(244, 247, 250), font=default_font(28))
    y += 58
    for line in lines[:10]:
        for wrapped in wrap_text(line, 38):
            draw.text((40, y), wrapped, fill=(222, 230, 240), font=default_font(18))
            y += 26
    image.save(output_path, format="WEBP")
    return str(output_path)


def tsx_image_path(tsx_path: str) -> str:
    try:
        root = ET.parse(tsx_path).getroot()
    except Exception:
        return ""
    image = root.find("image")
    if image is None:
        return ""
    source = image.attrib.get("source", "")
    if not source:
        return ""
    candidate = Path(tsx_path).parent / source
    return str(candidate) if candidate.exists() else ""


def xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def xml_children(elem: ET.Element, name: str | None = None) -> list[ET.Element]:
    children = list(elem)
    if name is None:
        return children
    return [child for child in children if xml_name(child.tag) == name]


def xml_first(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem:
        if xml_name(child.tag) == name:
            return child
    return None


def decode_tiled_data(
    elem: ET.Element,
    expected_count: int,
    encoding: str | None = None,
    compression: str | None = None,
) -> list[int]:
    encoding = (encoding or elem.attrib.get("encoding", "")).lower()
    compression = (compression or elem.attrib.get("compression", "")).lower()
    if encoding == "csv":
        text = "".join(elem.itertext())
        return [int(part.strip()) for part in text.replace("\n", "").split(",") if part.strip()][:expected_count]
    if encoding == "base64":
        raw = base64.b64decode("".join(elem.itertext()).strip())
        if compression == "gzip":
            raw = gzip.decompress(raw)
        elif compression == "zlib":
            raw = zlib.decompress(raw)
        return [int.from_bytes(raw[i : i + 4], "little") for i in range(0, min(len(raw), expected_count * 4), 4)]
    values = []
    for tile in xml_children(elem, "tile"):
        values.append(int(tile.attrib.get("gid", "0") or 0))
    return values[:expected_count]


def tmx_has_nonzero_tiles(tmx_path: str) -> bool:
    try:
        root = ET.parse(tmx_path).getroot()
    except Exception:
        return False
    for layer in xml_children(root, "layer"):
        data = xml_first(layer, "data")
        if data is None:
            continue
        encoding = data.attrib.get("encoding", "")
        compression = data.attrib.get("compression", "")
        chunks = xml_children(data, "chunk")
        if chunks:
            for chunk in chunks:
                width = int(chunk.attrib.get("width", "0") or 0)
                height = int(chunk.attrib.get("height", "0") or 0)
                if any(value for value in decode_tiled_data(chunk, width * height, encoding, compression)):
                    return True
            continue
        width = int(layer.attrib.get("width", root.attrib.get("width", "0")) or 0)
        height = int(layer.attrib.get("height", root.attrib.get("height", "0")) or 0)
        if any(value for value in decode_tiled_data(data, width * height)):
            return True
    return False


def resolve_sample_resource_paths(sample: dict, files: list[dict], suffixes: set[str]) -> list[str]:
    found = [
        row["file_path"]
        for row in files
        if Path(row["file_path"]).suffix.lower() in suffixes and Path(row["file_path"]).exists()
    ]
    if found:
        return found
    rel = str(sample.get("resource_path") or "").replace("/", "\\").strip()
    if not rel:
        return []
    source_dir = Path(str(sample.get("source_directory") or ""))
    candidates = []
    if source_dir:
        candidates.extend([source_dir / rel, source_dir / Path(rel).name, source_dir.parent / rel])
    for row in files:
        file_path = Path(row["file_path"])
        for parent in file_path.parents:
            candidates.append(parent / rel)
    seen = set()
    resolved = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and candidate.suffix.lower() in suffixes:
            resolved.append(str(candidate))
    return resolved


def tsx_info(tsx_path: str) -> dict:
    try:
        root = ET.parse(tsx_path).getroot()
    except Exception:
        return {}
    image = root.find("image")
    if image is None:
        return {}
    source = image.attrib.get("source", "")
    if not source:
        return {}
    image_path = Path(tsx_path).parent / source
    if not image_path.exists():
        return {}
    return {
        "image_path": str(image_path),
        "tilewidth": int(root.attrib.get("tilewidth", "0") or 0),
        "tileheight": int(root.attrib.get("tileheight", "0") or 0),
        "columns": int(root.attrib.get("columns", "0") or 0),
        "tilecount": int(root.attrib.get("tilecount", "0") or 0),
    }


def save_tsx_tileset_preview(tsx_path: str, output_path: Path, size: int = 512, max_items: int = 16) -> str:
    info = tsx_info(tsx_path)
    if not info:
        raise ValueError("invalid tsx image reference")
    tile_w = info["tilewidth"]
    tile_h = info["tileheight"]
    columns = info["columns"]
    tilecount = info["tilecount"]
    if tile_w <= 0 or tile_h <= 0 or columns <= 0 or tilecount <= 0:
        return save_thumbnail(info["image_path"], output_path, size=size)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(info["image_path"]) as source:
        source = image_to_rgb(source)
        candidates = []
        for tile_id in range(tilecount):
            col = tile_id % columns
            row = tile_id // columns
            left = col * tile_w
            top = row * tile_h
            if left + tile_w > source.width or top + tile_h > source.height:
                continue
            crop = source.crop((left, top, left + tile_w, top + tile_h))
            extrema = crop.convert("RGB").getextrema()
            if any(lo != hi for lo, hi in extrema):
                candidates.append(crop)
        if not candidates:
            return save_thumbnail(info["image_path"], output_path, size=size)

        indexes = sample_paths([str(i) for i in range(len(candidates))], max_items)
        chosen = [candidates[int(i)] for i in indexes]
        grid = min(4, max(2, round(len(chosen) ** 0.5 + 0.499)))
        cell = size // grid
        padding = 8
        sheet = Image.new("RGB", (size, size), (246, 247, 249))
        for idx, tile in enumerate(chosen[: grid * grid]):
            row, col = divmod(idx, grid)
            tile = resize_to_fit(tile, cell - padding * 2, cell - padding * 2, allow_upscale=True)
            x = col * cell + (cell - tile.width) // 2
            y = row * cell + (cell - tile.height) // 2
            sheet.paste(tile, (x, y))
        sheet.save(output_path, format="WEBP")
        return str(output_path)


def build_report_preview(sample: dict) -> str:
    typ = sample["resource_type"]
    out_base = ASSET_DIR / f"{sample['id']}_{typ}"
    cached_preview = sample.get("cached_preview_path") or sample.get("preview_path") or ""
    if cached_preview and Path(cached_preview).exists():
        return copy_preview_asset(cached_preview, out_base)
    raise FileNotFoundError(f"missing cached preview for sample {sample.get('id')}")


def summarize_formats(format_rows: list[dict], limit: int = 10) -> str:
    if not format_rows:
        return "无文件记录"
    chunks = [f"`{row['file_format']}` {fmt_int(row['file_count'])}" for row in format_rows[:limit]]
    rest = len(format_rows) - limit
    if rest > 0:
        chunks.append(f"另 {rest} 种")
    return "；".join(chunks)


def common_root(paths: list[str]) -> str:
    if not paths:
        return ""
    if len(paths) == 1:
        return ntpath.dirname(paths[0])
    try:
        root = ntpath.commonpath(paths)
    except ValueError:
        root = ntpath.dirname(paths[0])
    if ntpath.splitext(root)[1]:
        root = ntpath.dirname(root)
    return root


def folder_tree(file_rows: list[dict], max_files: int = 12) -> str:
    if not file_rows:
        return "无文件记录"

    deduped = []
    seen = set()
    for row in file_rows:
        key = (row.get("file_path"), row.get("file_role"), row.get("file_format"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    all_paths = [row["file_path"] for row in deduped if row.get("file_path")]
    root = common_root(all_paths)
    root_label = ntpath.basename(root.rstrip("\\/")) or root
    shown = deduped[:max_files]
    lines = [root_label]
    for row in shown:
        file_path = row["file_path"]
        try:
            rel = ntpath.relpath(file_path, root)
        except ValueError:
            rel = ntpath.basename(file_path)
        prefix = "* " if row.get("is_primary") else "- "
        lines.append(f"  {prefix}{rel.replace(chr(92), '/')}")
    omitted = len(deduped) - len(shown)
    if omitted > 0:
        lines.append(f"  … 另 {fmt_int(omitted)} 个文件")
    return "\n".join(lines)


def get_files(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    return rows(
        conn,
        """
        SELECT file_path, file_name, file_format, file_role, is_primary
        FROM resource_file
        WHERE task_id=?
        ORDER BY is_primary DESC, file_path
        """,
        (task_id,),
    )


def get_best_cached_preview(conn: sqlite3.Connection, task_id: int) -> dict:
    result = rows(
        conn,
        """
        SELECT path AS preview_path, strategy AS preview_strategy, format AS preview_format
        FROM resource_preview
        WHERE task_id=? AND role='primary' AND path IS NOT NULL AND path<>''
        ORDER BY
          CASE WHEN path LIKE '%_metadata.%' THEN 1 ELSE 0 END,
          id DESC
        LIMIT 1
        """,
        (task_id,),
    )
    if result:
        return result[0]
    return {"preview_path": "", "preview_strategy": "", "preview_format": ""}


def tileset_base_resource_path(resource_path: str) -> str:
    parts = [part for part in resource_path.replace("\\", "/").split("/") if part]
    for idx in range(len(parts) - 1, -1, -1):
        if parts[idx].lower() in {"tile", "tiles", "small tiles"}:
            return "/".join(parts[:idx])
    return "/".join(parts[:-1])


def is_overview_resource_path(resource_path: str, base_path: str) -> bool:
    normalized = resource_path.replace("\\", "/").strip("/")
    base = base_path.strip("/")
    if base and not normalized.lower().startswith((base + "/").lower()):
        return False
    remainder = normalized[len(base):].strip("/") if base else normalized
    parts = [part for part in remainder.split("/") if part]
    if len(parts) < 2:
        return False
    if parts[0].lower() not in TILESET_OVERVIEW_DIR_NAMES:
        return False
    return is_overview_file_name(Path(parts[-1]))


def get_related_overview_from_db(conn: sqlite3.Connection, sample: dict, raster_paths: list[str]) -> dict:
    base_path = tileset_base_resource_path(sample.get("resource_path") or "")
    if not base_path:
        return {}

    patterns = [f"{base_path}/{name}/%" for name in sorted(TILESET_OVERVIEW_DIR_NAMES)]
    where_patterns = " OR ".join("rt.resource_path LIKE ?" for _ in patterns)
    candidates = rows(
        conn,
        f"""
        SELECT rt.id AS related_overview_task_id,
               rt.resource_path AS related_overview_resource_path,
               rf.file_path AS related_overview_path,
               rp.path AS related_overview_preview_path
        FROM resource_task rt
        JOIN resource_file rf ON rf.task_id=rt.id AND rf.is_primary=1
        JOIN resource_preview rp ON rp.id = (
          SELECT MAX(id)
          FROM resource_preview
          WHERE task_id=rt.id AND role='primary' AND path IS NOT NULL AND path<>''
        )
        WHERE rt.source=? AND rt.pack_name=? AND rt.resource_type='single_image'
          AND ({where_patterns})
        LIMIT 100
        """,
        (sample.get("source") or "", sample.get("pack_name") or "", *patterns),
    )

    usable = []
    for candidate in candidates:
        overview_path = candidate.get("related_overview_path") or ""
        preview_path = candidate.get("related_overview_preview_path") or ""
        if not is_overview_resource_path(candidate.get("related_overview_resource_path") or "", base_path):
            continue
        if not overview_path or not Path(overview_path).exists() or not Path(preview_path).exists():
            continue
        if not is_tileset_overview_image(Path(overview_path), raster_paths):
            continue
        usable.append(candidate)

    if not usable:
        return {}

    tile_dirs = {Path(path).parent for path in raster_paths if Path(path).exists()}
    return sorted(usable, key=lambda item: tileset_overview_score(Path(item["related_overview_path"]), tile_dirs))[0]


SEQUENCE_ACTION_HINTS = {
    "attack",
    "attacking",
    "been hit",
    "die",
    "dying",
    "fly",
    "hit",
    "idle",
    "jump",
    "run",
    "running",
    "shoot",
    "stand",
    "stopped",
    "talk",
    "walk",
    "walking",
}

TRAILING_FRAME_RE = re.compile(r"^(.+?)(?:[\s_-]+(?:n|s|e|w|ne|nw|se|sw))?(\d{3,4})$", re.IGNORECASE)
GENERIC_TILE_PREFIXES = {"tile", "tiles", "tileset", "tile set", "tilemap", "terrain"}


def is_sequence_like_tileset(files: list[dict]) -> bool:
    """Detect mislabeled action-frame groups that should not be sampled as tilesets."""
    image_rows = [row for row in files if Path(row["file_path"]).suffix.lower() in RASTER_EXTS]
    if len(image_rows) < 3:
        return False

    prefixes: list[str] = []
    frame_numbers: list[int] = []
    for row in image_rows:
        stem = Path(row.get("file_name") or row.get("file_path") or "").stem.lower()
        match = TRAILING_FRAME_RE.match(stem)
        if not match:
            continue
        prefix = match.group(1).strip(" _-")
        if not prefix:
            continue
        prefixes.append(prefix)
        frame_numbers.append(int(match.group(2)))

    if len(prefixes) < max(3, int(len(image_rows) * 0.7)):
        return False

    common_prefix, common_count = Counter(prefixes).most_common(1)[0]
    if common_count < max(3, int(len(image_rows) * 0.7)):
        return False
    if common_prefix in GENERIC_TILE_PREFIXES or common_prefix.startswith("tile "):
        return False

    path_text = " ".join(str(row.get("file_path") or "").lower() for row in image_rows)
    action_like = (
        "reinerstilesets" in path_text
        or " " in common_prefix
        or any(hint in common_prefix for hint in SEQUENCE_ACTION_HINTS)
    )
    return action_like and len(set(frame_numbers)) >= 3


def sample_family_key(resource_type: str, sample: dict) -> str:
    if resource_type == "tileset":
        source = clean(sample.get("source", ""), 200).lower()
        pack = clean(sample.get("pack_name", ""), 200).lower()
        if pack:
            return f"{source}|{pack}"
        resource_path = str(sample.get("resource_path") or "").replace("/", "\\")
        parent = ntpath.dirname(resource_path).lower()
        return f"{source}|{parent or resource_path.lower()}"
    return str(sample.get("id"))


def can_build_preview(resource_type: str, files: list[dict], sample: dict | None = None) -> bool:
    if not files:
        return False
    if resource_type == "tileset" and is_sequence_like_tileset(files):
        return False
    existing = [row for row in files if Path(row["file_path"]).exists()]
    if not existing:
        return False
    primary = next((row for row in existing if row.get("is_primary")), existing[0])
    primary_ext = Path(primary["file_path"]).suffix.lower()
    raster_count = sum(1 for row in existing if Path(row["file_path"]).suffix.lower() in RASTER_EXTS)

    if resource_type == "single_image":
        return primary_ext in RASTER_EXTS or primary_ext == ".svg"
    if resource_type == "animation_sequence":
        return raster_count >= 2
    if resource_type == "tiled_map" and sample is not None:
        tmx_paths = [
            row["file_path"]
            for row in existing
            if Path(row["file_path"]).suffix.lower() == ".tmx"
        ]
        return bool(tmx_paths) and any(tmx_has_nonzero_tiles(path) for path in tmx_paths)
    if resource_type == "atlas":
        xml_paths = [row["file_path"] for row in existing if Path(row["file_path"]).suffix.lower() == ".xml"]
        raster_paths = [row["file_path"] for row in existing if Path(row["file_path"]).suffix.lower() in RASTER_EXTS]
        return any(atlas_source_image_path(xml_path, raster_paths) for xml_path in xml_paths)
    if resource_type in {"tileset", "pack", "tiled_map"}:
        return raster_count >= 1
    if resource_type == "font_file":
        return primary_ext in FONT_EXTS
    if resource_type == "audio_file":
        return True
    return True


def cached_preview_usable(resource_type: str, cached_preview: dict) -> bool:
    path = cached_preview.get("preview_path") or ""
    if not path or not Path(path).exists():
        return False
    if resource_type != "audio_file" and "_metadata" in Path(path).stem.lower():
        return False
    if cached_preview.get("fail_reason"):
        return False
    return True


def preview_content_key(cached_preview: dict) -> str:
    path = cached_preview.get("preview_path") or ""
    if not path:
        return ""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def pick_samples(conn: sqlite3.Connection, resource_type: str, limit: int = SAMPLES_PER_TYPE, file_format: str | None = None) -> list[dict]:
    params: list[object] = [resource_type]
    fmt_join = ""
    fmt_where = ""
    if file_format:
        fmt_join = "JOIN resource_file pf ON pf.task_id=rt.id AND pf.is_primary=1"
        fmt_where = "AND lower(pf.file_format)=?"
        params.append(file_format)
    candidates = rows(
        conn,
        f"""
        WITH file_counts AS (
          SELECT task_id, COUNT(*) AS file_count
          FROM resource_file
          GROUP BY task_id
        ),
        latest_description AS (
          SELECT rd.task_id, rd.main_content, rd.detail_content, rd.full_description
          FROM resource_description rd
          JOIN (
            SELECT task_id, MAX(id) AS max_id
            FROM resource_description
            GROUP BY task_id
          ) latest ON latest.task_id=rd.task_id AND latest.max_id=rd.id
        )
        SELECT rt.id, rt.resource_type, rt.source, rt.title, rt.pack_name, rt.resource_path,
               rt.source_directory,
               rt.source_resource_id, rt.process_state, COALESCE(fc.file_count, 0) AS file_count,
               ld.main_content AS description_main,
               ld.detail_content AS description_detail,
               ld.full_description AS description_full
        FROM resource_task rt
        {fmt_join}
        LEFT JOIN file_counts fc ON fc.task_id = rt.id
        LEFT JOIN latest_description ld ON ld.task_id = rt.id
        WHERE rt.resource_type=? {fmt_where}
        GROUP BY rt.id
        ORDER BY RANDOM()
        LIMIT 800
        """,
        tuple(params),
    )
    cached_candidates = rows(
        conn,
        f"""
        WITH file_counts AS (
          SELECT task_id, COUNT(*) AS file_count
          FROM resource_file
          GROUP BY task_id
        ),
        latest_description AS (
          SELECT rd.task_id, rd.main_content, rd.detail_content, rd.full_description
          FROM resource_description rd
          JOIN (
            SELECT task_id, MAX(id) AS max_id
            FROM resource_description
            GROUP BY task_id
          ) latest ON latest.task_id=rd.task_id AND latest.max_id=rd.id
        ),
        usable_preview AS (
          SELECT task_id, MAX(id) AS preview_id
          FROM resource_preview
          WHERE role='primary'
            AND path IS NOT NULL AND path<>''
            AND lower(path) NOT LIKE '%_metadata.%'
          GROUP BY task_id
        )
        SELECT rt.id, rt.resource_type, rt.source, rt.title, rt.pack_name, rt.resource_path,
               rt.source_directory,
               rt.source_resource_id, rt.process_state, COALESCE(fc.file_count, 0) AS file_count,
               ld.main_content AS description_main,
               ld.detail_content AS description_detail,
               ld.full_description AS description_full
        FROM resource_task rt
        {fmt_join}
        JOIN usable_preview up ON up.task_id = rt.id
        LEFT JOIN file_counts fc ON fc.task_id = rt.id
        LEFT JOIN latest_description ld ON ld.task_id = rt.id
        WHERE rt.resource_type=? {fmt_where}
        GROUP BY rt.id
        ORDER BY RANDOM()
        LIMIT 800
        """,
        tuple(params),
    )
    seen_candidate_ids = {row["id"] for row in candidates}
    candidates.extend(row for row in cached_candidates if row["id"] not in seen_candidate_ids)
    picked: list[dict] = []
    picked_families: set[str] = set()
    picked_preview_keys: set[str] = set()
    for sample in candidates:
        family_key = sample_family_key(resource_type, sample)
        if family_key in picked_families:
            continue
        files = get_files(conn, sample["id"])
        if can_build_preview(resource_type, files, sample):
            cached_preview = get_best_cached_preview(conn, sample["id"])
            if not cached_preview_usable(resource_type, cached_preview):
                continue
            preview_key = preview_content_key(cached_preview)
            if preview_key and preview_key in picked_preview_keys:
                continue
            sample["files"] = files
            sample.update(cached_preview)
            picked.append(sample)
            picked_families.add(family_key)
            if preview_key:
                picked_preview_keys.add(preview_key)
            if len(picked) >= limit:
                return picked

    if len(picked) < limit:
        seen = {sample["id"] for sample in picked}
        for sample in candidates:
            if sample["id"] in seen:
                continue
            family_key = sample_family_key(resource_type, sample)
            if family_key in picked_families:
                continue
            sample["files"] = get_files(conn, sample["id"])
            if resource_type == "tileset" and is_sequence_like_tileset(sample["files"]):
                continue
            cached_preview = get_best_cached_preview(conn, sample["id"])
            if not can_build_preview(resource_type, sample["files"], sample):
                continue
            if not cached_preview_usable(resource_type, cached_preview):
                continue
            preview_key = preview_content_key(cached_preview)
            if preview_key and preview_key in picked_preview_keys:
                continue
            sample.update(cached_preview)
            picked.append(sample)
            seen.add(sample["id"])
            picked_families.add(family_key)
            if preview_key:
                picked_preview_keys.add(preview_key)
            if len(picked) >= limit:
                break
    return picked


def collect() -> dict:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    data: dict = {}
    data["type_counts"] = rows(
        conn,
        """
        SELECT resource_type, COUNT(*) AS resource_count
        FROM resource_task
        WHERE resource_type NOT IN ('tiled_tileset')
        GROUP BY resource_type
        ORDER BY resource_count DESC, resource_type
        """,
    )
    data["total"] = sum(row["resource_count"] for row in data["type_counts"])

    format_rows = rows(
        conn,
        """
        SELECT rt.resource_type, lower(rf.file_format) AS file_format,
               COUNT(*) AS file_count,
               COUNT(DISTINCT rf.task_id) AS resource_count
        FROM resource_file rf
        JOIN resource_task rt ON rt.id = rf.task_id
        WHERE rt.resource_type NOT IN ('tiled_tileset')
        GROUP BY rt.resource_type, lower(rf.file_format)
        ORDER BY rt.resource_type, file_count DESC, file_format
        """,
    )
    data["formats_by_type"] = defaultdict(list)
    for row in format_rows:
        data["formats_by_type"][row["resource_type"]].append(row)

    data["file_stats"] = {
        row["resource_type"]: row
        for row in rows(
            conn,
            """
            WITH c AS (
              SELECT rt.id, rt.resource_type, COUNT(rf.id) AS file_count
              FROM resource_task rt
              LEFT JOIN resource_file rf ON rf.task_id = rt.id
              WHERE rt.resource_type NOT IN ('tiled_tileset')
              GROUP BY rt.id, rt.resource_type
            )
            SELECT resource_type,
                   MIN(file_count) AS min_files,
                   MAX(file_count) AS max_files,
                   ROUND(AVG(file_count), 2) AS avg_files
            FROM c
            GROUP BY resource_type
            """,
        )
    }

    data["single_formats"] = rows(
        conn,
        """
        SELECT lower(rf.file_format) AS file_format, COUNT(*) AS file_count
        FROM resource_file rf
        JOIN resource_task rt ON rt.id = rf.task_id
        WHERE rt.resource_type='single_image' AND rf.is_primary=1
        GROUP BY lower(rf.file_format)
        ORDER BY file_count DESC, file_format
        """,
    )

    samples = []
    for row in data["single_formats"]:
        fmt = row["file_format"]
        for index, sample in enumerate(pick_samples(conn, "single_image", file_format=fmt), start=1):
            sample["sample_label"] = f"单图 {fmt} 示例 {index}"
            samples.append(sample)

    for type_row in data["type_counts"]:
        typ = type_row["resource_type"]
        if typ == "single_image":
            continue
        for index, sample in enumerate(pick_samples(conn, typ), start=1):
            sample["sample_label"] = f"{DISPLAY_NAMES.get(typ, typ)} 示例 {index}"
            samples.append(sample)

    for sample in samples:
        cached_preview_path = sample.get("preview_path") or ""
        cached_preview_strategy = sample.get("preview_strategy") or ""
        sample["cached_preview_path"] = cached_preview_path
        sample["preview_path"] = build_report_preview(sample)
        sample["preview_strategy"] = f"crawler_cached:{cached_preview_strategy or 'primary'}"
        sample["preview_format"] = Path(sample["preview_path"]).suffix.lstrip(".")
    data["samples"] = samples
    conn.close()
    return data


def build_markdown(data: dict) -> str:
    count_lookup = {row["resource_type"]: row["resource_count"] for row in data["type_counts"]}
    lines: list[str] = []
    lines.append("# Client 资源统计简表")
    lines.append("")
    lines.append(f"- 生成日期：{dt.datetime.now().strftime('%Y-%m-%d')}")
    lines.append(f"- 数据来源：`{DB_PATH.name}`")
    lines.append(f"- 统计口径：资源数量按 `resource_task` 统计；格式按 `resource_file.file_format` 统计。")
    lines.append(f"- 总资源数：**{fmt_int(data['total'])}**")
    lines.append("")

    lines.append("## 资源类型汇总")
    lines.append("")
    lines.append("| 资源类型 | 数量 | 结构 | 格式 | 文件数概况 |")
    lines.append("|---|---:|---|---|---|")
    for row in data["type_counts"]:
        typ = row["resource_type"]
        stats = data["file_stats"].get(typ, {})
        lines.append(
            "| "
            + " | ".join(
                [
                    type_title(typ),
                    fmt_int(row["resource_count"]),
                    md_escape(STRUCTURES.get(typ, "资源任务通过 task_id 关联文件和预览。")),
                    md_escape(summarize_formats(data["formats_by_type"].get(typ, []))),
                    f"最少 {fmt_int(stats.get('min_files'))}，平均 {stats.get('avg_files', 0)}，最多 {fmt_int(stats.get('max_files'))}",
                ]
            )
            + " |"
        )
    lines.append("")

    lines.append("## 示例")
    lines.append("")
    lines.append(
        "说明：每种资源类型随机抽取约 5 个示例。文件夹结构中 `*` 表示数据库标记的主文件，"
        "`-` 表示同资源下的其他文件。预览图为本报告从 client 数据库记录的缓存预览复制。"
    )
    lines.append("")

    for sample in data["samples"]:
        typ = sample["resource_type"]
        title = sample["title"] or sample["resource_path"] or sample["source_resource_id"]
        display_files = atlas_display_files(sample["files"]) if typ == "atlas" else sample["files"]
        lines.append(f"### {sample['sample_label']}")
        lines.append("")
        lines.append(f"- 资源类型：{type_title(typ)}")
        lines.append(f"- ID：`{sample['id']}`")
        lines.append(f"- 来源：`{sample['source']}`")
        lines.append(f"- 标题/路径：{clean(title, 140)}")
        lines.append(f"- 所属包：{clean(sample['pack_name'] or '-', 140)}")
        lines.append(f"- 文件数：{fmt_int(len(display_files) if typ == 'atlas' else sample['file_count'])}")
        description = sample.get("description_full") or sample.get("description_main") or "暂无描述"
        lines.append(f"- 描述：{clean(description, 180)}")
        preview_format = sample["preview_format"] or Path(sample["preview_path"]).suffix.lstrip(".") or "未记录"
        lines.append(f"- 预览：`{sample['preview_strategy']}` / `{preview_format}`")
        lines.append("")
        lines.append(f"![预览图]({preview_link(sample['preview_path'])})")
        lines.append("")
        lines.append("文件夹结构：")
        lines.append("")
        lines.append("```text")
        lines.append(folder_tree(display_files))
        lines.append("```")
        lines.append("")

    lines.append("## 单图格式数量")
    lines.append("")
    lines.append("| 格式 | 数量 |")
    lines.append("|---|---:|")
    for row in data["single_formats"]:
        lines.append(f"| `{row['file_format']}` | {fmt_int(row['file_count'])} |")
    lines.append("")
    lines.append("注：这份 Markdown 已去掉 DOCX 版里额外的状态、描述覆盖、上传任务等统计。")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    data = collect()
    OUT_PATH.write_text(build_markdown(data), encoding="utf-8")
    print(f"Wrote {OUT_PATH.resolve()}")
    print(f"Resources: {fmt_int(data['total'])}")
    print(f"Samples: {len(data['samples'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
