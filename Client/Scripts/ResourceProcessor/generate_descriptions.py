"""Description generation step of the split pipeline.

Usage:
    python -m ResourceProcessor.generate_descriptions \
        --crawler-output K:\\ResourceCrawler\\output \
        --db-path pipeline.db --limit 100
"""

from __future__ import annotations

import asyncio
import os
import random
import sys

from ResourceProcessor.pipeline_common import (
    Report,
    env,
    make_arg_parser,
    print_progress,
    state_ge,
    state_lt,
)


# ---------------------------------------------------------------------------
# LLM provider registration (must happen before importing generate_resource_description)
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


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


async def _generate_with_retry(
    cache,
    task_id: int,
    entity,
    llm_provider: str,
    report: Report,
    max_attempts: int = 6,
    base_delay_seconds: float = 3.0,
    success_delay_seconds: float = 0.35,
) -> bool:
    """Generate description with rate-limit retry. Returns True on success."""
    from ResourceProcessor.crawler.resource_adapter import build_description_input
    from ResourceProcessor.description.description_generator import generate_resource_description

    desc_input = build_description_input(entity)
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = await generate_resource_description(desc_input, provider_name=llm_provider)
            cache.insert_description(
                task_id,
                main_content=result.main_content,
                detail_content=result.detail_content,
                full_description=result.full_description,
                prompt_version=result.prompt_version,
                quality_score=result.description_quality_score,
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
    raise RuntimeError("description generation failed without exception")


def _get_retry_candidates(cache, max_retries: int) -> list[dict]:
    """Return DESCRIPTION_FAILED tasks with retry_count < max_retries."""
    from ResourceProcessor.preview_metadata import ProcessState
    rows = cache.get_failed_tasks()
    # Filter to DESCRIPTION_FAILED only
    desc_failed = [r for r in rows if r["process_state"] == ProcessState.DESCRIPTION_FAILED.value]
    return [r for r in desc_failed if r["retry_count"] < max_retries]


def main() -> int:
    parser = make_arg_parser(
        "生成资源描述并写入 SQLite",
        extra_args=[
            ("--llm-provider", {"default": None, "help": "LLM provider 名称 (默认 CLIENT_LLM_PROVIDER env 或 mock)"}),
            ("--retry-failed", {"action": "store_true", "help": "重试描述生成失败的任务"}),
            ("--max-retries", {"type": int, "default": 3, "help": "最大重试次数 (默认 3)"}),
        ],
    )
    args = parser.parse_args()

    from ResourceProcessor.cache.local_cache import LocalCacheStore
    from ResourceProcessor.crawler.catalog_loader import load_crawler_catalog
    from ResourceProcessor.crawler.resource_adapter import build_processing_entity
    from ResourceProcessor.preview_metadata import ProcessState

    db_path = os.path.abspath(args.db_path)
    cache = LocalCacheStore(db_path)

    crawler_output = os.path.abspath(args.crawler_output)
    if not os.path.isdir(crawler_output):
        print(f"错误：crawler output 目录不存在: {crawler_output}", file=sys.stderr)
        return 1

    llm_provider = args.llm_provider or env("CLIENT_LLM_PROVIDER", "mock")

    report = Report(label="描述生成")
    print("=" * 60)
    print("  描述生成 (generate_descriptions)")
    print(f"  Crawler Output: {crawler_output}")
    print(f"  数据库:         {db_path}")
    print(f"  LLM Provider:   {llm_provider}")
    if args.limit:
        print(f"  限制:           {args.limit}")
    print("=" * 60)

    catalog = load_crawler_catalog(crawler_output, db_path=db_path)

    state_counts = cache.count_tasks_by_state()
    report.ok("当前状态统计", ", ".join(f"{k}={v}" for k, v in state_counts.items()) or "(空)")

    processed = 0
    skipped_preview = 0
    skipped_already = 0
    desc_ok = 0
    failed = 0

    # --retry-failed mode: re-process failed tasks from DB
    if args.retry_failed:
        candidates = _get_retry_candidates(cache, args.max_retries)
        report.ok("重试模式", f"找到 {len(candidates)} 个可重试的失败任务")
        for task in candidates:
            task_id = task["id"]
            cache.increment_retry(task_id)
            entity = cache.rebuild_entity_from_cache(task_id)
            if entity is None:
                continue
            if entity.resource_type == "audio_file":
                continue
            try:
                asyncio.run(_generate_with_retry(cache, task_id, entity, llm_provider, report))
                cache.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
                desc_ok += 1
            except Exception as exc:
                cache.update_task_state(
                    task_id, ProcessState.DESCRIPTION_FAILED,
                    error_code="desc_error",
                    error_message=str(exc)[:500],
                )
                failed += 1
                report.fail(
                    f"描述 [{entity.title or entity.resource_path or entity.content_md5[:12]}]",
                    str(exc)[:120],
                )
            processed += 1
            if processed % 25 == 0:
                print_progress(processed, processed, f"描述成功 {desc_ok}, 失败 {failed}")
        report.ok("重试完成", f"处理 {processed}, 成功 {desc_ok}, 失败 {failed}")
    else:
        # Normal mode: iterate catalog
        for record in catalog.iter_resources(
            limit=args.limit,
            resource_type=args.resource_type,
            source_filter=args.source_filter,
        ):
            # --resume 快速跳过：用 source_resource_id 查状态，避免 MD5 计算
            if args.resume and record.id:
                existing_state = cache.get_task_state_by_source_id(record.id)
                if existing_state and state_ge(existing_state, ProcessState.DESCRIPTION_READY.value):
                    skipped_already += 1
                    total = processed + skipped_preview + skipped_already
                    if total % 1000 == 0:
                        print_progress(processed, total, f"成功 {desc_ok}, 失败 {failed}, 跳过 {skipped_already}, 音频跳过 {skipped_preview}")
                    continue

            entity = build_processing_entity(record)
            task_id, is_existing = cache.upsert_task(entity)

            if is_existing:
                task = cache.get_task_by_id(task_id)
                current_state = ProcessState(task["process_state"]) if task else ProcessState.DISCOVERED
            else:
                current_state = ProcessState.DISCOVERED

            # Skip if preview not ready
            if state_lt(current_state.value, ProcessState.PREVIEW_READY.value):
                skipped_preview += 1
                continue

            # Skip if already done (unless resume=False means always re-run... but resume means skip done)
            if args.resume and state_ge(current_state.value, ProcessState.DESCRIPTION_READY.value):
                skipped_already += 1
                continue

            # Skip audio files (current LLM models don't support audio input)
            if entity.resource_type == "audio_file":
                skipped_preview += 1
                continue

            # Rebuild entity with previews from cache
            cached_entity = cache.rebuild_entity_from_cache(task_id)
            if cached_entity is not None:
                entity = cached_entity

            try:
                asyncio.run(_generate_with_retry(cache, task_id, entity, llm_provider, report))
                cache.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
                desc_ok += 1
            except Exception as exc:
                failed += 1
                cache.update_task_state(
                    task_id, ProcessState.DESCRIPTION_FAILED,
                    error_code="desc_error",
                    error_message=str(exc)[:500],
                )
                report.fail(
                    f"描述 [{entity.title or entity.resource_path or entity.content_md5[:12]}]",
                    str(exc)[:120],
                )

            processed += 1
            if processed % 25 == 0:
                print_progress(
                    processed, processed + skipped_preview + skipped_already,
                    f"描述成功 {desc_ok}, 失败 {failed}",
                )

        report.ok("描述生成", f"处理 {processed}, 跳过(无预览) {skipped_preview}, 跳过(已完成) {skipped_already}, 失败 {failed}")

    report.ok("最终状态统计", ", ".join(f"{k}={v}" for k, v in cache.count_tasks_by_state().items()))

    cache.close()
    ok = report.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
