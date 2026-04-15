"""Preview generation step of the split pipeline.

Usage:
    python -m ResourceProcessor.generate_previews \
        --crawler-output K:\\ResourceCrawler\\output \
        --db-path pipeline.db --limit 100
"""

from __future__ import annotations

import asyncio
import os
import sys

from ResourceProcessor.pipeline_common import (
    Report,
    env,
    make_arg_parser,
    print_progress,
    state_ge,
)


def main() -> int:
    parser = make_arg_parser(
        "生成资源预览并写入 SQLite",
        extra_args=[
            ("--work-dir", {"default": None, "help": "预览输出目录 (默认 <project>/test_workdir_crawler/previews)"}),
        ],
    )
    args = parser.parse_args()

    from ResourceProcessor.cache.local_cache import LocalCacheStore
    from ResourceProcessor.crawler.catalog_loader import load_crawler_catalog
    from ResourceProcessor.crawler.resource_adapter import build_processing_entity
    from ResourceProcessor.preview.crawler_thumbnail_policy import CrawlerThumbnailPolicy
    from ResourceProcessor.preview_metadata import ProcessState

    db_path = os.path.abspath(args.db_path)
    cache = LocalCacheStore(db_path)

    crawler_output = os.path.abspath(args.crawler_output)
    if not os.path.isdir(crawler_output):
        print(f"错误：crawler output 目录不存在: {crawler_output}", file=sys.stderr)
        return 1

    # Determine previews output dir
    from pathlib import Path
    project_root = Path(__file__).resolve().parents[2]
    work_dir = os.path.abspath(args.work_dir) if args.work_dir else str(project_root / "test_workdir_crawler")
    previews_dir = os.path.join(work_dir, "previews")
    os.makedirs(previews_dir, exist_ok=True)

    report = Report(label="预览生成")
    print("=" * 60)
    print("  预览生成 (generate_previews)")
    print(f"  Crawler Output: {crawler_output}")
    print(f"  数据库:         {db_path}")
    print(f"  预览目录:       {previews_dir}")
    if args.limit:
        print(f"  限制:           {args.limit}")
    print("=" * 60)

    catalog = load_crawler_catalog(crawler_output, db_path=db_path)
    policy = CrawlerThumbnailPolicy(previews_dir)

    state_counts = cache.count_tasks_by_state()
    report.ok("当前状态统计", ", ".join(f"{k}={v}" for k, v in state_counts.items()) or "(空)")

    processed = 0
    skipped = 0
    preview_count = 0
    failed = 0

    for record in catalog.iter_resources(
        limit=args.limit,
        resource_type=args.resource_type,
        source_filter=args.source_filter,
    ):
        # --resume 快速跳过：用 source_resource_id 查状态，避免 MD5 计算
        if args.resume and record.id:
            existing_state = cache.get_task_state_by_source_id(record.id)
            if existing_state and state_ge(existing_state, ProcessState.PREVIEW_READY.value):
                skipped += 1
                if skipped % 1000 == 0:
                    print_progress(processed, processed + skipped, f"预览累计 {preview_count}, 跳过 {skipped}")
                continue

        entity = build_processing_entity(record)
        task_id, is_existing = cache.upsert_task(entity)

        if is_existing:
            task = cache.get_task_by_id(task_id)
            current_state = ProcessState(task["process_state"]) if task else ProcessState.DISCOVERED
            if args.resume and state_ge(current_state.value, ProcessState.PREVIEW_READY.value):
                skipped += 1
                continue

        try:
            previews = asyncio.run(policy.generate_previews(entity))
            entity.previews = previews
            for preview in previews:
                if preview.path:
                    cache.insert_preview(task_id, preview)
                    preview_count += 1
            cache.update_task_state(task_id, ProcessState.PREVIEW_READY)
        except Exception as exc:
            failed += 1
            cache.update_task_state(
                task_id, ProcessState.PREVIEW_FAILED,
                error_code="preview_error",
                error_message=str(exc)[:500],
            )
            report.fail(
                f"预览 [{entity.title or entity.resource_path or entity.content_md5[:12]}]",
                str(exc)[:120],
            )
            continue

        processed += 1
        if processed % 25 == 0:
            print_progress(processed, processed + skipped, f"预览累计 {preview_count}, 跳过 {skipped}")

    report.ok("预览生成", f"处理 {processed}, 跳过 {skipped}, 失败 {failed}, 预览图片 {preview_count} 张")
    report.ok("最终状态统计", ", ".join(f"{k}={v}" for k, v in cache.count_tasks_by_state().items()))

    cache.close()
    ok = report.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
