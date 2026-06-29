"""Preview generation step of the split pipeline.

Usage:
    python -m ResourceProcessor.generate_previews \
        --db-path pipeline.db --limit 100
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ResourceProcessor.pipeline_common import (
    Report,
    make_arg_parser,
    print_progress,
    state_ge,
)


PACK_CHILD_PRIORITY = {
    "tiled_map": 5,
    "atlas": 10,
    "spine_skeleton": 15,
    "tileset": 20,
    "animation_sequence": 30,
    "font_file": 40,
    "audio_file": 50,
    "single_image": 70,
}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}
PREVIEW_FILE_EXTS = IMAGE_EXTS | {".avif"}
ATLAS_EXTS = {".xml", ".json"}
TILESET_EXTS = {".tsx"}
OVERVIEW_DIR_NAMES = {"overview", "overviews", "preview", "previews", "sample", "samples", "cover", "covers"}
OVERVIEW_EXACT_STEMS = {"overview", "preview", "sample", "cover", "spritesheet", "tilesheet", "tilemap", "sheet"}
OVERVIEW_SUFFIX_STEMS = ("spritesheet", "tilesheet", "tilemap")


def _name_keys(value: str) -> set[str]:
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        return set()
    name = Path(text).name.lower()
    stem = Path(name).stem.lower()
    compact = re.sub(r"[\s_\-]+", "", stem)
    keys = {name, stem}
    if compact:
        keys.add(compact)
    return {key for key in keys if key}


def _json_reference_keys(value) -> set[str]:
    keys: set[str] = set()
    interesting = {"name", "filename", "file", "source", "image", "imagepath", "image_path"}
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if "." in key_text or "/" in key_text or "\\" in key_text:
                keys.update(_name_keys(key_text))
            if key_text.lower() in interesting and isinstance(child, str):
                keys.update(_name_keys(child))
            keys.update(_json_reference_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_json_reference_keys(child))
    return keys


def _metadata_reference_keys(file_path: str) -> set[str]:
    path = Path(file_path)
    ext = path.suffix.lower()
    keys: set[str] = set()
    try:
        if ext == ".json":
            keys.update(_json_reference_keys(json.loads(path.read_text(encoding="utf-8"))))
        elif ext in ATLAS_EXTS or ext in TILESET_EXTS:
            root = ET.parse(path).getroot()
            for elem in root.iter():
                for attr in ("name", "source", "image", "imagePath", "imagepath", "file", "filename"):
                    if elem is root and attr.lower() in {"source", "image", "imagepath", "image_path"}:
                        continue
                    value = elem.attrib.get(attr, "")
                    if value:
                        keys.update(_name_keys(value))
    except Exception:
        return set()
    return keys


def _resource_file_keys(files: list[dict]) -> set[str]:
    keys: set[str] = set()
    for file_info in files:
        keys.update(_name_keys(file_info.get("file_name") or file_info.get("file_path") or ""))
    return keys


def _raster_source_paths(files: list[dict]) -> list[str]:
    paths: list[str] = []
    for file_info in files:
        file_path = str(file_info.get("file_path") or "")
        if file_path and Path(file_path).suffix.lower() in IMAGE_EXTS and Path(file_path).is_file():
            paths.append(file_path)
    return paths


def _coverage_keys(resource_type: str, files: list[dict]) -> set[str]:
    keys: set[str] = set()
    for file_info in files:
        file_path = file_info.get("file_path") or ""
        if not file_path:
            continue
        ext = Path(file_path).suffix.lower()
        if resource_type == "atlas" and ext in ATLAS_EXTS:
            keys.update(_metadata_reference_keys(file_path))
        elif resource_type == "tileset" and ext in TILESET_EXTS:
            keys.update(_metadata_reference_keys(file_path))
        elif resource_type in {"tileset", "animation_sequence"} and ext in IMAGE_EXTS:
            keys.update(_name_keys(file_info.get("file_name") or file_path))

    if not keys and resource_type in {"tileset", "animation_sequence"}:
        keys.update(_resource_file_keys(files))
    return keys


def _is_overview_single_image(child: dict, files: list[dict], single_image_count: int) -> bool:
    if child["resource_type"] != "single_image" or single_image_count < 8:
        return False
    parts = [
        child.get("resource_path", ""),
        child.get("title", ""),
        *(file_info.get("file_name", "") for file_info in files),
    ]
    for value in parts:
        text = str(value or "").replace("\\", "/").lower().strip()
        if not text:
            continue
        path = Path(text)
        if any(part in OVERVIEW_DIR_NAMES for part in path.parts[:-1]):
            return True
        stem = path.stem
        compact_stem = re.sub(r"[\s_\-]+", "", stem)
        if compact_stem in OVERVIEW_EXACT_STEMS:
            return True
        if any(compact_stem.endswith(suffix) for suffix in OVERVIEW_SUFFIX_STEMS):
            return True
    return False


def _preview_digest(path: str) -> str:
    try:
        import hashlib

        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _delete_old_preview_files(old_previews: list[dict], keep_paths: set[str]) -> tuple[int, int]:
    deleted = 0
    skipped = 0
    keep_keys = {_path_key(path) for path in keep_paths if path}
    seen: set[str] = set()
    for preview in old_previews:
        raw_path = preview.get("path") if isinstance(preview, dict) else ""
        if not raw_path:
            skipped += 1
            continue
        path = Path(raw_path)
        key = _path_key(path)
        if key in seen or key in keep_keys:
            skipped += 1
            continue
        seen.add(key)
        if path.suffix.lower() not in PREVIEW_FILE_EXTS or not path.is_file():
            skipped += 1
            continue
        try:
            path.unlink()
            deleted += 1
        except OSError:
            skipped += 1
    return deleted, skipped


def _latest_existing_primary_preview(cache, task_id: int) -> dict | None:
    rows = cache._conn.execute(
        """SELECT * FROM resource_preview
           WHERE task_id = ? AND role = 'primary' AND path IS NOT NULL AND path <> ''
           ORDER BY id DESC""",
        (task_id,),
    ).fetchall()
    for row in rows:
        preview = dict(row)
        if preview.get("path") and Path(preview["path"]).is_file():
            return preview
    return None


def _chunks(values: list[int], size: int = 800):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _latest_existing_primary_previews(cache, task_ids: list[int]) -> dict[int, dict]:
    previews: dict[int, dict] = {}
    for chunk in _chunks(task_ids):
        placeholders = ",".join("?" * len(chunk))
        rows = cache._conn.execute(
            f"""SELECT * FROM resource_preview
                WHERE task_id IN ({placeholders})
                  AND role = 'primary' AND path IS NOT NULL AND path <> ''
                ORDER BY task_id, id DESC""",
            chunk,
        ).fetchall()
        for row in rows:
            preview = dict(row)
            task_id = int(preview["task_id"])
            if task_id in previews:
                continue
            if preview.get("path") and Path(preview["path"]).is_file():
                previews[task_id] = preview
    return previews


def _files_by_task(cache, task_ids: list[int]) -> dict[int, list[dict]]:
    files: dict[int, list[dict]] = {task_id: [] for task_id in task_ids}
    for chunk in _chunks(task_ids):
        placeholders = ",".join("?" * len(chunk))
        rows = cache._conn.execute(
            f"""SELECT * FROM resource_file
                WHERE task_id IN ({placeholders})
                ORDER BY task_id, is_primary DESC, id""",
            chunk,
        ).fetchall()
        for row in rows:
            item = dict(row)
            files.setdefault(int(item["task_id"]), []).append(item)
    return files


def _pack_child_tasks(cache, task_id: int, entity) -> list[dict]:
    rows = []
    if entity.source_resource_id:
        rows = cache._conn.execute(
            """SELECT * FROM resource_task
               WHERE parent_resource_id = ? AND id <> ? AND resource_type <> 'pack'
               ORDER BY id""",
            (entity.source_resource_id, task_id),
        ).fetchall()
    if not rows and entity.source and entity.pack_name:
        rows = cache._conn.execute(
            """SELECT * FROM resource_task
               WHERE source = ? AND pack_name = ? AND id <> ? AND resource_type <> 'pack'
               ORDER BY id""",
            (entity.source, entity.pack_name, task_id),
        ).fetchall()
    return [dict(row) for row in rows]


def _dedupe_pack_child_previews(records: list[dict]) -> list[dict]:
    records.sort(
        key=lambda item: (
            item["priority"],
            -item["coverage_count"],
            _natural_key(item.get("resource_path") or item.get("title") or ""),
        )
    )
    selected: list[dict] = []
    seen_preview_digests: set[str] = set()
    covered_by_selected: set[str] = set()

    for record in records:
        preview_digest = _preview_digest(record["preview_path"])
        if preview_digest and preview_digest in seen_preview_digests:
            continue
        if preview_digest:
            seen_preview_digests.add(preview_digest)

        file_keys = record.pop("_file_keys", set())
        coverage = record.pop("_coverage_keys", set())
        if record["resource_type"] == "single_image" and file_keys & covered_by_selected:
            continue
        if coverage and any(
            coverage <= selected_record.get("_coverage_keys", set())
            and selected_record["priority"] <= record["priority"]
            for selected_record in selected
        ):
            continue

        record["_coverage_keys"] = coverage
        selected.append(record)
        covered_by_selected.update(coverage)

    cleaned: list[dict] = []
    for record in selected:
        record = dict(record)
        record.pop("_coverage_keys", None)
        record.pop("_file_keys", None)
        cleaned.append(record)
    return cleaned


def _natural_key(value: str) -> list[tuple[int, object]]:
    parts: list[tuple[int, object]] = []
    chunk = ""
    is_digit = False
    for ch in Path(str(value)).name.lower():
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


def _attach_pack_child_previews(cache, task_id: int, entity) -> None:
    if entity.resource_type != "pack":
        return

    records: list[dict] = []
    children = _pack_child_tasks(cache, task_id, entity)
    child_ids = [int(child["id"]) for child in children]
    previews_by_task = _latest_existing_primary_previews(cache, child_ids)
    files_by_task = _files_by_task(cache, child_ids)
    single_image_count = sum(1 for child in children if child["resource_type"] == "single_image")
    all_child_file_keys: set[str] = set()
    for child_id in child_ids:
        all_child_file_keys.update(_resource_file_keys(files_by_task.get(child_id, [])))

    for child in children:
        child_id = int(child["id"])
        preview = previews_by_task.get(child_id)
        if not preview:
            continue
        files = files_by_task.get(child_id, [])
        coverage = _coverage_keys(child["resource_type"], files)
        file_keys = _resource_file_keys(files)
        source_paths = _raster_source_paths(files)
        is_overview_single = _is_overview_single_image(child, files, single_image_count)
        priority = 12 if is_overview_single else PACK_CHILD_PRIORITY.get(child["resource_type"], 100)
        if is_overview_single:
            coverage = set(all_child_file_keys)
        records.append(
            {
                "task_id": child_id,
                "source_resource_id": child.get("source_resource_id", ""),
                "resource_type": child["resource_type"],
                "title": child.get("title", ""),
                "resource_path": child.get("resource_path", ""),
                "preview_path": preview["path"],
                "source_paths": source_paths[:16],
                "preview_strategy": preview.get("strategy", ""),
                "priority": priority,
                "coverage_count": len(coverage),
                "_coverage_keys": coverage,
                "_file_keys": file_keys,
            }
        )

    entity.auxiliary_metadata["child_previews"] = _dedupe_pack_child_previews(records)


def _cached_task_ids(
    cache,
    resource_type: str = "",
    source_filter: str = "",
    limit: int | None = None,
    task_ids: list[int] | None = None,
    min_task_id: int | None = None,
    max_task_id: int | None = None,
) -> list[int]:
    sql = "SELECT id FROM resource_task WHERE 1 = 1"
    params: list = []
    if task_ids:
        placeholders = ",".join(["?"] * len(task_ids))
        sql += f" AND id IN ({placeholders})"
        params.extend(task_ids)
    if min_task_id is not None:
        sql += " AND id >= ?"
        params.append(min_task_id)
    if max_task_id is not None:
        sql += " AND id <= ?"
        params.append(max_task_id)
    if resource_type:
        sql += " AND resource_type = ?"
        params.append(resource_type)
    if source_filter:
        sql += " AND source = ?"
        params.append(source_filter)
    sql += " ORDER BY CASE WHEN resource_type = 'pack' THEN 1 ELSE 0 END, id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [int(row["id"]) for row in cache._conn.execute(sql, params).fetchall()]


def main() -> int:
    parser = make_arg_parser(
        "生成资源预览并写入 SQLite",
        extra_args=[
            ("--work-dir", {"default": None, "help": "工作目录；预览写入 <work-dir>/previews/<resource_type>/"}),
            ("--force", {"action": "store_true", "help": "强制重新生成匹配资源的预览；成功后清除旧预览记录和旧预览文件"}),
            ("--task-id", {"action": "append", "type": int, "help": "只处理指定 task id；可重复传入"}),
            ("--min-task-id", {"type": int, "default": None, "help": "只处理 id >= 该值的 task"}),
            ("--max-task-id", {"type": int, "default": None, "help": "只处理 id <= 该值的 task"}),
        ],
    )
    args = parser.parse_args()

    from ResourceProcessor.cache.local_cache import LocalCacheStore
    from ResourceProcessor.preview.crawler_thumbnail_policy import CrawlerThumbnailPolicy
    from ResourceProcessor.preview_metadata import ProcessState

    db_path = os.path.abspath(args.db_path)
    cache = LocalCacheStore(db_path)

    project_root = Path(__file__).resolve().parents[3]
    work_dir = os.path.abspath(args.work_dir) if args.work_dir else str(project_root / "data" / "workdirs" / "test_workdir_crawler")
    previews_dir = os.path.join(work_dir, "previews")
    os.makedirs(previews_dir, exist_ok=True)

    report = Report(label="预览生成")
    print("=" * 60)
    print("  预览生成 (generate_previews)")
    print("  数据源:         DB-only")
    print(f"  数据库:         {db_path}")
    print(f"  预览目录:       {previews_dir}")
    if args.limit:
        print(f"  限制:           {args.limit}")
    if args.resource_type:
        print(f"  资源类型:       {args.resource_type}")
    if args.task_id:
        print(f"  Task过滤:       {', '.join(str(item) for item in args.task_id)}")
    if args.min_task_id is not None or args.max_task_id is not None:
        print(f"  Task范围:       {args.min_task_id or '-inf'}..{args.max_task_id or '+inf'}")
    if args.force:
        print("  强制刷新:       enabled")
    print("=" * 60)

    policy = CrawlerThumbnailPolicy(previews_dir)

    state_counts = cache.count_tasks_by_state()
    report.ok("当前状态统计", ", ".join(f"{k}={v}" for k, v in state_counts.items()) or "(空)")

    processed = 0
    skipped = 0
    preview_count = 0
    deleted_preview_count = 0
    deleted_preview_file_count = 0
    skipped_preview_file_count = 0
    failed = 0

    def process_entity(task_id: int, entity, *, update_state: bool = True) -> bool:
        nonlocal preview_count, deleted_preview_count, deleted_preview_file_count, skipped_preview_file_count, failed
        _attach_pack_child_previews(cache, task_id, entity)
        try:
            old_previews = cache.get_previews_by_task(task_id)
            previews = asyncio.run(policy.generate_previews(entity))
            entity.previews = previews
            deleted_preview_count += cache.delete_previews_by_task(task_id)
            for preview in previews:
                if preview.path:
                    cache.insert_preview(task_id, preview)
                    preview_count += 1
            deleted_files, skipped_files = _delete_old_preview_files(
                old_previews,
                {preview.path for preview in previews if preview.path},
            )
            deleted_preview_file_count += deleted_files
            skipped_preview_file_count += skipped_files
            if update_state:
                cache.update_task_state(task_id, ProcessState.PREVIEW_READY)
        except Exception as exc:
            failed += 1
            if update_state:
                cache.update_task_state(
                    task_id, ProcessState.PREVIEW_FAILED,
                    error_code="preview_error",
                    error_message=str(exc)[:500],
                )
            else:
                cache.record_task_error(task_id, "preview_error", str(exc)[:500])
            report.fail(
                f"预览 [{entity.title or entity.resource_path or entity.content_md5[:12]}]",
                str(exc)[:120],
            )
            return False

        return True

    for task_id in _cached_task_ids(
        cache,
        args.resource_type,
        args.source_filter,
        args.limit,
        args.task_id,
        args.min_task_id,
        args.max_task_id,
    ):
        task = cache.get_task_by_id(task_id)
        if task is None:
            failed += 1
            continue
        current_state = ProcessState(task["process_state"])
        already_previewed = state_ge(current_state.value, ProcessState.PREVIEW_READY.value)
        if already_previewed and not args.force:
            skipped += 1
            continue
        entity = cache.rebuild_entity_from_cache(task_id)
        if entity is None:
            failed += 1
            continue
        preserve_later_state = already_previewed and args.force
        if process_entity(task_id, entity, update_state=not preserve_later_state):
            processed += 1
        if processed % 25 == 0 and processed:
            print_progress(processed, processed + skipped, f"预览累计 {preview_count}, 跳过 {skipped}")

    report.ok(
        "预览生成",
        (
            f"处理 {processed}, 跳过 {skipped}, 失败 {failed}, 清旧记录 {deleted_preview_count}, "
            f"删旧文件 {deleted_preview_file_count}, 文件跳过 {skipped_preview_file_count}, 预览图片 {preview_count} 张"
        ),
    )
    report.ok("最终状态统计", ", ".join(f"{k}={v}" for k, v in cache.count_tasks_by_state().items()))

    cache.close()
    ok = report.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
