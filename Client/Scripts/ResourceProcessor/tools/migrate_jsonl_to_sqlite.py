"""Migrate existing JSONL data (crawler_resources.jsonl / test_results.jsonl) to SQLite.

Usage:
    python -m ResourceProcessor.tools.migrate_jsonl_to_sqlite \
        --resources-jsonl test_workdir/crawler_resources.jsonl \
        --results-jsonl test_workdir/test_results.jsonl \
        --db-path pipeline.db \
        [--crawler-output K:\\ResourceCrawler\\output] \
        [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from ResourceProcessor.pipeline_common import (
    Report,
)


def _iter_jsonl(path: str):
    if not os.path.isfile(path):
        return
    with open(path, "rb") as f:
        for raw_line in f:
            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue


def _build_resource_index(crawler_output: str) -> dict[str, dict]:
    """Read resource_index.jsonl and build a lookup by id. Fast: ~1s for 460k lines."""
    index_path = os.path.join(crawler_output, "metadata", "resource_index.jsonl")
    if not os.path.isfile(index_path):
        return {}
    index: dict[str, dict] = {}
    for entry in _iter_jsonl(index_path):
        rid = entry.get("id", "")
        if rid:
            index[rid] = entry
    return index


def _compute_content_md5(entry: dict) -> str:
    """Compute the same fingerprint as compute_resource_fingerprint()."""
    payload = {
        "id": entry.get("id", ""),
        "source": entry.get("source", ""),
        "pack_name": entry.get("pack_name", ""),
        "resource_type": entry.get("resource_type", ""),
        "resource_path": entry.get("resource_path", ""),
        "member_count": entry.get("member_count", 0),
        "file_paths": entry.get("file_paths", []),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.md5(blob).hexdigest()


def _resolve_file_paths(
    crawler_output: str,
    source: str,
    pack_name: str,
    file_paths: list[str],
) -> list[dict]:
    """Resolve relative file_paths to absolute, return file info dicts."""
    assets_root = os.path.join(crawler_output, "assets")
    resolved: list[dict] = []
    for rel_path in file_paths:
        abs_path = os.path.abspath(os.path.join(assets_root, source, pack_name, rel_path))
        if not os.path.isfile(abs_path):
            continue
        file_name = Path(abs_path).name
        file_format = Path(abs_path).suffix.lstrip(".").lower()
        resolved.append({
            "file_path": abs_path,
            "file_name": file_name,
            "file_size": os.path.getsize(abs_path),
            "file_format": file_format,
            "file_role": "main",
        })
    return resolved


def _pick_primary(files: list[dict], resource_type: str) -> list[dict]:
    """Mark the first raster image as primary."""
    if not files:
        return files
    image_exts = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg"}
    for f in files:
        if f["file_format"] in image_exts:
            f["is_primary"] = True
            return files
    files[0]["is_primary"] = True
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="将 JSONL 状态迁移到 SQLite")
    parser.add_argument("--resources-jsonl", required=True, help="crawler_resources.jsonl 路径")
    parser.add_argument("--results-jsonl", required=True, help="test_results.jsonl 路径")
    parser.add_argument("--db-path", default="pipeline.db", help="SQLite 数据库路径")
    parser.add_argument("--crawler-output", default=None, help="ResourceCrawler output 目录 (用于补充 resource_file)")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写入")
    args = parser.parse_args()

    from ResourceProcessor.cache.local_cache import LocalCacheStore
    from ResourceProcessor.preview_metadata import (
        FileInfo,
        PreviewInfo,
        PreviewStrategy,
        ProcessState,
        ResourceProcessingEntity,
    )

    db_path = os.path.abspath(args.db_path)
    report = Report(label="迁移")

    if not os.path.isfile(args.resources_jsonl):
        print(f"错误：crawler_resources.jsonl 不存在: {args.resources_jsonl}", file=sys.stderr)
        return 1

    # Build resource_index lookup (fast: read resource_index.jsonl, ~1s for 460k lines)
    resource_index: dict[str, dict] = {}
    if args.crawler_output and os.path.isdir(args.crawler_output):
        resource_index = _build_resource_index(args.crawler_output)
        report.ok("加载 resource_index.jsonl", f"{len(resource_index)} 条索引")

    # --- Phase 1: migrate crawler_resources.jsonl (preview state + files) ---
    resources_rows = list(_iter_jsonl(args.resources_jsonl))
    report.ok("读取 crawler_resources.jsonl", f"{len(resources_rows)} 条")

    if not args.dry_run:
        cache = LocalCacheStore(db_path)

    migrated_preview = 0
    skipped_preview_dup = 0
    failed_preview = 0
    files_backfilled = 0

    for row in resources_rows:
        source_resource_id = row.get("source_resource_id", "")
        resource_type = row.get("resource_type", "")
        title = row.get("title", "")
        pack_name = row.get("pack_name", "")
        resource_path = row.get("resource_path", "")
        preview_paths = row.get("preview_paths", [])

        # Get content_md5 and file info from resource_index
        index_entry = resource_index.get(source_resource_id)
        if index_entry is not None:
            content_md5 = _compute_content_md5(index_entry)
            source = index_entry.get("source", "")
            file_paths = index_entry.get("file_paths", [])
        else:
            # Fallback: compute from available fields (won't have files)
            payload = {"source_resource_id": source_resource_id, "resource_path": resource_path}
            content_md5 = hashlib.md5(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest()
            source = ""
            file_paths = []

        if args.dry_run:
            migrated_preview += 1
            if file_paths:
                files_backfilled += len(file_paths)
            continue

        # Check for existing task
        existing = cache.get_tasks_by_md5(content_md5)
        if existing:
            skipped_preview_dup += 1
            continue

        # Resolve file_paths to FileInfo list
        files: list[FileInfo] = []
        if file_paths and args.crawler_output:
            resolved = _resolve_file_paths(args.crawler_output, source, pack_name, file_paths)
            resolved = _pick_primary(resolved, resource_type)
            files = [
                FileInfo(
                    file_path=f["file_path"],
                    file_name=f["file_name"],
                    file_size=f["file_size"],
                    file_format=f["file_format"],
                    content_md5="",
                    file_role=f["file_role"],
                    is_primary=f.get("is_primary", False),
                )
                for f in resolved
            ]
            files_backfilled += len(files)

        # Build entity for insertion
        entity = ResourceProcessingEntity(
            resource_type=resource_type,
            source_directory="",
            content_md5=content_md5,
            source=source,
            source_resource_id=source_resource_id,
            title=title,
            pack_name=pack_name,
            resource_path=resource_path,
            files=files,
            process_state=ProcessState.PREVIEW_READY,
        )

        try:
            task_id = cache.insert_task(entity)

            # Insert preview records
            for preview_path in preview_paths:
                if isinstance(preview_path, str) and os.path.isfile(preview_path):
                    preview = PreviewInfo(
                        strategy=PreviewStrategy.STATIC,
                        role="primary",
                        path=preview_path,
                    )
                    cache.insert_preview(task_id, preview)

            cache.update_task_state(task_id, ProcessState.PREVIEW_READY)
            migrated_preview += 1
        except Exception as exc:
            failed_preview += 1
            report.fail(f"预览迁移 [{title or source_resource_id}]", str(exc)[:120])

    report.ok(
        "预览迁移",
        f"成功 {migrated_preview}, 重复跳过 {skipped_preview_dup}, 失败 {failed_preview}, "
        f"文件 {files_backfilled} 个",
    )

    # --- Phase 2: migrate test_results.jsonl (description state) ---
    results_rows = list(_iter_jsonl(args.results_jsonl)) if os.path.isfile(args.results_jsonl) else []
    report.ok("读取 test_results.jsonl", f"{len(results_rows)} 条")

    migrated_desc = 0
    skipped_desc_no_task = 0
    skipped_desc_dup = 0
    failed_desc = 0

    for row in results_rows:
        source_resource_id = row.get("source_resource_id", "")
        description_main = row.get("description_main", "")
        description_detail = row.get("description_detail", "")
        description_full = row.get("description_full", "")

        if not description_full.strip():
            continue

        # Find task by content_md5
        index_entry = resource_index.get(source_resource_id)
        if index_entry is not None:
            content_md5 = _compute_content_md5(index_entry)
        else:
            payload = {"source_resource_id": source_resource_id, "resource_path": row.get("resource_path", "")}
            content_md5 = hashlib.md5(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest()

        if args.dry_run:
            migrated_desc += 1
            continue

        existing_tasks = cache.get_tasks_by_md5(content_md5)
        if not existing_tasks:
            skipped_desc_no_task += 1
            continue

        task = existing_tasks[0]
        task_id = task["id"]

        # Check if description already exists
        existing_desc = cache.get_description_by_task(task_id)
        if existing_desc:
            skipped_desc_dup += 1
            continue

        try:
            cache.insert_description(
                task_id,
                main_content=description_main,
                detail_content=description_detail,
                full_description=description_full,
                prompt_version="migrated",
            )
            cache.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
            migrated_desc += 1
        except Exception as exc:
            failed_desc += 1
            report.fail(f"描述迁移 [{source_resource_id}]", str(exc)[:120])

    report.ok(
        "描述迁移",
        f"成功 {migrated_desc}, 无任务跳过 {skipped_desc_no_task}, 重复跳过 {skipped_desc_dup}, 失败 {failed_desc}",
    )

    # --- Summary ---
    if not args.dry_run:
        report.ok("最终状态统计", ", ".join(f"{k}={v}" for k, v in cache.count_tasks_by_state().items()))
        cache.close()
    else:
        report.ok("dry-run", "未写入任何数据")

    ok = report.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
