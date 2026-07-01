"""Upload step of the split pipeline.

Usage:
    python -m ResourceProcessor.upload_resources \
        --db-path pipeline.db --limit 100
"""

from __future__ import annotations

import asyncio
import os

from ResourceProcessor.pipeline_common import (
    Report,
    env,
    make_arg_parser,
    print_progress,
    state_ge,
    state_lt,
)


async def _upload_one(task_id, item, server, session, report, semaphore, counters):
    """Upload a single resource with concurrency control."""
    from ResourceProcessor.core.upload_pipeline import upload_enriched_resources
    from ResourceProcessor.preview_metadata import ProcessState

    last_error: dict[str, str] = {}

    def _report_cb(status, step, detail):
        if status == "OK":
            report.ok(step, detail)
        else:
            last_error["message"] = f"{step}: {detail}"[:2000]
            report.fail(step, detail)

    async with semaphore:
        summary = await asyncio.to_thread(
            upload_enriched_resources,
            [item], server,
            reporter=_report_cb,
            session=session, health_checked=True,
        )
        if summary.success_count > 0:
            counters["success"] += 1
            counters["cache"].update_task_state(task_id, ProcessState.COMMITTED)
        elif summary.skipped_no_files > 0:
            counters["skipped_desc"] += 1
        elif summary.skipped_count > 0:
            counters["skipped_desc"] += 1
        else:
            counters["failed"] += 1
            error_message = last_error.get("message")
            if not error_message:
                error_message = f"success={summary.success_count} failed={summary.failed_count}"
            counters["cache"].record_task_error(
                task_id,
                error_code="upload_error",
                error_message=error_message,
            )
        counters["processed"] += 1
        total = counters["processed"]
        if total % 25 == 0:
            print_progress(total, total, f"上传成功 {counters['success']}, 失败 {counters['failed']}")


_SENTINEL = None  # Queue poison pill


def _iter_upload_task_ids(cache, process_state, *, limit, resource_type, source_filter):
    """Yield uploadable task ids from both description and classify-ready states."""
    yielded = 0
    states = [process_state.DESCRIPTION_READY, process_state.CLASSIFY_READY]
    for state in states:
        remaining = None if limit is None else max(limit - yielded, 0)
        if remaining == 0:
            break
        for task_id in cache.iter_tasks_by_state(
            state,
            limit=remaining,
            resource_type=resource_type,
            source=source_filter,
        ):
            yield task_id
            yielded += 1
            if limit is not None and yielded >= limit:
                return


async def _upload_worker(queue, server, session, report, semaphore, counters):
    """Consume items from queue and upload them."""
    while True:
        item = await queue.get()
        if item is _SENTINEL:
            queue.task_done()
            break
        task_id, upload_item = item
        await _upload_one(task_id, upload_item, server, session, report, semaphore, counters)
        queue.task_done()


def main() -> int:
    parser = make_arg_parser(
        "上传资源到服务端",
        extra_args=[
            ("--server", {"default": None, "help": "服务端地址 (默认 TEST_SERVER_URL env 或 localhost:8000)"}),
            ("--dry-run", {"action": "store_true", "help": "只统计，不实际上传"}),
            ("--force", {"action": "store_true", "help": "重置已提交资源，重新上传（服务端清库或换地址时使用）"}),
            ("--retry-failed", {"action": "store_true", "help": "仅重置上传失败的资源（last_error_code=upload_error），重新上传"}),
            ("--concurrency", {"type": int, "default": 5, "help": "并发上传数 (默认 5)"}),
        ],
    )
    args = parser.parse_args()

    from ResourceProcessor.cache.local_cache import LocalCacheStore
    from ResourceProcessor.preview_metadata import ProcessState

    db_path = os.path.abspath(args.db_path)
    cache = LocalCacheStore(db_path)

    server = args.server or env("TEST_SERVER_URL", "http://localhost:8000")

    report = Report(label="上传")
    print("=" * 60)
    print("  上传资源 (upload_resources)")
    print("  数据源:         DB-only")
    print(f"  数据库:         {db_path}")
    print(f"  服务端:         {server}")
    print(f"  并发数:         {args.concurrency}")
    if args.dry_run:
        print("  模式:           dry-run (仅统计)")
    if args.limit:
        print(f"  限制:           {args.limit}")
    print("=" * 60)

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

    if args.retry_failed:
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=300)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "UPDATE resource_task SET process_state = 'description_ready', "
            "last_error_code = NULL, last_error_message = NULL "
            "WHERE process_state = 'committed' AND last_error_code = 'upload_error'"
        ).rowcount
        conn.commit()
        conn.close()
        report.ok("重置失败资源", f"committed -> description_ready: {rows} 个资源")
        state_counts = cache.count_tasks_by_state()
        report.ok("重置后状态", ", ".join(f"{k}={v}" for k, v in state_counts.items()))

    # Create shared session for connection reuse
    import requests as _requests
    session = _requests.Session()

    # Health check once
    try:
        health_resp = session.get(f"{server}/health", timeout=5)
        health = health_resp.json()
        if health.get("status") != "ok":
            report.fail("服务端健康检查", f"状态: {health.get('status')}")
            cache.close()
            return 1
        report.ok("服务端健康检查", "所有组件正常")
    except Exception as exc:
        report.fail("服务端健康检查", f"无法连接: {exc}")
        cache.close()
        return 1

    def _build_item(task_id: int) -> dict | None:
        """从 cache 重建 entity 并构建上传 item；状态不满足则返回 None。"""
        task = cache.get_task_by_id(task_id)
        current_state = ProcessState(task["process_state"]) if task else ProcessState.DISCOVERED
        if state_lt(current_state.value, ProcessState.DESCRIPTION_READY.value):
            return None
        if state_ge(current_state.value, ProcessState.COMMITTED.value):
            return None
        cached_entity = cache.rebuild_entity_from_cache(task_id)
        if cached_entity is None:
            return None
        return {
            "resource": cached_entity,
            "resource_type": cached_entity.resource_type,
            "description": {
                "main": cached_entity.description_main,
                "detail": cached_entity.description_detail,
                "full": cached_entity.description_full,
                "usage_space": cached_entity.usage_space,
                "usage_category": cached_entity.usage_category,
                "usage_subcategories": cached_entity.usage_subcategories,
                "usage_classification_reason": cached_entity.usage_classification_reason,
                "usage_classification_suggestion": cached_entity.usage_classification_suggestion,
                "usage_classification_version": cached_entity.usage_classification_version,
            },
        }

    async def _stream_and_upload_db():
        """DB-only：从 resource_task 表直接遍历 description_ready 的记录。"""
        counters = {"processed": 0, "success": 0, "failed": 0, "skipped_desc": 0, "cache": cache}
        semaphore = asyncio.Semaphore(args.concurrency)
        queue: asyncio.Queue = asyncio.Queue(maxsize=args.concurrency * 4)
        loop = asyncio.get_running_loop()

        workers = [
            asyncio.create_task(_upload_worker(queue, server, session, report, semaphore, counters))
            for _ in range(args.concurrency)
        ]

        def _feed_queue():
            for task_id in _iter_upload_task_ids(
                cache,
                ProcessState,
                limit=args.limit,
                resource_type=args.resource_type,
                source_filter=args.source_filter,
            ):
                item = _build_item(task_id)
                if item is None:
                    continue
                asyncio.run_coroutine_threadsafe(queue.put((task_id, item)), loop).result()

            for _ in workers:
                asyncio.run_coroutine_threadsafe(queue.put(_SENTINEL), loop).result()

        await loop.run_in_executor(None, _feed_queue)
        await asyncio.gather(*workers)
        return counters

    if args.dry_run:
        dry_run_count = 0
        skipped_desc = 0
        for task_id in _iter_upload_task_ids(
            cache,
            ProcessState,
            limit=args.limit,
            resource_type=args.resource_type,
            source_filter=args.source_filter,
        ):
            cached_entity = cache.rebuild_entity_from_cache(task_id)
            if cached_entity is None:
                skipped_desc += 1
                continue
            dry_run_count += 1
        report.ok("dry-run 完成", f"可上传 {dry_run_count} 个资源 (跳过无描述 {skipped_desc})")
    else:
        counters = asyncio.run(_stream_and_upload_db())
        report.ok("上传完成", f"处理 {counters['processed']}, 成功 {counters['success']}, 失败 {counters['failed']}")

    report.ok("最终状态统计", ", ".join(f"{k}={v}" for k, v in cache.count_tasks_by_state().items()))

    cache.close()
    ok = report.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
