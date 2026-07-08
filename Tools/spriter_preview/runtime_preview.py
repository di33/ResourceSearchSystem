from __future__ import annotations

import asyncio
import math
import os
import shutil
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

from ResourceProcessor.preview.thumbnail_generator import validate_preview
from ResourceProcessor.preview_metadata import PreviewInfo, PreviewStrategy, ResourceProcessingEntity


RASTER_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
SPRITER_RUNTIME_RENDERER = "spriter-scml-pillow"
BACKGROUND = (246, 247, 249)
CELL_BACKGROUND = (255, 255, 255)
LABEL_COLOR = (31, 41, 55)


@dataclass(frozen=True)
class Spatial:
    x: float = 0.0
    y: float = 0.0
    angle: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0
    alpha: float = 1.0


@dataclass(frozen=True)
class FileRef:
    folder: int
    file: int
    name: str
    width: int
    height: int
    pivot_x: float
    pivot_y: float
    path: Path | None


@dataclass(frozen=True)
class ObjectState:
    spatial: Spatial
    folder: int | None = None
    file: int | None = None
    pivot_x: float | None = None
    pivot_y: float | None = None


@dataclass(frozen=True)
class TimelineKey:
    id: int
    time: float
    spin: int
    curve_type: str
    c1: float
    c2: float
    c3: float
    c4: float
    state: ObjectState


@dataclass(frozen=True)
class Timeline:
    id: int
    name: str
    type: str
    keys: list[TimelineKey]


@dataclass(frozen=True)
class MainlineRef:
    id: int
    parent: int | None
    timeline: int
    key: int
    z_index: int
    folder: int | None = None
    file: int | None = None


@dataclass(frozen=True)
class MainlineKey:
    id: int
    time: float
    bone_refs: list[MainlineRef]
    object_refs: list[MainlineRef]


@dataclass(frozen=True)
class Animation:
    id: int
    name: str
    length: float
    looping: bool
    mainline_keys: list[MainlineKey]
    timelines: dict[int, Timeline]


@dataclass(frozen=True)
class SpriterData:
    scml_path: Path
    files: dict[tuple[int, int], FileRef]
    animations: list[Animation]


@dataclass(frozen=True)
class RenderSprite:
    file_ref: FileRef
    spatial: Spatial
    pivot_x: float
    pivot_y: float
    z_index: int


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(elem: ET.Element, name: str | None = None) -> list[ET.Element]:
    items = list(elem)
    if name is None:
        return items
    return [child for child in items if _xml_name(child.tag) == name]


def _child(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem:
        if _xml_name(child.tag) == name:
            return child
    return None


def _attr_float(elem: ET.Element, name: str, default: float = 0.0) -> float:
    raw = elem.attrib.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _attr_int(elem: ET.Element, name: str, default: int = 0) -> int:
    raw = elem.attrib.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _attr_optional_int(elem: ET.Element, name: str) -> int | None:
    if name not in elem.attrib:
        return None
    value = _attr_int(elem, name, -1)
    return value if value >= 0 else None


def _normalize_path_key(value: str | Path) -> str:
    text = str(value).replace("\\", "/").strip().lower()
    while text.startswith("./"):
        text = text[2:]
    return text


def _image_lookup(image_paths: Iterable[Path]) -> dict[str, Path]:
    lookup: dict[str, Path] = {}
    basename_seen: dict[str, Path | None] = {}
    for image_path in image_paths:
        if not image_path.is_file():
            continue
        normalized = _normalize_path_key(image_path.name)
        basename_seen[normalized] = image_path if normalized not in basename_seen else None
        parts = image_path.parts
        for depth in range(2, min(5, len(parts)) + 1):
            key = _normalize_path_key(Path(*parts[-depth:]))
            lookup.setdefault(key, image_path)
    for key, path in basename_seen.items():
        if path is not None:
            lookup.setdefault(key, path)
    return lookup


def _resolve_image_path(scml_path: Path, raw_name: str, lookup: dict[str, Path]) -> Path | None:
    if not raw_name:
        return None
    candidates = [
        scml_path.parent / raw_name,
        scml_path.parent.parent / raw_name,
        Path(raw_name),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    key = _normalize_path_key(raw_name)
    if key in lookup:
        return lookup[key]
    basename = _normalize_path_key(Path(raw_name).name)
    return lookup.get(basename)


def _parse_object_state(elem: ET.Element, *, is_object: bool) -> ObjectState:
    return ObjectState(
        spatial=Spatial(
            x=_attr_float(elem, "x", 0.0),
            y=_attr_float(elem, "y", 0.0),
            angle=_attr_float(elem, "angle", 0.0),
            scale_x=_attr_float(elem, "scale_x", 1.0),
            scale_y=_attr_float(elem, "scale_y", 1.0),
            alpha=max(0.0, min(1.0, _attr_float(elem, "a", 1.0))),
        ),
        folder=_attr_optional_int(elem, "folder") if is_object else None,
        file=_attr_optional_int(elem, "file") if is_object else None,
        pivot_x=_attr_float(elem, "pivot_x", math.nan) if is_object else None,
        pivot_y=_attr_float(elem, "pivot_y", math.nan) if is_object else None,
    )


def _parse_timeline_key(key_elem: ET.Element, timeline_type: str) -> TimelineKey | None:
    object_elem = _child(key_elem, "object")
    bone_elem = _child(key_elem, "bone")
    state_elem = bone_elem if timeline_type == "bone" else (object_elem if object_elem is not None else bone_elem)
    if state_elem is None:
        return None
    curve_type = key_elem.attrib.get("curve_type", "").strip().lower()
    return TimelineKey(
        id=_attr_int(key_elem, "id", 0),
        time=_attr_float(key_elem, "time", 0.0),
        spin=_attr_int(key_elem, "spin", 1),
        curve_type=curve_type,
        c1=_attr_float(key_elem, "c1", 0.0),
        c2=_attr_float(key_elem, "c2", 0.0),
        c3=_attr_float(key_elem, "c3", 1.0),
        c4=_attr_float(key_elem, "c4", 1.0),
        state=_parse_object_state(state_elem, is_object=_xml_name(state_elem.tag) == "object"),
    )


def _parse_timeline(timeline_elem: ET.Element) -> Timeline | None:
    timeline_id = _attr_int(timeline_elem, "id", 0)
    timeline_type = timeline_elem.attrib.get("type", "").strip().lower()
    keys: list[TimelineKey] = []
    inferred_type = timeline_type
    for key_elem in _children(timeline_elem, "key"):
        if not inferred_type:
            if _child(key_elem, "bone") is not None:
                inferred_type = "bone"
            else:
                inferred_type = "sprite"
        key = _parse_timeline_key(key_elem, inferred_type)
        if key is not None:
            keys.append(key)
    if not keys:
        return None
    return Timeline(
        id=timeline_id,
        name=timeline_elem.attrib.get("name", f"timeline_{timeline_id}"),
        type=inferred_type or "sprite",
        keys=sorted(keys, key=lambda key: (key.time, key.id)),
    )


def _parse_mainline_ref(elem: ET.Element) -> MainlineRef:
    return MainlineRef(
        id=_attr_int(elem, "id", 0),
        parent=_attr_optional_int(elem, "parent"),
        timeline=_attr_int(elem, "timeline", 0),
        key=_attr_int(elem, "key", 0),
        z_index=_attr_int(elem, "z_index", _attr_int(elem, "id", 0)),
        folder=_attr_optional_int(elem, "folder"),
        file=_attr_optional_int(elem, "file"),
    )


def _parse_animation(animation_elem: ET.Element) -> Animation | None:
    mainline = _child(animation_elem, "mainline")
    if mainline is None:
        return None

    mainline_keys: list[MainlineKey] = []
    for key_elem in _children(mainline, "key"):
        mainline_keys.append(
            MainlineKey(
                id=_attr_int(key_elem, "id", 0),
                time=_attr_float(key_elem, "time", 0.0),
                bone_refs=[_parse_mainline_ref(elem) for elem in _children(key_elem, "bone_ref")],
                object_refs=[_parse_mainline_ref(elem) for elem in _children(key_elem, "object_ref")],
            )
        )
    timelines = [
        timeline
        for elem in _children(animation_elem, "timeline")
        if (timeline := _parse_timeline(elem)) is not None
    ]
    if not mainline_keys or not timelines:
        return None

    length = max(1.0, _attr_float(animation_elem, "length", 1.0))
    looping_text = animation_elem.attrib.get("looping", "true").strip().lower()
    return Animation(
        id=_attr_int(animation_elem, "id", 0),
        name=animation_elem.attrib.get("name", f"animation_{_attr_int(animation_elem, 'id', 0)}"),
        length=length,
        looping=looping_text not in {"false", "0", "no"},
        mainline_keys=sorted(mainline_keys, key=lambda key: (key.time, key.id)),
        timelines={timeline.id: timeline for timeline in timelines},
    )


def _parse_scml(scml_path: Path, image_paths: Iterable[Path]) -> SpriterData:
    root = ET.parse(scml_path).getroot()
    lookup = _image_lookup(image_paths)
    files: dict[tuple[int, int], FileRef] = {}

    for folder_elem in _children(root, "folder"):
        folder_id = _attr_int(folder_elem, "id", 0)
        for file_elem in _children(folder_elem, "file"):
            file_id = _attr_int(file_elem, "id", 0)
            name = file_elem.attrib.get("name", "")
            path = _resolve_image_path(scml_path, name, lookup)
            width = _attr_int(file_elem, "width", 0)
            height = _attr_int(file_elem, "height", 0)
            if path and (width <= 0 or height <= 0):
                try:
                    with Image.open(path) as image:
                        width, height = image.size
                except Exception:
                    width = max(width, 1)
                    height = max(height, 1)
            files[(folder_id, file_id)] = FileRef(
                folder=folder_id,
                file=file_id,
                name=name,
                width=max(1, width),
                height=max(1, height),
                pivot_x=_attr_float(file_elem, "pivot_x", 0.0),
                pivot_y=_attr_float(file_elem, "pivot_y", 1.0),
                path=path,
            )

    animations: list[Animation] = []
    for entity_elem in _children(root, "entity"):
        for animation_elem in _children(entity_elem, "animation"):
            animation = _parse_animation(animation_elem)
            if animation is not None:
                animations.append(animation)
    if not animations:
        raise RuntimeError("spriter scml has no renderable animations")
    return SpriterData(scml_path=scml_path, files=files, animations=animations)


def _current_mainline_key(animation: Animation, time_ms: float) -> MainlineKey:
    sample_time = time_ms % animation.length if animation.looping else min(max(time_ms, 0.0), animation.length)
    current = animation.mainline_keys[0]
    for key in animation.mainline_keys:
        if key.time <= sample_time:
            current = key
        else:
            break
    return current


def _curve_t(key: TimelineKey, t: float) -> float:
    t = max(0.0, min(1.0, t))
    curve_type = key.curve_type
    if curve_type in {"instant", "1"}:
        return 0.0
    if curve_type in {"quadratic", "2"}:
        return (1.0 - t) * (1.0 - t) * 0.0 + 2.0 * (1.0 - t) * t * key.c1 + t * t
    if curve_type in {"cubic", "3"}:
        return (
            3.0 * (1.0 - t) * (1.0 - t) * t * key.c1
            + 3.0 * (1.0 - t) * t * t * key.c2
            + t * t * t
        )
    if curve_type in {"bezier", "quartic"}:
        return _bezier_y_for_x(t, key.c1, key.c2, key.c3, key.c4)
    return t


def _bezier_y_for_x(x_target: float, x1: float, y1: float, x2: float, y2: float) -> float:
    low = 0.0
    high = 1.0
    t = x_target
    for _ in range(10):
        t = (low + high) / 2.0
        x = _cubic_bezier(t, 0.0, x1, x2, 1.0)
        if x < x_target:
            low = t
        else:
            high = t
    return _cubic_bezier(t, 0.0, y1, y2, 1.0)


def _cubic_bezier(t: float, p0: float, p1: float, p2: float, p3: float) -> float:
    inv = 1.0 - t
    return inv * inv * inv * p0 + 3.0 * inv * inv * t * p1 + 3.0 * inv * t * t * p2 + t * t * t * p3


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _angle_lerp(a: float, b: float, t: float, spin: int) -> float:
    if spin == 0:
        return a
    delta = b - a
    if spin > 0 and delta < 0:
        b += 360.0
    elif spin < 0 and delta > 0:
        b -= 360.0
    return _lerp(a, b, t)


def _interpolate_state(a: ObjectState, b: ObjectState, t: float, spin: int) -> ObjectState:
    pivot_x: float | None = None
    pivot_y: float | None = None
    if a.pivot_x is not None and not math.isnan(a.pivot_x):
        pivot_x = a.pivot_x
    if b.pivot_x is not None and not math.isnan(b.pivot_x) and pivot_x is not None:
        pivot_x = _lerp(pivot_x, b.pivot_x, t)
    if a.pivot_y is not None and not math.isnan(a.pivot_y):
        pivot_y = a.pivot_y
    if b.pivot_y is not None and not math.isnan(b.pivot_y) and pivot_y is not None:
        pivot_y = _lerp(pivot_y, b.pivot_y, t)

    return ObjectState(
        spatial=Spatial(
            x=_lerp(a.spatial.x, b.spatial.x, t),
            y=_lerp(a.spatial.y, b.spatial.y, t),
            angle=_angle_lerp(a.spatial.angle, b.spatial.angle, t, spin),
            scale_x=_lerp(a.spatial.scale_x, b.spatial.scale_x, t),
            scale_y=_lerp(a.spatial.scale_y, b.spatial.scale_y, t),
            alpha=_lerp(a.spatial.alpha, b.spatial.alpha, t),
        ),
        folder=a.folder if a.folder is not None else b.folder,
        file=a.file if a.file is not None else b.file,
        pivot_x=pivot_x,
        pivot_y=pivot_y,
    )


def _timeline_key_index(timeline: Timeline, key_id: int) -> int:
    for index, key in enumerate(timeline.keys):
        if key.id == key_id:
            return index
    return 0


def _sample_timeline(animation: Animation, ref: MainlineRef, time_ms: float) -> ObjectState:
    timeline = animation.timelines.get(ref.timeline)
    if timeline is None or not timeline.keys:
        return ObjectState(Spatial())

    key_index = _timeline_key_index(timeline, ref.key)
    key_a = timeline.keys[key_index]
    if key_index + 1 < len(timeline.keys):
        key_b = timeline.keys[key_index + 1]
        end_time = key_b.time
    elif animation.looping:
        key_b = timeline.keys[0]
        end_time = animation.length + key_b.time
    else:
        return key_a.state

    start_time = key_a.time
    sample_time = time_ms % animation.length if animation.looping else min(max(time_ms, 0.0), animation.length)
    if sample_time < start_time and animation.looping:
        sample_time += animation.length
    if end_time <= start_time:
        return key_a.state
    t = _curve_t(key_a, (sample_time - start_time) / (end_time - start_time))
    return _interpolate_state(key_a.state, key_b.state, t, key_a.spin)


def _unmap_from_parent(local: Spatial, parent: Spatial) -> Spatial:
    angle = math.radians(parent.angle)
    scaled_x = local.x * parent.scale_x
    scaled_y = local.y * parent.scale_y
    child_angle = -local.angle if parent.scale_x * parent.scale_y < 0 else local.angle
    return Spatial(
        x=parent.x + math.cos(angle) * scaled_x - math.sin(angle) * scaled_y,
        y=parent.y + math.sin(angle) * scaled_x + math.cos(angle) * scaled_y,
        angle=parent.angle + child_angle,
        scale_x=parent.scale_x * local.scale_x,
        scale_y=parent.scale_y * local.scale_y,
        alpha=parent.alpha * local.alpha,
    )


def _frame_sprites(data: SpriterData, animation: Animation, time_ms: float) -> list[RenderSprite]:
    mainline = _current_mainline_key(animation, time_ms)
    identity = Spatial()
    bones: dict[int, Spatial] = {}

    for ref in sorted(mainline.bone_refs, key=lambda item: item.id):
        state = _sample_timeline(animation, ref, time_ms)
        parent = bones.get(ref.parent, identity) if ref.parent is not None else identity
        bones[ref.id] = _unmap_from_parent(state.spatial, parent)

    sprites: list[RenderSprite] = []
    for ref in sorted(mainline.object_refs, key=lambda item: (item.z_index, item.id)):
        state = _sample_timeline(animation, ref, time_ms)
        folder = state.folder if state.folder is not None else ref.folder
        file_id = state.file if state.file is not None else ref.file
        if folder is None or file_id is None:
            continue
        file_ref = data.files.get((folder, file_id))
        if file_ref is None or file_ref.path is None:
            continue
        parent = bones.get(ref.parent, identity) if ref.parent is not None else identity
        world = _unmap_from_parent(state.spatial, parent)
        pivot_x = state.pivot_x if state.pivot_x is not None and not math.isnan(state.pivot_x) else file_ref.pivot_x
        pivot_y = state.pivot_y if state.pivot_y is not None and not math.isnan(state.pivot_y) else file_ref.pivot_y
        sprites.append(
            RenderSprite(
                file_ref=file_ref,
                spatial=world,
                pivot_x=pivot_x,
                pivot_y=pivot_y,
                z_index=ref.z_index,
            )
        )
    return sprites


def _sprite_corners(sprite: RenderSprite) -> list[tuple[float, float]]:
    width = sprite.file_ref.width
    height = sprite.file_ref.height
    angle = math.radians(sprite.spatial.angle)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    points: list[tuple[float, float]] = []
    for u, v in ((0.0, 0.0), (width, 0.0), (width, height), (0.0, height)):
        local_x = (u - sprite.pivot_x * width) * sprite.spatial.scale_x
        local_y = ((1.0 - sprite.pivot_y) * height - v) * sprite.spatial.scale_y
        points.append(
            (
                sprite.spatial.x + cos_a * local_x - sin_a * local_y,
                sprite.spatial.y + sin_a * local_x + cos_a * local_y,
            )
        )
    return points


def _animation_sample_times(animation: Animation, frames: int) -> list[float]:
    frames = max(1, frames)
    if frames == 1:
        return [0.0]
    if animation.looping:
        return [animation.length * index / frames for index in range(frames)]
    return [animation.length * index / (frames - 1) for index in range(frames)]


def _bounds_for_animation(data: SpriterData, animation: Animation, frames: int) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for time_ms in _animation_sample_times(animation, max(frames, 6)):
        for sprite in _frame_sprites(data, animation, time_ms):
            for x, y in _sprite_corners(sprite):
                xs.append(x)
                ys.append(y)
    if not xs or not ys:
        raise RuntimeError(f"animation has no visible sprites: {animation.name}")
    min_x = min(xs)
    max_x = max(xs)
    min_y = min(ys)
    max_y = max(ys)
    if max_x - min_x < 1.0:
        max_x += 0.5
        min_x -= 0.5
    if max_y - min_y < 1.0:
        max_y += 0.5
        min_y -= 0.5
    return min_x, min_y, max_x, max_y


def _scale_for_bounds(bounds_list: Iterable[tuple[float, float, float, float]], *, size: int) -> float:
    max_w = 1.0
    max_h = 1.0
    for min_x, min_y, max_x, max_y in bounds_list:
        max_w = max(max_w, max_x - min_x)
        max_h = max(max_h, max_y - min_y)
    padding = max(8, int(size * 0.05))
    available = max(1, size - padding * 2)
    return min(available / max_w, available / max_h)


def _render_animation_frame(
    data: SpriterData,
    animation: Animation,
    time_ms: float,
    bounds: tuple[float, float, float, float],
    *,
    size: int,
    scale: float | None = None,
) -> Image.Image:
    min_x, min_y, max_x, max_y = bounds
    bbox_w = max(1.0, max_x - min_x)
    bbox_h = max(1.0, max_y - min_y)
    if scale is None:
        scale = _scale_for_bounds([bounds], size=size)
    content_w = bbox_w * scale
    content_h = bbox_h * scale
    offset_x = (size - content_w) / 2.0
    offset_y = (size - content_h) / 2.0

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    for sprite in _frame_sprites(data, animation, time_ms):
        source_path = sprite.file_ref.path
        if source_path is None:
            continue
        try:
            with Image.open(source_path) as image:
                source = image.convert("RGBA")
        except Exception:
            continue
        if sprite.spatial.alpha < 0.999:
            alpha = source.getchannel("A").point(lambda value: int(value * sprite.spatial.alpha))
            source.putalpha(alpha)

        transformed = _transform_sprite_to_canvas(
            source,
            sprite,
            bounds=(min_x, min_y, max_x, max_y),
            canvas_size=size,
            scale=scale,
            offset=(offset_x, offset_y),
        )
        if transformed is not None:
            canvas.alpha_composite(transformed)
    return canvas


def _transform_sprite_to_canvas(
    source: Image.Image,
    sprite: RenderSprite,
    *,
    bounds: tuple[float, float, float, float],
    canvas_size: int,
    scale: float,
    offset: tuple[float, float],
) -> Image.Image | None:
    min_x, _min_y, _max_x, max_y = bounds
    offset_x, offset_y = offset
    angle = math.radians(sprite.spatial.angle)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    sx = sprite.spatial.scale_x
    sy = sprite.spatial.scale_y
    px = sprite.pivot_x * sprite.file_ref.width
    py = (1.0 - sprite.pivot_y) * sprite.file_ref.height

    a = scale * cos_a * sx
    b = scale * sin_a * sy
    c = offset_x + scale * (sprite.spatial.x - min_x - cos_a * sx * px - sin_a * sy * py)
    d = -scale * sin_a * sx
    e = scale * cos_a * sy
    f = offset_y + scale * (max_y - sprite.spatial.y + sin_a * sx * px - cos_a * sy * py)
    det = a * e - b * d
    if abs(det) < 1e-8:
        return None
    inverse = (
        e / det,
        -b / det,
        (b * f - e * c) / det,
        -d / det,
        a / det,
        (d * c - a * f) / det,
    )
    return source.transform(
        (canvas_size, canvas_size),
        Image.Transform.AFFINE,
        inverse,
        resample=Image.Resampling.BICUBIC,
    )


def _fit_text(text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if _text_width(text, font) <= max_width:
        return text
    ellipsis = "..."
    trimmed = text
    while trimmed and _text_width(trimmed + ellipsis, font) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + ellipsis) if trimmed else ellipsis


def _text_width(text: str, font: ImageFont.ImageFont) -> int:
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0]


def _load_font(size: int) -> ImageFont.ImageFont:
    for name in ("arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _composite_action_grid(
    rendered: list[tuple[Animation, list[Image.Image]]],
    *,
    frame_index: int,
    tile_size: int,
    label_height: int,
    columns: int,
) -> Image.Image:
    rows = math.ceil(len(rendered) / columns)
    gap = 8
    width = columns * tile_size + (columns + 1) * gap
    height = rows * (tile_size + label_height) + (rows + 1) * gap
    canvas = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    font = _load_font(14)

    for index, (animation, frames) in enumerate(rendered):
        row = index // columns
        col = index % columns
        x = gap + col * (tile_size + gap)
        y = gap + row * (tile_size + label_height + gap)
        draw.rectangle((x, y, x + tile_size - 1, y + tile_size + label_height - 1), fill=CELL_BACKGROUND)
        frame = frames[frame_index % len(frames)]
        canvas.paste(CELL_BACKGROUND, (x, y, x + tile_size, y + tile_size))
        canvas.paste(frame.convert("RGB"), (x, y), frame.getchannel("A"))
        label = _fit_text(animation.name or f"Animation {animation.id}", font, tile_size - 12)
        draw.text((x + 6, y + tile_size + 8), label, fill=LABEL_COLOR, font=font)
    return canvas


def _visible_alpha_bbox(image: Image.Image) -> tuple[int, int, int, int] | None:
    return image.getchannel("A").getbbox()


def _union_frame_bbox(frames: list[Image.Image], padding: int) -> tuple[int, int, int, int]:
    boxes = [box for frame in frames if (box := _visible_alpha_bbox(frame)) is not None]
    if not boxes:
        return (0, 0, frames[0].width, frames[0].height)
    left = max(0, min(box[0] for box in boxes) - padding)
    top = max(0, min(box[1] for box in boxes) - padding)
    right = min(frames[0].width, max(box[2] for box in boxes) + padding)
    bottom = min(frames[0].height, max(box[3] for box in boxes) + padding)
    return left, top, max(left + 1, right), max(top + 1, bottom)


def _composite_single_action_frame(
    animation: Animation,
    frame: Image.Image,
    *,
    label_height: int,
) -> Image.Image:
    gap = 8
    font = _load_font(14)
    label = animation.name or f"Animation {animation.id}"
    content_width = max(frame.width, _text_width(label, font) + 12)
    width = content_width + gap * 2
    height = frame.height + label_height + gap * 2
    canvas = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((gap, gap, width - gap - 1, height - gap - 1), fill=CELL_BACKGROUND)
    x = gap + (content_width - frame.width) // 2
    canvas.paste(frame.convert("RGB"), (x, gap), frame.getchannel("A"))
    fitted_label = _fit_text(label, font, content_width - 12)
    draw.text((gap + 6, gap + frame.height + 8), fitted_label, fill=LABEL_COLOR, font=font)
    return canvas


def _write_actions_gif(data: SpriterData, output_path: Path, *, max_size: int) -> None:
    frames_per_action = _int_env("SPRITER_PREVIEW_FRAMES", 5)
    max_actions = _int_env("SPRITER_PREVIEW_MAX_ANIMATIONS", 8)
    tile_size = min(max(144, max_size // 3), 160)
    label_height = 34

    animations = data.animations[:max_actions]
    rendered: list[tuple[Animation, list[Image.Image]]] = []
    animation_bounds = [
        (animation, _bounds_for_animation(data, animation, frames_per_action))
        for animation in animations
    ]
    shared_scale = _scale_for_bounds((bounds for _animation, bounds in animation_bounds), size=tile_size)
    for animation, bounds in animation_bounds:
        frames = [
            _render_animation_frame(data, animation, time_ms, bounds, size=tile_size, scale=shared_scale)
            for time_ms in _animation_sample_times(animation, frames_per_action)
        ]
        if frames:
            rendered.append((animation, frames))
    if not rendered:
        raise RuntimeError("spriter scml produced no rendered frames")

    if len(rendered) == 1:
        animation, frames = rendered[0]
        crop_box = _union_frame_bbox(frames, padding=8)
        cropped_frames = [frame.crop(crop_box) for frame in frames]
        grid_frames = [
            _composite_single_action_frame(animation, frame, label_height=label_height)
            for frame in cropped_frames
        ]
    else:
        columns = max(1, min(len(rendered), 3, max_size // max(1, tile_size)))
        grid_frames = [
            _composite_action_grid(
                rendered,
                frame_index=index,
                tile_size=tile_size,
                label_height=label_height,
                columns=columns,
            )
            for index in range(frames_per_action)
        ]
    if len(grid_frames) == 1:
        grid_frames.append(grid_frames[0].copy())
    first, *rest = grid_frames
    output_path.parent.mkdir(parents=True, exist_ok=True)
    first.save(output_path, save_all=True, append_images=rest, duration=130, loop=0, optimize=True)


def _spriter_source_paths(entity: ResourceProcessingEntity) -> tuple[Path, list[Path]]:
    paths = [Path(file.file_path) for file in entity.files if file.file_path]
    existing = [path for path in paths if path.is_file()]
    scml_paths = [path for path in existing if path.suffix.lower() == ".scml"]
    image_paths = [path for path in existing if path.suffix.lower() in RASTER_EXTS]
    if not scml_paths:
        raise RuntimeError("missing spriter scml file")
    if not image_paths:
        raise RuntimeError("missing spriter image files")
    return scml_paths[0], image_paths


def _generate_spriter_runtime_previews_sync(
    entity: ResourceProcessingEntity,
    output_dir: Path,
    *,
    max_size: int,
) -> list[PreviewInfo]:
    scml_path, image_paths = _spriter_source_paths(entity)
    stage_dir = output_dir / f"_spriter_runtime_{uuid.uuid4().hex[:12]}"
    stage_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        data = _parse_scml(scml_path, image_paths)
        stem = entity.content_md5 or uuid.uuid4().hex
        stage_preview = stage_dir / f"{stem}_spriter_runtime_actions.gif"
        _write_actions_gif(data, stage_preview, max_size=max_size)

        passed, reason = validate_preview(str(stage_preview))
        if not passed:
            raise RuntimeError(reason)

        target = output_dir / stage_preview.name
        shutil.copy2(stage_preview, target)
        with Image.open(target) as image:
            width, height = image.size
        return [
            PreviewInfo(
                strategy=PreviewStrategy.GIF,
                role="primary",
                path=str(target.resolve()),
                mode="spriter_runtime_actions_gif",
                confidence="high",
                format="gif",
                width=width,
                height=height,
                size=target.stat().st_size,
                renderer=SPRITER_RUNTIME_RENDERER,
            )
        ]
    finally:
        if not _truthy(os.environ.get("SPRITER_PREVIEW_KEEP_WORK_DIR")):
            shutil.rmtree(stage_dir, ignore_errors=True)


async def generate_spriter_runtime_previews(
    entity: ResourceProcessingEntity,
    output_dir: str | Path,
    *,
    max_size: int = 512,
) -> list[PreviewInfo]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _generate_spriter_runtime_previews_sync(
            entity,
            Path(output_dir),
            max_size=max_size,
        ),
    )
