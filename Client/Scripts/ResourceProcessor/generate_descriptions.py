"""Description generation step of the split pipeline.

Usage:
    python -m ResourceProcessor.generate_descriptions \
        --db-path pipeline.db --limit 100
"""

from __future__ import annotations

import asyncio
import os
import random

import requests

from ResourceProcessor.pipeline_common import (
    Report,
    env,
    make_arg_parser,
    print_progress,
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
try:
    import ResourceProcessor.description.codex_exec_provider  # noqa: F401
except Exception:
    pass


_CODEX_PROVIDERS = {"codex", "codex-exec"}


def _is_codex_provider(provider: str) -> bool:
    return (provider or "").strip().lower() in _CODEX_PROVIDERS


def _select_audio_provider(
    llm_provider: str,
    audio_llm_provider: str,
    audio_llm_model: str,
) -> tuple[str, str] | None:
    """Pick an audio provider while keeping Codex image-only."""
    if audio_llm_provider:
        return audio_llm_provider, audio_llm_model
    if audio_llm_model and not _is_codex_provider(llm_provider):
        return llm_provider, audio_llm_model
    return None


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


_NON_RETRYABLE_ERROR_MARKERS = (
    "code=400",
    "code=413",
    "bad request",
    "payload too large",
    "validation errors",
    "multimodal data is corrupted",
)

_TRANSIENT_ERROR_MARKERS = (
    "read timed out",
    "connect timed out",
    "connection timed out",
    "write operation timed out",
    "connection aborted",
    "connection reset",
    "remote end closed connection",
    "max retries exceeded",
    "temporarily unavailable",
    "ssleoferror",
    "sslerror",
)


def _is_non_retryable_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _NON_RETRYABLE_ERROR_MARKERS)


def _is_transient_error(exc: Exception) -> bool:
    if _is_non_retryable_error(exc):
        return False
    if isinstance(
        exc,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.SSLError,
        ),
    ):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)


def _is_retryable_error(exc: Exception) -> bool:
    return _is_rate_limit_error(exc) or _is_transient_error(exc)


def _retry_reason(exc: Exception) -> str:
    if _is_rate_limit_error(exc):
        return "限流"
    return "临时网络错误"


async def _generate_with_retry(
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
    """Generate description with rate-limit retry. Returns True on success."""
    from ResourceProcessor.crawler.resource_adapter import build_description_input
    from ResourceProcessor.description.description_generator import generate_resource_description_text

    desc_input = build_description_input(entity)
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = await generate_resource_description_text(
                desc_input, provider_name=llm_provider, model=llm_model,
            )
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
            if attempt >= max_attempts or not _is_retryable_error(exc):
                break
            delay = base_delay_seconds * (2 ** (attempt - 1)) + random.uniform(0.0, 1.0)
            report.ok(
                f"{_retry_reason(exc)}退避 [{entity.title or entity.resource_path or entity.content_md5[:12]}]",
                f"第 {attempt} 次失败，等待 {delay:.1f}s: {str(exc)[:100]}",
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


def _has_existing_description(cache, task_id: int) -> bool:
    desc = cache.get_description_by_task(task_id)
    if not desc:
        return False
    return bool(
        str(desc.get("full_description") or "").strip()
        or str(desc.get("main_content") or "").strip()
        or str(desc.get("detail_content") or "").strip()
    )


async def _process_one(
    task_id, entity, desc_provider, desc_model,
    cache, report, semaphore, counters,
):
    """Process a single task with concurrency control."""
    from ResourceProcessor.preview_metadata import ProcessState

    async with semaphore:
        had_existing_description = _has_existing_description(cache, task_id)
        try:
            await _generate_with_retry(
                cache, task_id, entity, desc_provider, report, llm_model=desc_model,
            )
            cache.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
            counters["desc_ok"] += 1
        except Exception as exc:
            label = entity.title or entity.resource_path or entity.content_md5[:12]
            if had_existing_description:
                cache.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
                counters["fallback_existing"] = counters.get("fallback_existing", 0) + 1
                report.ok(
                    f"描述保留 [{label}]",
                    f"新生成失败，沿用已有描述: {str(exc)[:120]}",
                )
            else:
                cache.update_task_state(
                    task_id, ProcessState.DESCRIPTION_FAILED,
                    error_code="desc_error",
                    error_message=str(exc)[:500],
                )
                counters["failed"] += 1
                report.fail(
                    f"描述 [{label}]",
                    str(exc)[:120],
                )
        counters["processed"] += 1
        total = counters["processed"]
        if total % 25 == 0:
            print_progress(total, total, f"描述成功 {counters['desc_ok']}, 失败 {counters['failed']}")


_SENTINEL = None  # Signals consumer to stop


async def _consume(queue: asyncio.Queue, cache, report, semaphore, counters):
    """Pull tasks from queue and process them."""
    while True:
        item = await queue.get()
        if item is _SENTINEL:
            queue.task_done()
            break
        task_id, entity, desc_provider, desc_model = item
        await _process_one(
            task_id, entity, desc_provider, desc_model,
            cache, report, semaphore, counters,
        )
        queue.task_done()


async def _run_normal_mode(
    args, cache, report,
    llm_provider, audio_llm_provider, audio_llm_model,
):
    """Normal mode: stream preview-ready tasks from SQLite into workers."""
    import threading

    from ResourceProcessor.preview_metadata import ProcessState

    counters = {"processed": 0, "desc_ok": 0, "failed": 0}
    skipped_audio = 0
    skipped_rebuild = 0
    skipped_existing = 0
    semaphore = asyncio.Semaphore(args.concurrency)
    queue: asyncio.Queue = asyncio.Queue(maxsize=args.concurrency * 4)

    def _feeder():
        """Synchronous DB iteration — runs in a background thread."""
        nonlocal skipped_audio, skipped_rebuild, skipped_existing
        total_seen = 0
        try:
            for task_id in cache.iter_tasks_by_state(
                ProcessState.PREVIEW_READY,
                limit=args.limit,
                resource_type=args.resource_type,
                source=args.source_filter,
            ):
                total_seen += 1
                if args.resume and _has_existing_description(cache, task_id):
                    cache.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
                    skipped_existing += 1
                    continue

                entity = cache.rebuild_entity_from_cache(task_id)
                if entity is None:
                    skipped_rebuild += 1
                    continue

                # Audio files: keep using audio-capable API-key providers.
                is_audio = entity.resource_type == "audio_file"
                if is_audio:
                    selected = _select_audio_provider(
                        llm_provider, audio_llm_provider, audio_llm_model,
                    )
                    if selected is None:
                        skipped_audio += 1
                        continue
                    desc_provider, desc_model = selected
                else:
                    desc_provider, desc_model = llm_provider, ""

                # Put into queue — use run_coroutine_threadsafe + put() to block
                # when full (put_nowait raises QueueFull inside event loop, which
                # cannot be caught by the feeder thread).
                fut = asyncio.run_coroutine_threadsafe(
                    queue.put((task_id, entity, desc_provider, desc_model)), loop,
                )
                fut.result()  # block until item is enqueued

                if total_seen % 1000 == 0:
                    qsize = queue.qsize()
                    print_progress(
                        counters["processed"], total_seen,
                        f"已有描述跳过 {skipped_existing}, 音频跳过 {skipped_audio}, 重建失败 {skipped_rebuild}, 队列 {qsize}",
                    )
        finally:
            # Signal all consumers to stop — send one sentinel per consumer.
            # queue.put() blocks until there's room, so no retry needed.
            for _ in range(args.concurrency):
                fut = asyncio.run_coroutine_threadsafe(queue.put(_SENTINEL), loop)
                fut.result()

    loop = asyncio.get_running_loop()
    feeder_thread = threading.Thread(target=_feeder, daemon=True)
    feeder_thread.start()

    # Start consumer coroutines
    consumers = [
        asyncio.create_task(_consume(queue, cache, report, semaphore, counters))
        for _ in range(args.concurrency)
    ]
    await asyncio.gather(*consumers)
    feeder_thread.join()

    report.ok(
        "描述生成",
        f"处理 {counters['processed']}, 跳过(已有描述) {skipped_existing}, "
        f"跳过(音频) {skipped_audio}, 跳过(重建失败) {skipped_rebuild}, "
        f"沿用已有描述 {counters.get('fallback_existing', 0)}, 失败 {counters['failed']}",
    )


def main() -> int:
    parser = make_arg_parser(
        "生成资源描述文本并写入 SQLite",
        extra_args=[
            ("--llm-provider", {"default": None, "help": "LLM provider 名称 (默认 CLIENT_LLM_PROVIDER env 或 mock)"}),
            ("--audio-llm-provider", {"default": None, "help": "音频资源 LLM provider (默认 AUDIO_LLM_PROVIDER env，未设置则跳过音频)"}),
            ("--retry-failed", {"action": "store_true", "help": "重试描述生成失败的任务"}),
            ("--max-retries", {"type": int, "default": 3, "help": "最大重试次数 (默认 3)"}),
            ("--concurrency", {"type": int, "default": None, "help": "并发请求数 (默认 API=5，Codex=1)"}),
        ],
    )
    args = parser.parse_args()

    from ResourceProcessor.cache.local_cache import LocalCacheStore

    db_path = os.path.abspath(args.db_path)
    cache = LocalCacheStore(db_path)

    llm_provider = args.llm_provider or env("CLIENT_LLM_PROVIDER", "mock")
    audio_llm_provider = args.audio_llm_provider or env("AUDIO_LLM_PROVIDER", "")
    audio_llm_model = env("AUDIO_LLM_MODEL", "")
    if args.concurrency is None:
        args.concurrency = int(
            env("CODEX_CONCURRENCY", "1")
            if _is_codex_provider(llm_provider)
            else env("DESCRIPTION_CONCURRENCY", "5")
        )

    report = Report(label="描述生成")
    print("=" * 60)
    print("  描述生成 (generate_descriptions)")
    print("  输出状态:       description_ready")
    print("  数据源:         DB-only")
    print(f"  数据库:         {db_path}")
    print(f"  LLM Provider:   {llm_provider}")
    if audio_llm_provider:
        print(f"  音频 Provider:  {audio_llm_provider}")
    elif audio_llm_model:
        if _is_codex_provider(llm_provider):
            print("  音频:           跳过 (主 provider 为 Codex；请配置 AUDIO_LLM_PROVIDER)")
        else:
            print(f"  音频模型:       {audio_llm_model} (使用主 provider {llm_provider})")
    else:
        print(f"  音频:           跳过 (未配置 AUDIO_LLM_PROVIDER 或 AUDIO_LLM_MODEL)")
    print(f"  并发数:         {args.concurrency}")
    if args.limit:
        print(f"  限制:           {args.limit}")
    print("=" * 60)

    state_counts = cache.count_tasks_by_state()
    report.ok("当前状态统计", ", ".join(f"{k}={v}" for k, v in state_counts.items()) or "(空)")

    # --retry-failed mode: re-process failed tasks from DB
    if args.retry_failed:
        candidates = _get_retry_candidates(cache, args.max_retries)
        report.ok("重试模式", f"找到 {len(candidates)} 个可重试的失败任务")

        tasks_to_run = []
        skipped_existing = 0
        for task in candidates:
            task_id = task["id"]
            if args.resume and _has_existing_description(cache, task_id):
                cache.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
                skipped_existing += 1
                continue
            cache.increment_retry(task_id)
            entity = cache.rebuild_entity_from_cache(task_id)
            if entity is None:
                continue
            is_audio = entity.resource_type == "audio_file"
            if is_audio:
                selected = _select_audio_provider(
                    llm_provider, audio_llm_provider, audio_llm_model,
                )
                if selected is None:
                    continue
                else:
                    desc_provider, desc_model = selected
            else:
                desc_provider, desc_model = llm_provider, ""
            tasks_to_run.append((task_id, entity, desc_provider, desc_model))

        counters = {"processed": 0, "desc_ok": 0, "failed": 0}

        async def _run_retry():
            semaphore = asyncio.Semaphore(args.concurrency)
            await asyncio.gather(*[
                _process_one(tid, ent, prov, mdl, cache, report, semaphore, counters)
                for tid, ent, prov, mdl in tasks_to_run
            ])

        asyncio.run(_run_retry())
        report.ok(
            "重试完成",
            f"处理 {counters['processed']}, 成功 {counters['desc_ok']}, "
            f"跳过(已有描述) {skipped_existing}, "
            f"沿用已有描述 {counters.get('fallback_existing', 0)}, 失败 {counters['failed']}",
        )
    else:
        # Normal mode: producer-consumer pattern — iterate and process concurrently
        # so that LLM calls start as soon as the first task is found, not after
        # rebuilding all eligible DB records.
        asyncio.run(_run_normal_mode(
            args, cache, report,
            llm_provider, audio_llm_provider, audio_llm_model,
        ))

    report.ok("最终状态统计", ", ".join(f"{k}={v}" for k, v in cache.count_tasks_by_state().items()))

    cache.close()
    ok = report.summary()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
