"""
增量管线：同一 work_dir 重复执行时，按「源文件路径 + 指纹」跳过已处理资源，避免重复拷贝与重复生成预览。
状态保存在 work_dir/.pipeline_state.json。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List

from PIL import Image

from ResourceProcessor.preview_metadata import PreviewInfo, PreviewStrategy
from ResourceProcessor.resource_filter import copy_single_categorized_resource
from ResourceProcessor.thumbnail_generator import ThumbnailGenerator, validate_preview

STATE_FILENAME = ".pipeline_state.json"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
FBX_EXT = ".fbx"


def norm_source(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(path)))


def fingerprint(path: str) -> list:
    st = os.stat(path)
    ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
    return [int(st.st_size), int(ns)]


def load_state(work_dir: str) -> dict:
    p = Path(work_dir) / STATE_FILENAME
    if not p.is_file():
        return {"version": 1, "by_source": {}}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data.get("by_source"), dict):
        data["by_source"] = {}
    return data


def save_state(work_dir: str, state: dict) -> None:
    p = Path(work_dir) / STATE_FILENAME
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def resolve_copies(source_paths: List[str], work_dir: str, state: dict) -> Dict[str, str]:
    """
    根据状态决定跳过、覆盖或新建拷贝。返回本次管线中「源路径 -> 工作目录内拷贝路径」。
    """
    by_source = state.setdefault("by_source", {})
    mapping: Dict[str, str] = {}

    for src in source_paths:
        key = norm_source(src)
        fp = fingerprint(src)
        entry = dict(by_source.get(key, {}))
        copied = entry.get("copied_path")

        if copied and entry.get("fingerprint") == fp and os.path.isfile(copied):
            mapping[src] = copied
            by_source[key] = entry
            continue

        if copied and os.path.isfile(copied) and entry.get("fingerprint") != fp:
            shutil.copy2(src, copied)
            entry["fingerprint"] = fp
            entry["preview_path"] = None
            by_source[key] = entry
            mapping[src] = copied
            continue

        dest = copy_single_categorized_resource(src, work_dir)
        if dest is None:
            continue
        sp = str(dest)
        by_source[key] = {
            "fingerprint": fp,
            "copied_path": sp,
            "preview_path": None,
        }
        mapping[src] = sp

    return mapping


def build_index_extra(source_paths: List[str], state: dict) -> Dict[str, Dict[str, Any]]:
    by_source = state.get("by_source", {})
    extra: Dict[str, Dict[str, Any]] = {}
    for p in source_paths:
        key = norm_source(p)
        e = by_source.get(key, {})
        extra[p] = {
            "copied_path": e.get("copied_path"),
            "preview_path": e.get("preview_path"),
        }
    return extra


def _content_md5(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


async def ensure_previews(
    mapping: Dict[str, str],
    work_dir: str,
    state: dict,
    max_size: int = 512,
) -> None:
    """对图片生成 webp 缩略图，对 .fbx 生成旋转预览 GIF；占位图不会阻止后续重试。"""
    previews_dir = Path(work_dir) / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    gen = ThumbnailGenerator(str(previews_dir))
    by_source = state.setdefault("by_source", {})

    async def one(src: str, copied: str) -> None:
        key = norm_source(src)
        cp = Path(copied)
        ext = cp.suffix.lower()
        if ext not in IMAGE_EXTS and ext != FBX_EXT:
            return

        fp = fingerprint(src)
        entry = dict(by_source.get(key, {}))
        prev = entry.get("preview_path")
        preview_renderer = entry.get("preview_renderer")
        should_skip = (
            prev
            and os.path.isfile(prev)
            and entry.get("fingerprint") == fp
            and (ext in IMAGE_EXTS or preview_renderer == "blender")
        )
        if should_skip:
            return

        content_hash = _content_md5(copied)

        if ext in IMAGE_EXTS:
            out_path = await gen.generate_preview(copied, content_hash, max_size=max_size)
            preview_renderer = "pillow"
        else:
            safe_name = f"{content_hash}_preview.gif"
            preview = await gen.generate_fbx_preview_gif_result(copied, safe_name)
            out_path = str(preview["path"])
            preview_renderer = str(preview["renderer"])

        passed, reason = validate_preview(out_path)
        entry = by_source.setdefault(key, {})
        if not passed:
            entry["preview_path"] = None
            entry["preview_failed"] = True
            entry["preview_fail_reason"] = reason
            preview_info = PreviewInfo(
                strategy=PreviewStrategy.STATIC if ext in IMAGE_EXTS else PreviewStrategy.GIF,
                fail_reason=reason,
            )
        else:
            entry["preview_path"] = os.path.abspath(out_path)
            entry.pop("preview_failed", None)
            entry.pop("preview_fail_reason", None)
            with Image.open(out_path) as im:
                w, h = im.size
            preview_info = PreviewInfo(
                strategy=PreviewStrategy.STATIC if ext in IMAGE_EXTS else PreviewStrategy.GIF,
                path=os.path.abspath(out_path),
                format=Path(out_path).suffix.lstrip('.'),
                width=w,
                height=h,
                size=os.path.getsize(out_path),
                renderer=preview_renderer,
                used_placeholder=(preview_renderer == "placeholder"),
            )
        entry["preview_info"] = preview_info.to_dict()
        entry["preview_renderer"] = preview_renderer
        entry["fingerprint"] = fp
        entry["copied_path"] = os.path.abspath(copied)

    await asyncio.gather(*(one(src, c) for src, c in mapping.items()))


def run_previews_sync(
    mapping: Dict[str, str],
    work_dir: str,
    state: dict,
    max_size: int = 512,
) -> None:
    asyncio.run(ensure_previews(mapping, work_dir, state, max_size))
