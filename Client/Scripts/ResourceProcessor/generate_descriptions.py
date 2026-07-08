"""Client DB description refresh step of the split pipeline.

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
)
from resource_contracts.resource_types import (
    AUDIO_FILE_RESOURCE_TYPE,
    is_search_indexable_resource_type,
)


# ---------------------------------------------------------------------------
# LLM provider registration (must happen before Tools description generation)
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
    "code=502",
    "code=503",
    "code=504",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "cloud elb",
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


def _should_generate_description(resource_type: str, *, force_resource_type: str = "") -> bool:
    return is_search_indexable_resource_type(resource_type)


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
    from ResourceProcessor.description.pack_description_builder import build_description_input_for_generation
    from ResourceProcessor.description.description_generator import generate_resource_description_text

    desc_input = await build_description_input_for_generation(cache, task_id, entity)
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


def _description_progress_label(
    counters: dict,
    *,
    skipped_existing: int = 0,
    skipped_audio: int = 0,
    skipped_rebuild: int = 0,
    skipped_non_indexable: int = 0,
) -> str:
    """Format progress for a streaming queue whose final total is not known yet."""
    skipped_existing = skipped_existing or counters.get("skipped_existing", 0)
    skipped_audio = skipped_audio or counters.get("skipped_audio", 0)
    skipped_rebuild = skipped_rebuild or counters.get("skipped_rebuild", 0)
    skipped_non_indexable = skipped_non_indexable or counters.get("skipped_non_indexable", 0)
    queue = counters.get("queue")
    try:
        queue_size = queue.qsize() if queue is not None else 0
    except Exception:
        queue_size = 0
    scan_state = "扫描完成" if counters.get("feeder_done") else "扫描中"
    return (
        f"已处理 {counters.get('processed', 0)}, "
        f"已入队 {counters.get('enqueued', 0)}, {scan_state}, 队列 {queue_size}; "
        f"描述成功 {counters.get('desc_ok', 0)}, 失败 {counters.get('failed', 0)}, "
        f"已有描述跳过 {skipped_existing}, 音频跳过 {skipped_audio}, "
        f"非索引资源跳过 {skipped_non_indexable}, 重建失败 {skipped_rebuild}"
    )


def _run_async_cli(coro, report: Report) -> bool:
    try:
        asyncio.run(coro)
        return True
    except KeyboardInterrupt:
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        report.fail(
            "用户中断",
            "收到 Ctrl+C，已停止描述生成；可重新运行命令继续增量处理。",
        )
        return False


async def _process_one(
    task_id, entity, desc_provider, desc_model,
    cache, report, semaphore, counters,
):
    """Process a single task with concurrency control."""
    from ResourceProcessor.preview_metadata import ProcessState

    async with semaphore:
        success_state = ProcessState.DESCRIPTION_READY
        had_existing_description = _has_existing_description(cache, task_id)
        try:
            await _generate_with_retry(
                cache, task_id, entity, desc_provider, report, llm_model=desc_model,
            )
            cache.update_task_state(task_id, success_state)
            counters["desc_ok"] += 1
        except Exception as exc:
            from ResourceProcessor.description.pack_description_builder import PackChildDescriptionsNotReadyError

            label = entity.title or entity.resource_path or entity.content_md5[:12]
            if had_existing_description and not isinstance(exc, PackChildDescriptionsNotReadyError):
                cache.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
                counters["fallback_existing"] = counters.get("fallback_existing", 0) + 1
                report.ok(
                    f"描述保留 [{label}]",
                    f"新生成失败，沿用已有描述: {str(exc)[:120]}",
                )
            else:
                cache.update_task_state(
                    task_id,
                    ProcessState.DESCRIPTION_FAILED,
                    error_code="desc_error",
                    error_message=str(exc)[:500],
                )
                counters["failed"] += 1
                report.fail(
                    f"描述 [{label}]",
                    str(exc)[:120],
                )
        counters["processed"] += 1
        if counters["processed"] % 25 == 0:
            print(f"    进度: {_description_progress_label(counters)}")


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

    counters = {
        "processed": 0,
        "desc_ok": 0,
        "failed": 0,
        "enqueued": 0,
        "feeder_done": False,
        "skipped_existing": 0,
        "skipped_audio": 0,
        "skipped_rebuild": 0,
        "skipped_non_indexable": 0,
    }
    skipped_audio = 0
    skipped_rebuild = 0
    skipped_existing = 0
    skipped_non_indexable = 0
    feeder_exception: Exception | None = None
    semaphore = asyncio.Semaphore(args.concurrency)
    queue: asyncio.Queue = asyncio.Queue(maxsize=args.concurrency * 4)

    def _feeder():
        """Synchronous DB iteration — runs in a background thread."""
        nonlocal skipped_audio, skipped_rebuild, skipped_existing, skipped_non_indexable, feeder_exception
        total_seen = 0
        try:
            task_ids = (
                cache.iter_tasks(
                    limit=args.limit,
                    resource_type=args.resource_type,
                    source=args.source_filter,
                )
                if args.force
                else cache.iter_tasks_by_state(
                    ProcessState.PREVIEW_READY,
                    limit=args.limit,
                    resource_type=args.resource_type,
                    source=args.source_filter,
                )
            )
            for task_id in task_ids:
                total_seen += 1
                if args.resume and not args.force and _has_existing_description(cache, task_id):
                    cache.update_task_state(task_id, ProcessState.DESCRIPTION_READY)
                    skipped_existing += 1
                    counters["skipped_existing"] = skipped_existing
                    continue

                entity = cache.rebuild_entity_from_cache(task_id)
                if entity is None:
                    skipped_rebuild += 1
                    counters["skipped_rebuild"] = skipped_rebuild
                    continue
                if not _should_generate_description(entity.resource_type, force_resource_type=args.resource_type if args.force else ""):
                    skipped_non_indexable += 1
                    counters["skipped_non_indexable"] = skipped_non_indexable
                    continue

                # Audio files: keep using audio-capable API-key providers.
                is_audio = entity.resource_type == AUDIO_FILE_RESOURCE_TYPE
                if is_audio:
                    selected = _select_audio_provider(
                        llm_provider, audio_llm_provider, audio_llm_model,
                    )
                    if selected is None:
                        skipped_audio += 1
                        counters["skipped_audio"] = skipped_audio
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
                counters["enqueued"] += 1

                if total_seen % 1000 == 0:
                    qsize = queue.qsize()
                    print(
                        "    进度: "
                        f"已扫描 {total_seen}, 已入队 {counters['enqueued']}, "
                        f"已处理 {counters['processed']}, 队列 {qsize}; "
                        f"已有描述跳过 {skipped_existing}, 音频跳过 {skipped_audio}, "
                        f"非索引资源跳过 {skipped_non_indexable}, "
                        f"重建失败 {skipped_rebuild}"
                    )
        except Exception as exc:
            feeder_exception = exc
        finally:
            counters["feeder_done"] = True
            # Signal all consumers to stop — send one sentinel per consumer.
            # queue.put() blocks until there's room, so no retry needed.
            for _ in range(args.concurrency):
                fut = asyncio.run_coroutine_threadsafe(queue.put(_SENTINEL), loop)
                fut.result()

    loop = asyncio.get_running_loop()
    counters["queue"] = queue
    feeder_thread = threading.Thread(target=_feeder, daemon=True)
    feeder_thread.start()

    # Start consumer coroutines
    consumers = [
        asyncio.create_task(_consume(queue, cache, report, semaphore, counters))
        for _ in range(args.concurrency)
    ]
    await asyncio.gather(*consumers)
    feeder_thread.join()

    if feeder_exception is not None:
        report.fail(
            "描述扫描线程",
            f"{type(feeder_exception).__name__}: {str(feeder_exception)[:500]}",
        )
        return

    report.ok(
        "描述生成",
        f"处理 {counters['processed']}, 跳过(已有描述) {skipped_existing}, "
        f"跳过(音频) {skipped_audio}, 跳过(重建失败) {skipped_rebuild}, "
        f"跳过(非索引资源) {skipped_non_indexable}, "
        f"沿用已有描述 {counters.get('fallback_existing', 0)}, 失败 {counters['failed']}",
    )


def main() -> int:
    parser = make_arg_parser(
        "从客户端 pipeline.db 读取待处理资源，生成描述并写回 SQLite",
        extra_args=[
            ("--llm-provider", {"default": None, "help": "LLM provider 名称 (默认 CLIENT_LLM_PROVIDER env 或 mock)"}),
            ("--audio-llm-provider", {"default": None, "help": "音频资源 LLM provider (默认 AUDIO_LLM_PROVIDER env，未设置则跳过音频)"}),
            ("--retry-failed", {"action": "store_true", "help": "重试描述生成失败的任务"}),
            ("--force", {"action": "store_true", "help": "强制刷新匹配资源的描述；必须配合 --resource-type 使用，旧描述不删除，新描述作为最新记录写入"}),
            ("--max-retries", {"type": int, "default": 3, "help": "最大重试次数 (默认 3)"}),
            ("--concurrency", {"type": int, "default": None, "help": "并发请求数 (默认 API=5，Codex=1)"}),
        ],
    )
    args = parser.parse_args()

    if args.force and not args.resource_type:
        parser.error("--force 必须配合 --resource-type 使用，避免误刷新所有资源类型")
    if args.force and args.retry_failed:
        parser.error("--force 不能和 --retry-failed 同时使用")

    from ResourceProcessor.cache.local_cache import LocalCacheStore
    from ResourceProcessor.preview_metadata import ProcessState

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

    report = Report(label="客户端描述刷新")
    print("=" * 60)
    print("  客户端描述刷新 (generate_descriptions)")
    print("  输出状态:       description_ready")
    print("  数据源:         pipeline.db")
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
    if args.force:
        print(f"  强制刷新:       {args.resource_type}")
    print("=" * 60)

    state_counts = cache.count_tasks_by_state()
    report.ok("当前状态统计", ", ".join(f"{k}={v}" for k, v in state_counts.items()) or "(空)")

    interrupted = False
    try:
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
                if not _should_generate_description(entity.resource_type, force_resource_type=args.resource_type if args.force else ""):
                    continue
                is_audio = entity.resource_type == AUDIO_FILE_RESOURCE_TYPE
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

            counters = {
                "processed": 0,
                "desc_ok": 0,
                "failed": 0,
                "enqueued": len(tasks_to_run),
                "feeder_done": True,
            }

            async def _run_retry():
                semaphore = asyncio.Semaphore(args.concurrency)
                await asyncio.gather(*[
                    _process_one(tid, ent, prov, mdl, cache, report, semaphore, counters)
                    for tid, ent, prov, mdl in tasks_to_run
                ])

            interrupted = not _run_async_cli(_run_retry(), report)
            if not interrupted:
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
            interrupted = not _run_async_cli(
                _run_normal_mode(
                    args, cache, report,
                    llm_provider, audio_llm_provider, audio_llm_model,
                ),
                report,
            )

        status_label = "中断时状态统计" if interrupted else "最终状态统计"
        report.ok(status_label, ", ".join(f"{k}={v}" for k, v in cache.count_tasks_by_state().items()))
    finally:
        cache.close()

    ok = report.summary()
    if interrupted:
        return 130
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
