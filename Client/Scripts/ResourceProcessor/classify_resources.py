"""Usage classification step of the split pipeline.

Usage:
    python -m ResourceProcessor.classify_resources \
        --db-path pipeline.db --limit 100
"""

from __future__ import annotations

import asyncio
import os
import random

from ResourceProcessor.pipeline_common import (
    Report,
    env,
    make_arg_parser,
    print_progress,
)


# ---------------------------------------------------------------------------
# LLM provider registration
# ---------------------------------------------------------------------------

try:
    import ResourceProcessor.description.dashscope_llm_provider  # noqa: F401
except Exception:
    pass
try:
    import ResourceProcessor.description.zhipu_llm_provider  # noqa: F401
except Exception:
    pass
try:
    import ResourceProcessor.description.ksyun_llm_provider  # noqa: F401
except Exception:
    pass
try:
    import ResourceProcessor.description.codex_exec_provider  # noqa: F401
except Exception:
    pass


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


async def _classify_with_retry(
    cache,
    task_id: int,
    entity,
    llm_provider: str,
    report: Report,
    max_attempts: int = 6,
    base_delay_seconds: float = 3.0,
    success_delay_seconds: float = 0,
    llm_model: str = "",
) -> bool:
    """Generate usage classification with rate-limit retry."""
    from ResourceProcessor.crawler.resource_adapter import build_description_input
    from ResourceProcessor.description.description_generator import classify_resource_usage

    desc_input = build_description_input(entity)
    main_content = entity.description_main
    detail_content = entity.description_detail
    if not (main_content or detail_content):
        raise RuntimeError("resource has no generated description")

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            classification = await classify_resource_usage(
                desc_input,
                main_content=main_content,
                detail_content=detail_content,
                provider_name=llm_provider,
                model=llm_model,
            )
            cache.insert_description(
                task_id,
                main_content=entity.description_main,
                detail_content=entity.description_detail,
                full_description=entity.description_full,
                prompt_version=entity.prompt_version,
                quality_score=entity.description_quality_score,
                usage_space=classification.space,
                usage_category=classification.category,
                usage_subcategories=classification.subcategories,
                usage_classification_reason=classification.reason,
                usage_classification_suggestion=classification.suggestion,
                usage_classification_version=classification.version,
            )
            if success_delay_seconds > 0:
                await asyncio.sleep(success_delay_seconds)
            return True
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_rate_limit_error(exc):
                break
            delay = base_delay_seconds * (2 ** (attempt - 1)) + random.uniform(0.0, 1.0)
            report.ok(
                f"限流退避 [{entity.title or entity.resource_path or entity.content_md5[:12]}]",
                f"第 {attempt} 次重试，等待 {delay:.1f}s",
            )
            await asyncio.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("classification generation failed without exception")


def _get_retry_candidates(cache, max_retries: int) -> list[dict]:
    from ResourceProcessor.preview_metadata import ProcessState

    rows = cache.get_tasks_by_state(ProcessState.DESCRIPTION_READY)
    failed = [r for r in rows if r.get("last_error_code") == "classification_error"]
    return [r for r in failed if r["retry_count"] < max_retries]


async def _process_one(
    task_id,
    entity,
    llm_provider,
    llm_model,
    cache,
    report,
    semaphore,
    counters,
):
    from ResourceProcessor.preview_metadata import ProcessState

    async with semaphore:
        try:
            await _classify_with_retry(
                cache,
                task_id,
                entity,
                llm_provider,
                report,
                llm_model=llm_model,
            )
            cache.update_task_state(task_id, ProcessState.CLASSIFY_READY)
            counters["class_ok"] += 1
        except Exception as exc:
            cache.record_task_error(
                task_id,
                error_code="classification_error",
                error_message=str(exc)[:500],
            )
            counters["failed"] += 1
            report.fail(
                f"分类 [{entity.title or entity.resource_path or entity.content_md5[:12]}]",
                str(exc)[:120],
            )
        counters["processed"] += 1
        total = counters["processed"]
        if total % 25 == 0:
            print_progress(total, total, f"分类成功 {counters['class_ok']}, 失败 {counters['failed']}")


_SENTINEL = None


async def _consume(queue: asyncio.Queue, cache, report, semaphore, counters):
    while True:
        item = await queue.get()
        if item is _SENTINEL:
            queue.task_done()
            break
        task_id, entity, llm_provider, llm_model = item
        await _process_one(
            task_id,
            entity,
            llm_provider,
            llm_model,
            cache,
            report,
            semaphore,
            counters,
        )
        queue.task_done()


async def _run_normal_mode(args, cache, report, llm_provider, llm_model):
    import threading

    from ResourceProcessor.preview_metadata import ProcessState

    counters = {"processed": 0, "class_ok": 0, "failed": 0}
    skipped_rebuild = 0
    skipped_no_description = 0
    semaphore = asyncio.Semaphore(args.concurrency)
    queue: asyncio.Queue = asyncio.Queue(maxsize=args.concurrency * 4)

    def _feeder():
        nonlocal skipped_rebuild, skipped_no_description
        total_seen = 0
        try:
            for task_id in cache.iter_tasks_by_state(
                ProcessState.DESCRIPTION_READY,
                limit=args.limit,
                resource_type=args.resource_type,
                source=args.source_filter,
            ):
                total_seen += 1
                entity = cache.rebuild_entity_from_cache(task_id)
                if entity is None:
                    skipped_rebuild += 1
                    continue
                if not (entity.description_main or entity.description_detail):
                    skipped_no_description += 1
                    continue
                fut = asyncio.run_coroutine_threadsafe(
                    queue.put((task_id, entity, llm_provider, llm_model)),
                    loop,
                )
                fut.result()

                if total_seen % 1000 == 0:
                    print_progress(
                        counters["processed"],
                        total_seen,
                        f"无描述跳过 {skipped_no_description}, 重建失败 {skipped_rebuild}, 队列 {queue.qsize()}",
                    )
        finally:
            for _ in range(args.concurrency):
                fut = asyncio.run_coroutine_threadsafe(queue.put(_SENTINEL), loop)
                fut.result()

    loop = asyncio.get_running_loop()
    feeder_thread = threading.Thread(target=_feeder, daemon=True)
    feeder_thread.start()

    consumers = [
        asyncio.create_task(_consume(queue, cache, report, semaphore, counters))
        for _ in range(args.concurrency)
    ]
    await asyncio.gather(*consumers)
    feeder_thread.join()

    report.ok(
        "分类生成",
        (
            f"处理 {counters['processed']}, 跳过(无描述) {skipped_no_description}, "
            f"跳过(重建失败) {skipped_rebuild}, 失败 {counters['failed']}"
        ),
    )


def main() -> int:
    parser = make_arg_parser(
        "根据已生成描述生成用途分类并写入 SQLite",
        extra_args=[
            ("--llm-provider", {"default": None, "help": "分类 LLM provider 名称 (默认 CLASSIFICATION_LLM_PROVIDER env 或 CLIENT_LLM_PROVIDER env 或 mock)"}),
            ("--retry-failed", {"action": "store_true", "help": "重试分类生成失败的任务"}),
            ("--max-retries", {"type": int, "default": 3, "help": "最大重试次数 (默认 3)"}),
            ("--concurrency", {"type": int, "default": None, "help": "并发请求数 (默认 CLASSIFICATION_CONCURRENCY env 或 5)"}),
        ],
    )
    args = parser.parse_args()

    from ResourceProcessor.cache.local_cache import LocalCacheStore

    db_path = os.path.abspath(args.db_path)
    cache = LocalCacheStore(db_path)

    llm_provider = (
        args.llm_provider
        or env("CLASSIFICATION_LLM_PROVIDER", "")
        or env("CLIENT_LLM_PROVIDER", "mock")
    )
    llm_model = env("CLASSIFICATION_LLM_MODEL", "")
    if args.concurrency is None:
        args.concurrency = int(env("CLASSIFICATION_CONCURRENCY", "5"))

    report = Report(label="分类生成")
    print("=" * 60)
    print("  分类生成 (classify_resources)")
    print("  数据源:         DB-only")
    print(f"  数据库:         {db_path}")
    print(f"  LLM Provider:   {llm_provider}")
    if llm_model:
        print(f"  LLM Model:      {llm_model}")
    print(f"  并发数:         {args.concurrency}")
    if args.limit:
        print(f"  限制:           {args.limit}")
    print("=" * 60)

    state_counts = cache.count_tasks_by_state()
    report.ok("当前状态统计", ", ".join(f"{k}={v}" for k, v in state_counts.items()) or "(空)")

    if args.retry_failed:
        candidates = _get_retry_candidates(cache, args.max_retries)
        report.ok("重试模式", f"找到 {len(candidates)} 个可重试的分类失败任务")

        tasks_to_run = []
        for task in candidates:
            task_id = task["id"]
            cache.increment_retry(task_id)
            entity = cache.rebuild_entity_from_cache(task_id)
            if entity is not None:
                tasks_to_run.append((task_id, entity, llm_provider, llm_model))

        counters = {"processed": 0, "class_ok": 0, "failed": 0}

        async def _run_retry():
            semaphore = asyncio.Semaphore(args.concurrency)
            await asyncio.gather(*[
                _process_one(tid, ent, prov, mdl, cache, report, semaphore, counters)
                for tid, ent, prov, mdl in tasks_to_run
            ])

        asyncio.run(_run_retry())
        report.ok("重试完成", f"处理 {counters['processed']}, 成功 {counters['class_ok']}, 失败 {counters['failed']}")
    else:
        asyncio.run(_run_normal_mode(args, cache, report, llm_provider, llm_model))

    report.ok("最终状态统计", ", ".join(f"{k}={v}" for k, v in cache.count_tasks_by_state().items()))

    cache.close()
    ok = report.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
