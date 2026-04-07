"""
增量管线：同一 work_dir 重复执行时，按「源文件路径 + 指纹」跳过已处理资源，避免重复拷贝与重复生成预览。
状态保存在 work_dir/.pipeline_state.json。

支持多文件资源（同目录下的多个文件 = 同一个资源）和多预览。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

from PIL import Image

from ResourceProcessor.preview_metadata import PreviewInfo, PreviewStrategy
from ResourceProcessor.core.resource_filter import (
    copy_single_categorized_resource,
    group_files_by_directory,
    determine_file_role,
    compute_composite_md5,
)
from ResourceProcessor.preview.thumbnail_generator import ThumbnailGenerator, validate_preview

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
        return {"version": 2, "by_source": {}, "by_directory": {}}
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data.get("by_source"), dict):
        data["by_source"] = {}
    if not isinstance(data.get("by_directory"), dict):
        data["by_directory"] = {}
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
            entry["preview_paths"] = []
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
            "preview_paths": [],
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
            "preview_paths": e.get("preview_paths"),
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
    """对每个文件生成对应的预览图，支持多文件多预览。

    对图片生成 webp 缩略图，对 .fbx 生成旋转预览 GIF；占位图不会阻止后续重试。
    """
    previews_dir = Path(work_dir) / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    gen = ThumbnailGenerator(str(previews_dir))
    by_source = state.setdefault("by_source", {})

    async def one(src: str, copied: str) -> Tuple[str, PreviewInfo | None]:
        key = norm_source(src)
        cp = Path(copied)
        ext = cp.suffix.lower()
        if ext not in IMAGE_EXTS and ext != FBX_EXT:
            return src, None

        fp = fingerprint(src)
        entry = dict(by_source.get(key, {}))
        existing_preview_paths = entry.get("preview_paths", [])
        preview_renderer = entry.get("preview_renderer")

        # Check if preview already exists and source unchanged
        if existing_preview_paths and entry.get("fingerprint") == fp:
            if all(os.path.isfile(p) for p in existing_preview_paths):
                return src, None

        content_hash = _content_md5(copied)
        preview_info: PreviewInfo | None = None

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
            entry["preview_paths"] = []
            entry["preview_failed"] = True
            entry["preview_fail_reason"] = reason
            preview_info = PreviewInfo(
                strategy=PreviewStrategy.STATIC if ext in IMAGE_EXTS else PreviewStrategy.GIF,
                role="primary",
                fail_reason=reason,
            )
        else:
            abs_out = os.path.abspath(out_path)
            entry["preview_paths"] = [abs_out]
            entry.pop("preview_failed", None)
            entry.pop("preview_fail_reason", None)
            with Image.open(out_path) as im:
                w, h = im.size
            preview_info = PreviewInfo(
                strategy=PreviewStrategy.STATIC if ext in IMAGE_EXTS else PreviewStrategy.GIF,
                role="primary",
                path=abs_out,
                format=Path(out_path).suffix.lstrip('.'),
                width=w,
                height=h,
                size=os.path.getsize(out_path),
                renderer=preview_renderer,
                used_placeholder=(preview_renderer == "placeholder"),
            )
        entry["preview_renderer"] = preview_renderer
        entry["fingerprint"] = fp
        entry["copied_path"] = os.path.abspath(copied)
        return src, preview_info

    results = await asyncio.gather(*(one(src, c) for src, c in mapping.items()))

    # Group previews by directory for multi-file resources
    dir_groups = group_files_by_directory([src for src, _ in mapping.items()])
    by_directory = state.setdefault("by_directory", {})

    for dir_path, file_paths in dir_groups.items():
        dir_key = norm_source(dir_path)
        previews_list = []
        copied_paths = []

        for fp in file_paths:
            key = norm_source(fp)
            entry = by_source.get(key, {})
            if entry.get("copied_path"):
                copied_paths.append(entry["copied_path"])
            prev_paths = entry.get("preview_paths", [])
            for pp in prev_paths:
                if os.path.isfile(pp):
                    with Image.open(pp) as im:
                        w, h = im.size
                    file_role, is_primary = determine_file_role(fp, file_paths)
                    preview_info = PreviewInfo(
                        strategy=PreviewStrategy.STATIC if Path(pp).suffix.lower() in {".webp", ".png", ".jpg", ".jpeg"} else PreviewStrategy.GIF,
                        role="primary" if is_primary else "gallery",
                        path=os.path.abspath(pp),
                        format=Path(pp).suffix.lstrip('.'),
                        width=w,
                        height=h,
                        size=os.path.getsize(pp),
                        renderer=entry.get("preview_renderer", "pillow"),
                        used_placeholder=False,
                    )
                    previews_list.append(preview_info.to_dict())

        by_directory[dir_key] = {
            "source_directory": dir_path,
            "copied_paths": copied_paths,
            "previews": previews_list,
            "composite_md5": compute_composite_md5(file_paths),
        }


def run_previews_sync(
    mapping: Dict[str, str],
    work_dir: str,
    state: dict,
    max_size: int = 512,
) -> None:
    asyncio.run(ensure_previews(mapping, work_dir, state, max_size))


def get_resource_entities(state: dict) -> List[Dict[str, Any]]:
    """Extract per-directory resource entities from the pipeline state.

    Returns a list of dicts ready to be converted to ResourceProcessingEntity.
    """
    by_directory = state.get("by_directory", {})
    by_source = state.get("by_source", {})
    resources = []

    for dir_key, dir_data in by_directory.items():
        files = []
        copied_paths = dir_data.get("copied_paths", [])

        for i, cp in enumerate(copied_paths):
            src_key = None
            for k, v in by_source.items():
                if v.get("copied_path") == cp:
                    src_key = k
                    break

            if src_key and src_key in by_source:
                src_entry = by_source[src_key]
                fp_orig = src_key
                fn = os.path.basename(fp_orig)
                ext = Path(fn).suffix.lower()
                file_size = os.path.getsize(cp) if os.path.isfile(cp) else 0
                file_md5 = _content_md5(cp) if os.path.isfile(cp) else ""
                file_role, is_primary = determine_file_role(fp_orig, [fp_orig])

                files.append({
                    "file_path": cp,
                    "file_name": fn,
                    "file_size": file_size,
                    "file_format": ext.lstrip('.'),
                    "content_md5": file_md5,
                    "file_role": file_role,
                    "is_primary": is_primary,
                })

        previews = []
        for p in dir_data.get("previews", []):
            previews.append(p)

        resources.append({
            "source_directory": dir_data.get("source_directory", dir_key),
            "content_md5": dir_data.get("composite_md5", ""),
            "files": files,
            "previews": previews,
        })

    return resources
