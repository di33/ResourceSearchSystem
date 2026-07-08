from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont


BACKGROUND = (246, 247, 249)
INK = (32, 36, 42)
MUTED = (96, 104, 116)
LINE = (215, 220, 228)
DIFF_THRESHOLD = 18
MIN_CONTENT_PIXELS = 1000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build GIF/sheet/overview previews from Spine runtime frame captures.")
    parser.add_argument("--manifest", action="append", required=True, help="Path to render_actions_cli.mjs manifest.json")
    parser.add_argument("--thumb", type=int, default=260, help="Tile image area size in pixels.")
    parser.add_argument("--gif-size", type=int, default=320, help="Animated GIF canvas size in pixels.")
    parser.add_argument("--duration-ms", type=int, default=130, help="GIF frame duration.")
    return parser.parse_args()


def safe_name(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value or "")
    cleaned = re.sub(r"\s+", "_", cleaned).strip("_")
    return cleaned or fallback


def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"):
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


FONT_LABEL = font(16)
FONT_META = font(12)


def content_mask(image: Image.Image) -> Image.Image:
    rgb = image.convert("RGB")
    bg = Image.new("RGB", rgb.size, BACKGROUND)
    diff = ImageChops.difference(rgb, bg).convert("L")
    return diff.point(lambda value: 255 if value > DIFF_THRESHOLD else 0)


def content_info(image: Image.Image) -> tuple[tuple[int, int, int, int] | None, int]:
    mask = content_mask(image)
    bbox = mask.getbbox()
    if not bbox:
        return None, 0
    score = mask.histogram()[255]
    return bbox, score


def padded_bbox(bbox: tuple[int, int, int, int], image_size: tuple[int, int], pad: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = bbox
    width, height = image_size
    return (
        max(0, left - pad),
        max(0, top - pad),
        min(width, right + pad),
        min(height, bottom + pad),
    )


def compose_thumb(image: Image.Image, size: int, pad: int = 18) -> Image.Image:
    bbox, _ = content_info(image)
    source = image.convert("RGBA")
    if bbox:
        source = source.crop(padded_bbox(bbox, source.size, pad))

    canvas = Image.new("RGB", (size, size), BACKGROUND)
    max_side = max(1, size - 24)
    scale = min(max_side / source.width, max_side / source.height)
    target = (max(1, int(source.width * scale)), max(1, int(source.height * scale)))
    source = source.resize(target, Image.Resampling.LANCZOS)
    x = (size - target[0]) // 2
    y = (size - target[1]) // 2
    canvas.paste(source.convert("RGB"), (x, y), source.getchannel("A"))
    return canvas


def compose_canvas_thumb(image: Image.Image, size: int) -> Image.Image:
    source = image.convert("RGBA")
    canvas = Image.new("RGB", (size, size), BACKGROUND)
    max_side = max(1, size - 24)
    scale = min(max_side / source.width, max_side / source.height)
    target = (max(1, int(source.width * scale)), max(1, int(source.height * scale)))
    source = source.resize(target, Image.Resampling.LANCZOS)
    x = (size - target[0]) // 2
    y = (size - target[1]) // 2
    canvas.paste(source.convert("RGB"), (x, y), source.getchannel("A"))
    return canvas


def draw_centered_text(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, fill: tuple[int, int, int], font_obj: ImageFont.ImageFont) -> None:
    left, top, right, bottom = box
    bbox = draw.textbbox((0, 0), text, font=font_obj)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    draw.text((left + (right - left - text_w) / 2, top + (bottom - top - text_h) / 2), text, fill=fill, font=font_obj)


def write_sheet(out_path: Path, frames: list[Image.Image], thumb_size: int) -> None:
    gap = 12
    cols = min(5, max(1, len(frames)))
    rows = math.ceil(len(frames) / cols)
    width = cols * thumb_size + (cols + 1) * gap
    height = rows * thumb_size + (rows + 1) * gap
    sheet = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    for index, frame in enumerate(frames):
        col = index % cols
        row = index // cols
        x = gap + col * (thumb_size + gap)
        y = gap + row * (thumb_size + gap)
        thumb = compose_thumb(frame, thumb_size)
        sheet.paste(thumb, (x, y))
        draw.rectangle((x, y, x + thumb_size - 1, y + thumb_size - 1), outline=LINE)
    sheet.save(out_path, quality=92)


def write_gif(out_path: Path, frames: list[Image.Image], gif_size: int, duration_ms: int) -> None:
    thumbs = [compose_thumb(frame, gif_size) for frame in frames]
    thumbs[0].save(
        out_path,
        save_all=True,
        append_images=thumbs[1:],
        duration=duration_ms,
        loop=0,
        disposal=2,
    )


def write_overview(out_path: Path, covers: list[tuple[str, Image.Image, int, float]], thumb_size: int) -> None:
    gap = 14
    label_h = 46
    cols = min(4, max(1, len(covers)))
    rows = math.ceil(len(covers) / cols)
    tile_w = thumb_size
    tile_h = thumb_size + label_h
    width = cols * tile_w + (cols + 1) * gap
    height = rows * tile_h + (rows + 1) * gap
    overview = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(overview)
    for index, (name, frame, frame_count, duration) in enumerate(covers):
        col = index % cols
        row = index // cols
        x = gap + col * (tile_w + gap)
        y = gap + row * (tile_h + gap)
        thumb = compose_thumb(frame, thumb_size)
        overview.paste(thumb, (x, y))
        draw.rectangle((x, y, x + tile_w - 1, y + thumb_size - 1), outline=LINE)
        short_name = name if len(name) <= 24 else name[:21] + "..."
        draw_centered_text(draw, (x + 4, y + thumb_size + 5, x + tile_w - 4, y + thumb_size + 25), short_name, INK, FONT_LABEL)
        meta = f"{frame_count} frames  {duration:.2f}s"
        draw_centered_text(draw, (x + 4, y + thumb_size + 25, x + tile_w - 4, y + tile_h - 4), meta, MUTED, FONT_META)
    overview.save(out_path, quality=92)


def animation_frame_at(action: dict[str, object], time_value: float) -> Image.Image:
    frames = action["frames"]
    if not isinstance(frames, list) or not frames:
        raise ValueError("animation has no frames")
    duration = float(action.get("duration") or 0)
    if duration > 0 and time_value >= duration:
        return frames[-1]

    sample_times = action.get("sample_times")
    if isinstance(sample_times, list) and len(sample_times) == len(frames):
        selected = 0
        for index, sample_time in enumerate(sample_times):
            try:
                value = float(sample_time)
            except (TypeError, ValueError):
                continue
            if value <= time_value:
                selected = index
            else:
                break
        return frames[selected]

    if duration <= 0 or len(frames) == 1:
        return frames[0]
    index = int((time_value / duration) * (len(frames) - 1))
    return frames[max(0, min(len(frames) - 1, index))]


def write_actions_gif(out_path: Path, actions: list[dict[str, object]], tile_size: int) -> None:
    frame_count = max(len(action["frames"]) for action in actions if isinstance(action.get("frames"), list))
    if frame_count <= 0:
        raise ValueError("no action frames")

    gap = 14
    label_h = 46
    cols = min(4, max(1, len(actions)))
    rows = math.ceil(len(actions) / cols)
    tile_w = tile_size
    tile_h = tile_size + label_h
    width = cols * tile_w + (cols + 1) * gap
    height = rows * tile_h + (rows + 1) * gap
    max_duration = max(float(action.get("duration") or 0) for action in actions)
    times = [
        0.0 if frame_count == 1 else max_duration * index / (frame_count - 1)
        for index in range(frame_count)
    ]
    total_ms = max(1, int(round(max_duration * 1000)))
    durations = [
        max(20, int(round(total_ms * (index + 1) / frame_count)) - int(round(total_ms * index / frame_count)))
        for index in range(frame_count)
    ]
    frames = []
    for time_value in times:
        canvas = Image.new("RGB", (width, height), BACKGROUND)
        draw = ImageDraw.Draw(canvas)
        for index, action in enumerate(actions):
            col = index % cols
            row = index // cols
            x = gap + col * (tile_w + gap)
            y = gap + row * (tile_h + gap)
            thumb = compose_canvas_thumb(animation_frame_at(action, time_value), tile_size)
            canvas.paste(thumb, (x, y))
            draw.rectangle((x, y, x + tile_w - 1, y + tile_size - 1), outline=LINE)
            name = str(action.get("name") or f"animation_{index + 1}")
            short_name = name if len(name) <= 24 else name[:21] + "..."
            draw_centered_text(draw, (x + 4, y + tile_size + 5, x + tile_w - 4, y + tile_size + 25), short_name, INK, FONT_LABEL)
            duration = float(action.get("duration") or 0)
            state = "hold" if duration > 0 and time_value >= duration else f"{duration:.2f}s"
            draw_centered_text(draw, (x + 4, y + tile_size + 25, x + tile_w - 4, y + tile_h - 4), state, MUTED, FONT_META)
        frames.append(canvas)

    frames[0].save(
        out_path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        disposal=2,
        optimize=True,
    )


def load_visible_frames(paths: list[Path]) -> tuple[list[Image.Image], list[int]]:
    loaded = [Image.open(path).convert("RGBA") for path in paths]
    scored = [(image, content_info(image)[1]) for image in loaded]
    visible = [image for image, score in scored if score >= MIN_CONTENT_PIXELS]
    scores = [score for _, score in scored]
    return visible or loaded, scores


def process_manifest(manifest_path: Path, thumb_size: int, gif_size: int, duration_ms: int) -> dict[str, object]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    out_dir = manifest_path.parent
    covers: list[tuple[str, Image.Image, int, float]] = []
    action_frames: list[dict[str, object]] = []
    animation_outputs = []

    for index, animation in enumerate(manifest.get("animations", [])):
        name = str(animation.get("name") or f"animation_{index + 1}")
        frame_paths = [Path(path) for path in animation.get("frame_paths", [])]
        if not frame_paths:
            continue
        raw_frames = [Image.open(path).convert("RGBA") for path in frame_paths]
        sample_times = animation.get("sample_times") or []
        duration = float(animation.get("duration") or 0)
        frames, scores = load_visible_frames(frame_paths)
        visible_indices = [score_index for score_index, score in enumerate(scores) if score >= MIN_CONTENT_PIXELS]
        if not visible_indices:
            visible_indices = list(range(len(raw_frames)))
        visible_raw_frames = [raw_frames[score_index] for score_index in visible_indices]
        if isinstance(sample_times, list) and len(sample_times) == len(raw_frames):
            visible_sample_times = [sample_times[score_index] for score_index in visible_indices]
        else:
            visible_sample_times = []
        best_index = max(range(len(frames)), key=lambda item: content_info(frames[item])[1])
        best_frame = frames[best_index]
        stem = safe_name(name, f"animation_{index + 1}")
        sheet_path = out_dir / f"{stem}_sheet.webp"
        gif_path = out_dir / f"{stem}_preview.gif"
        write_sheet(sheet_path, frames, thumb_size)
        write_gif(gif_path, frames, gif_size, duration_ms)
        covers.append((name, best_frame, len(frames), duration))
        action_frames.append({
            "name": name,
            "duration": duration,
            "sample_times": visible_sample_times,
            "frames": visible_raw_frames,
        })
        animation_outputs.append({
            "name": name,
            "sheet": str(sheet_path),
            "gif": str(gif_path),
            "visible_frame_count": len(frames),
            "raw_scores": scores,
        })

    overview_path = out_dir / "all_actions_overview.webp"
    if covers:
        write_overview(overview_path, covers, thumb_size)

    actions_gif_path = out_dir / "all_actions_preview.gif"
    if action_frames:
        write_actions_gif(actions_gif_path, action_frames, gif_size)

    result = {
        "manifest": str(manifest_path),
        "overview": str(overview_path) if covers else "",
        "actions_gif": str(actions_gif_path) if action_frames else "",
        "animation_count": len(animation_outputs),
        "animations": animation_outputs,
    }
    (out_dir / "postprocess_manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    args = parse_args()
    results = [
        process_manifest(Path(path), args.thumb, args.gif_size, args.duration_ms)
        for path in args.manifest
    ]
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
