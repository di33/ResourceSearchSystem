"""Upload step of the split pipeline.

Usage:
    python -m ResourceProcessor.upload_resources \
        --crawler-output K:\\ResourceCrawler\\output \
        --db-path pipeline.db --limit 100
"""

from __future__ import annotations

import os
import sys

from ResourceProcessor.pipeline_common import (
    Report,
    env,
    make_arg_parser,
    print_progress,
    state_ge,
    state_lt,
)


def main() -> int:
    parser = make_arg_parser(
        "上传资源到服务端",
        extra_args=[
            ("--server", {"default": None, "help": "服务端地址 (默认 TEST_SERVER_URL env 或 localhost:8000)"}),
            ("--dry-run", {"action": "store_true", "help": "只统计，不实际上传"}),
            ("--force", {"action": "store_true", "help": "重置已提交资源，重新上传（服务端清库或换地址时使用）"}),
        ],
    )
    args = parser.parse_args()

    from ResourceProcessor.cache.local_cache import LocalCacheStore
    from ResourceProcessor.crawler.catalog_loader import load_crawler_catalog
    from ResourceProcessor.crawler.resource_adapter import build_processing_entity
    from ResourceProcessor.core.upload_pipeline import upload_enriched_resources
    from ResourceProcessor.preview_metadata import ProcessState

    db_path = os.path.abspath(args.db_path)
    cache = LocalCacheStore(db_path)

    crawler_output = os.path.abspath(args.crawler_output)
    if not os.path.isdir(crawler_output):
        print(f"错误：crawler output 目录不存在: {crawler_output}", file=sys.stderr)
        return 1

    server = args.server or env("TEST_SERVER_URL", "http://localhost:8000")

    report = Report(label="上传")
    print("=" * 60)
    print("  上传资源 (upload_resources)")
    print(f"  Crawler Output: {crawler_output}")
    print(f"  数据库:         {db_path}")
    print(f"  服务端:         {server}")
    if args.dry_run:
        print("  模式:           dry-run (仅统计)")
    if args.limit:
        print(f"  限制:           {args.limit}")
    print("=" * 60)

    catalog = load_crawler_catalog(crawler_output, db_path=db_path)

    state_counts = cache.count_tasks_by_state()
    report.ok("当前状态统计", ", ".join(f"{k}={v}" for k, v in state_counts.items()) or "(空)")

    if args.force:
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=300)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "UPDATE resource_task SET process_state = 'description_ready', "
            "last_error_code = NULL, last_error_message = NULL "
            "WHERE process_state = 'committed'"
        ).rowcount
        conn.commit()
        conn.close()
        report.ok("重置完成", f"committed -> description_ready: {rows} 个资源")
        state_counts = cache.count_tasks_by_state()
        report.ok("重置后状态", ", ".join(f"{k}={v}" for k, v in state_counts.items()))

    processed = 0
    skipped_desc = 0
    skipped_committed = 0
    success = 0
    failed = 0
    dry_run_count = 0

    for record in catalog.iter_resources(
        limit=args.limit,
        resource_type=args.resource_type,
        source_filter=args.source_filter,
    ):
        # 快速跳过已提交资源：用 source_resource_id 查状态，避免 MD5 计算
        if record.id:
            existing_state = cache.get_task_state_by_source_id(record.id)
            if existing_state:
                if state_ge(existing_state, ProcessState.COMMITTED.value):
                    skipped_committed += 1
                    total = processed + skipped_desc + skipped_committed
                    if total % 1000 == 0:
                        print_progress(processed, total, f"上传成功 {success}, 失败 {failed}, 跳过 {skipped_committed}")
                    continue
                if state_lt(existing_state, ProcessState.DESCRIPTION_READY.value):
                    skipped_desc += 1
                    continue

        entity = build_processing_entity(record)
        task_id, is_existing = cache.upsert_task(entity)

        if is_existing:
            task = cache.get_task_by_id(task_id)
            current_state = ProcessState(task["process_state"]) if task else ProcessState.DISCOVERED
        else:
            current_state = ProcessState.DISCOVERED

        # Skip if description not ready
        if state_lt(current_state.value, ProcessState.DESCRIPTION_READY.value):
            skipped_desc += 1
            continue

        # Skip if already committed
        if state_ge(current_state.value, ProcessState.COMMITTED.value):
            skipped_committed += 1
            continue

        # Rebuild full entity with previews and descriptions from cache
        cached_entity = cache.rebuild_entity_from_cache(task_id)
        if cached_entity is None:
            skipped_desc += 1
            continue
        entity = cached_entity

        if args.dry_run:
            dry_run_count += 1
            processed += 1
            continue

        item = {
            "resource": entity,
            "resource_type": entity.resource_type,
            "description": {
                "main": entity.description_main,
                "detail": entity.description_detail,
                "full": entity.description_full,
            },
        }

        def _report_cb(status, step, detail):
            if status == "OK":
                report.ok(step, detail)
            else:
                report.fail(step, detail)

        summary = upload_enriched_resources([item], server, reporter=_report_cb)

        if summary.success_count > 0:
            success += 1
            cache.update_task_state(task_id, ProcessState.COMMITTED)
        elif summary.skipped_no_files > 0:
            # metadata-only, no uploadable files — stay at current state
            skipped_desc += 1
        elif summary.skipped_count > 0:
            skipped_desc += 1
        else:
            failed += 1
            cache.update_task_state(
                task_id, ProcessState.COMMITTED,
                error_code="upload_error",
                error_message=f"success={summary.success_count} failed={summary.failed_count}",
            )

        processed += 1
        if processed % 25 == 0:
            print_progress(processed, processed + skipped_desc + skipped_committed, f"上传成功 {success}, 失败 {failed}")

    if args.dry_run:
        report.ok("dry-run 完成", f"可上传 {dry_run_count} 个资源 (跳过无描述 {skipped_desc}, 已完成 {skipped_committed})")
    else:
        report.ok("上传完成", f"处理 {processed}, 成功 {success}, 失败 {failed}, 跳过(无描述) {skipped_desc}, 跳过(已完成) {skipped_committed}")

    report.ok("最终状态统计", ", ".join(f"{k}={v}" for k, v in cache.count_tasks_by_state().items()))

    cache.close()
    ok = report.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
