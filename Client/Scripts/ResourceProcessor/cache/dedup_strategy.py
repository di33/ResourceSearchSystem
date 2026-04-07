"""Deduplication, reuse, and incremental rerun strategy.

Uses content_md5 to detect duplicate resources, decides whether to fully
reuse cached results, partially regenerate (description / embedding), or
resume from an interrupted state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ResourceProcessor.cache.local_cache import LocalCacheStore
from ResourceProcessor.preview_metadata import ProcessState


class ReuseDecision(str, Enum):
    """Dedup / reuse decision outcome."""
    NEW = "new"
    REUSE_ALL = "reuse_all"
    RERUN_DESCRIPTION = "rerun_description"
    RERUN_EMBEDDING = "rerun_embedding"
    RESUME = "resume"


@dataclass
class ProcessingConfig:
    """Current processing configuration used to detect version drift."""
    prompt_version: str = "prompt_v1"
    embedding_model_version: str = "mock_embed_v1"
    preview_max_size: int = 512
    preview_format_priority: str = "webp"


@dataclass
class DedupResult:
    """Result of a dedup lookup."""
    decision: ReuseDecision
    existing_task_id: Optional[int] = None
    reason: str = ""


_COMPLETED_STATES = frozenset({
    ProcessState.SYNCED,
    ProcessState.COMMITTED,
    ProcessState.UPLOADED,
})

_RESUMABLE_STATES = frozenset({
    ProcessState.DISCOVERED,
    ProcessState.PREVIEW_READY,
    ProcessState.PREVIEW_FAILED,
    ProcessState.DESCRIPTION_READY,
    ProcessState.DESCRIPTION_FAILED,
    ProcessState.EMBEDDING_READY,
    ProcessState.EMBEDDING_FAILED,
    ProcessState.PACKAGE_READY,
    ProcessState.REGISTERED,
})


def check_dedup(
    cache: LocalCacheStore,
    content_md5: str,
    config: ProcessingConfig,
) -> DedupResult:
    """Check whether an existing result can be reused for *content_md5*.

    Decision logic
    1. No prior task with same md5 → NEW
    2. Latest task completed & config unchanged → REUSE_ALL
    3. Latest task completed but prompt_version drifted → RERUN_DESCRIPTION
    4. Latest task completed but embedding_model_version drifted → RERUN_EMBEDDING
    5. Latest task in intermediate / failed state → RESUME
    6. Unrecognised state → NEW (safe fallback)
    """
    tasks = cache.get_tasks_by_md5(content_md5)
    if not tasks:
        return DedupResult(ReuseDecision.NEW, reason="未找到已有记录")

    latest = max(tasks, key=lambda t: t["id"])
    task_id: int = latest["id"]
    state = ProcessState(latest["process_state"])

    if state in _COMPLETED_STATES:
        desc = cache.get_description_by_task(task_id)
        embed = cache.get_embedding_by_task(task_id)

        prompt_changed = bool(
            desc and desc.get("prompt_version", "") != config.prompt_version
        )
        embed_changed = bool(
            embed and embed.get("model_version", "") != config.embedding_model_version
        )

        if prompt_changed:
            return DedupResult(
                ReuseDecision.RERUN_DESCRIPTION,
                existing_task_id=task_id,
                reason="prompt_version 已变更",
            )
        if embed_changed:
            return DedupResult(
                ReuseDecision.RERUN_EMBEDDING,
                existing_task_id=task_id,
                reason="embedding_model_version 已变更",
            )
        return DedupResult(
            ReuseDecision.REUSE_ALL,
            existing_task_id=task_id,
            reason="资源已完成且配置一致",
        )

    if state in _RESUMABLE_STATES:
        return DedupResult(
            ReuseDecision.RESUME,
            existing_task_id=task_id,
            reason=f"任务处于 {state.value} 状态，可从断点继续",
        )

    return DedupResult(
        ReuseDecision.NEW,
        reason=f"未知状态 {state.value}，创建新任务",
    )


def get_resumable_tasks(cache: LocalCacheStore) -> list[dict]:
    """Return all tasks in an intermediate or failed state."""
    resumable: list[dict] = []
    for state in (
        ProcessState.DISCOVERED,
        ProcessState.PREVIEW_READY,
        ProcessState.PREVIEW_FAILED,
        ProcessState.DESCRIPTION_READY,
        ProcessState.DESCRIPTION_FAILED,
        ProcessState.EMBEDDING_READY,
        ProcessState.EMBEDDING_FAILED,
        ProcessState.PACKAGE_READY,
    ):
        resumable.extend(cache.get_tasks_by_state(state))
    return resumable


def get_retry_candidates(cache: LocalCacheStore, max_retries: int = 3) -> list[dict]:
    """Return failed tasks whose retry_count has not yet reached *max_retries*."""
    failed = cache.get_failed_tasks()
    return [t for t in failed if t.get("retry_count", 0) < max_retries]
