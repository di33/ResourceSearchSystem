"""Build pack description inputs from child resource descriptions."""

from __future__ import annotations

import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import Awaitable, Callable, Iterable

from ResourceProcessor.crawler.resource_adapter import build_description_input
from ResourceProcessor.description.description_generator import DescriptionInput
from ResourceProcessor.description.embedding_client import generate_embeddings, get_model_version
from ResourceProcessor.preview_metadata import ResourceProcessingEntity
from resource_contracts.resource_types import PACK_RESOURCE_TYPE

Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]


class PackChildDescriptionsNotReadyError(ValueError):
    """Raised when a pack cannot be described because child descriptions are missing."""


@dataclass(frozen=True)
class ChildDescriptionRecord:
    task_id: int
    source_resource_id: str
    resource_type: str
    title: str
    resource_path: str
    main_content: str
    detail_content: str
    quality_score: float | None = None

    @classmethod
    def from_row(cls, row: dict) -> "ChildDescriptionRecord":
        quality = row.get("quality_score")
        return cls(
            task_id=int(row.get("task_id") or row.get("id") or 0),
            source_resource_id=str(row.get("source_resource_id") or ""),
            resource_type=str(row.get("resource_type") or ""),
            title=str(row.get("title") or ""),
            resource_path=str(row.get("resource_path") or ""),
            main_content=str(row.get("main_content") or "").strip(),
            detail_content=str(row.get("detail_content") or "").strip(),
            quality_score=float(quality) if quality is not None else None,
        )

    @property
    def text(self) -> str:
        return "\n".join(
            part for part in (self.main_content, self.detail_content) if part
        ).strip()


@dataclass(frozen=True)
class ChildDescriptionCluster:
    resource_type: str
    representative: ChildDescriptionRecord
    diverse_samples: list[ChildDescriptionRecord]
    title_examples: list[str]
    members: list[ChildDescriptionRecord]

    @property
    def count(self) -> int:
        return len(self.members)


@dataclass(frozen=True)
class ResourceTypeClusterSummary:
    resource_type: str
    child_count: int
    cluster_count: int
    selected_clusters: list[ChildDescriptionCluster]
    omitted_cluster_count: int
    omitted_child_count: int


@dataclass(frozen=True)
class PackDescriptionSummary:
    child_description_count: int
    embedding_input_count: int
    semantic_cluster_count: int
    selected_clusters: list[ChildDescriptionCluster]
    type_summaries: list[ResourceTypeClusterSummary]
    omitted_cluster_count: int
    omitted_child_count: int
    embedding_model: str
    embedding_seconds: float = 0.0
    clustering_seconds: float = 0.0


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_flag(name: str, default: str = "") -> bool:
    value = os.environ.get(name, default).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _timing_enabled() -> bool:
    return _env_flag("PACK_DESCRIPTION_TIMING_LOG", "1")


def _pack_label(entity: ResourceProcessingEntity, task_id: int) -> str:
    label = entity.title or entity.pack_name or entity.source_resource_id or str(task_id)
    text = " ".join(str(label).split())
    if len(text) <= 80:
        return text
    return text[:77].rstrip() + "..."


def _log_pack_timing(entity: ResourceProcessingEntity, task_id: int, fields: dict[str, object]) -> None:
    if not _timing_enabled():
        return
    parts = [f"task={task_id}", f'label="{_pack_label(entity, task_id)}"']
    for key, value in fields.items():
        if isinstance(value, float):
            parts.append(f"{key}={value:.3f}s")
        else:
            parts.append(f"{key}={value}")
    print("  [PACK_DESC] " + " ".join(parts), file=sys.stderr)


def _required_child_description_count(entity: ResourceProcessingEntity) -> int:
    fallback = _env_int("PACK_DESCRIPTION_MIN_CHILD_DESCRIPTIONS", 1)
    if not _env_flag("PACK_DESCRIPTION_REQUIRE_ALL_CHILD_DESCRIPTIONS", "1"):
        return fallback
    declared = entity.child_resource_count or len(entity.child_resource_ids or [])
    return declared if declared > 0 else fallback


def _choose_representative(records: list[ChildDescriptionRecord]) -> ChildDescriptionRecord:
    return max(
        records,
        key=lambda item: (
            item.quality_score if item.quality_score is not None else -1.0,
            len(item.text),
            -item.task_id,
        ),
    )


def _record_key(record: ChildDescriptionRecord) -> tuple[int, str]:
    return (record.task_id, record.source_resource_id)


def _record_label(record: ChildDescriptionRecord) -> str:
    return record.title or record.resource_path or record.source_resource_id or f"task-{record.task_id}"


def _title_examples(records: list[ChildDescriptionRecord]) -> list[str]:
    limit = _env_int("PACK_DESCRIPTION_TITLE_EXAMPLES", 6)
    examples: list[str] = []
    seen: set[str] = set()
    for record in sorted(records, key=lambda item: item.task_id):
        label = _record_label(record)
        normalized = " ".join(label.split()).lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        examples.append(label)
        if len(examples) >= limit:
            break
    return examples


def _mean_vector(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    length = len(vectors[0])
    sums = [0.0] * length
    for vector in vectors:
        for index, value in enumerate(vector[:length]):
            sums[index] += float(value)
    return [value / len(vectors) for value in sums]


def _build_cluster(
    records: list[ChildDescriptionRecord],
    vectors_by_key: dict[tuple[int, str], list[float]],
) -> ChildDescriptionCluster:
    available = [
        (record, vectors_by_key[_record_key(record)])
        for record in records
        if _record_key(record) in vectors_by_key
    ]
    if not available:
        representative = _choose_representative(records)
        diverse_samples: list[ChildDescriptionRecord] = []
    else:
        centroid = _mean_vector([vector for _record, vector in available])
        similarity_by_key = {
            _record_key(record): _cosine(vector, centroid)
            for record, vector in available
        }
        representative = max(
            records,
            key=lambda record: (
                similarity_by_key.get(_record_key(record), -1.0),
                record.quality_score if record.quality_score is not None else -1.0,
                len(record.text),
                -record.task_id,
            ),
        )
        extra_count = _env_int("PACK_DESCRIPTION_CLUSTER_EXTRA_SAMPLES", 2)
        candidates = [record for record in records if record != representative]
        diverse_samples = sorted(
            candidates,
            key=lambda record: (
                similarity_by_key.get(_record_key(record), 1.0),
                -(record.quality_score if record.quality_score is not None else -1.0),
                -len(record.text),
                record.task_id,
            ),
        )[:extra_count]

    return ChildDescriptionCluster(
        resource_type=representative.resource_type or "unknown",
        representative=representative,
        diverse_samples=diverse_samples,
        title_examples=_title_examples(records),
        members=records,
    )


def _cluster_with_numpy(
    records: list[ChildDescriptionRecord],
    vectors: list[list[float]],
    threshold: float,
) -> list[list[ChildDescriptionRecord]]:
    try:
        import numpy as np
    except Exception:
        return _cluster_without_numpy(records, vectors, threshold)

    matrix = np.asarray(vectors, dtype="float32")
    if matrix.ndim != 2 or matrix.shape[0] != len(records):
        raise ValueError("Embedding vector shape does not match child descriptions")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix = matrix / norms

    assigned = np.zeros(len(records), dtype=bool)
    clusters: list[list[ChildDescriptionRecord]] = []
    for index, record in enumerate(records):
        if assigned[index]:
            continue
        similarities = matrix @ matrix[index]
        member_indexes = np.where((similarities >= threshold) & (~assigned))[0]
        if member_indexes.size == 0:
            member_indexes = np.asarray([index])
        assigned[member_indexes] = True
        members = [records[int(member_index)] for member_index in member_indexes.tolist()]
        clusters.append(members)
    return clusters


def _cosine(left: list[float], right: list[float]) -> float:
    total = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        total += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return total / ((left_norm ** 0.5) * (right_norm ** 0.5))


def _cluster_without_numpy(
    records: list[ChildDescriptionRecord],
    vectors: list[list[float]],
    threshold: float,
) -> list[list[ChildDescriptionRecord]]:
    assigned: set[int] = set()
    clusters: list[list[ChildDescriptionRecord]] = []
    for index, record in enumerate(records):
        if index in assigned:
            continue
        members = [record]
        assigned.add(index)
        for candidate_index in range(index + 1, len(records)):
            if candidate_index in assigned:
                continue
            if _cosine(vectors[index], vectors[candidate_index]) >= threshold:
                assigned.add(candidate_index)
                members.append(records[candidate_index])
        clusters.append(members)
    return clusters


def _type_key(record: ChildDescriptionRecord) -> str:
    return record.resource_type or "unknown"


def _select_type_clusters(
    clusters_by_type: dict[str, list[ChildDescriptionCluster]],
    child_count_by_type: Counter[str],
) -> tuple[list[ResourceTypeClusterSummary], list[ChildDescriptionCluster]]:
    max_examples = _env_int("PACK_DESCRIPTION_MAX_CHILD_EXAMPLES", 30)
    min_per_type = _env_int("PACK_DESCRIPTION_MIN_CLUSTERS_PER_TYPE", 2)
    type_order = sorted(
        clusters_by_type,
        key=lambda resource_type: (-child_count_by_type[resource_type], resource_type),
    )

    selected_by_type: dict[str, list[ChildDescriptionCluster]] = {
        resource_type: [] for resource_type in type_order
    }
    selected_ids: set[int] = set()
    remaining = max_examples

    for resource_type in type_order:
        if remaining <= 0:
            break
        clusters = clusters_by_type[resource_type]
        take = min(min_per_type, len(clusters), remaining)
        for cluster in clusters[:take]:
            selected_by_type[resource_type].append(cluster)
            selected_ids.add(id(cluster))
        remaining -= take

    if remaining > 0:
        all_candidates = [
            cluster
            for clusters in clusters_by_type.values()
            for cluster in clusters
            if id(cluster) not in selected_ids
        ]
        all_candidates.sort(
            key=lambda cluster: (
                -cluster.count,
                -child_count_by_type[cluster.resource_type],
                cluster.resource_type,
                cluster.representative.task_id,
            )
        )
        for cluster in all_candidates[:remaining]:
            selected_by_type[cluster.resource_type].append(cluster)
            selected_ids.add(id(cluster))

    type_summaries: list[ResourceTypeClusterSummary] = []
    selected_clusters: list[ChildDescriptionCluster] = []
    for resource_type in type_order:
        clusters = clusters_by_type[resource_type]
        selected = selected_by_type[resource_type]
        selected.sort(key=lambda cluster: (-cluster.count, cluster.representative.task_id))
        omitted = [cluster for cluster in clusters if id(cluster) not in selected_ids]
        type_summaries.append(
            ResourceTypeClusterSummary(
                resource_type=resource_type,
                child_count=child_count_by_type[resource_type],
                cluster_count=len(clusters),
                selected_clusters=selected,
                omitted_cluster_count=len(omitted),
                omitted_child_count=sum(cluster.count for cluster in omitted),
            )
        )
        selected_clusters.extend(selected)

    selected_clusters.sort(
        key=lambda cluster: (
            -child_count_by_type[cluster.resource_type],
            cluster.resource_type,
            -cluster.count,
            cluster.representative.task_id,
        )
    )
    return type_summaries, selected_clusters


async def summarize_child_descriptions(
    rows: Iterable[dict],
    *,
    embedder: Embedder | None = None,
) -> PackDescriptionSummary:
    records = [ChildDescriptionRecord.from_row(row) for row in rows]
    records = [record for record in records if record.text]
    if not records:
        return PackDescriptionSummary(
            child_description_count=0,
            embedding_input_count=0,
            semantic_cluster_count=0,
            selected_clusters=[],
            type_summaries=[],
            omitted_cluster_count=0,
            omitted_child_count=0,
            embedding_model="",
        )

    max_embedding_items = _env_int("PACK_DESCRIPTION_MAX_EMBEDDING_ITEMS", 5000)
    embedding_records = records[:max_embedding_items]
    threshold = _env_float("PACK_DESCRIPTION_SIMILARITY_THRESHOLD", 0.92)
    embed = embedder or generate_embeddings
    embedding_started = time.perf_counter()
    vectors = await embed([record.text for record in embedding_records])
    embedding_seconds = time.perf_counter() - embedding_started
    clustering_started = time.perf_counter()
    vectors_by_key = {
        _record_key(record): vector
        for record, vector in zip(embedding_records, vectors)
    }
    child_count_by_type: Counter[str] = Counter(_type_key(record) for record in records)
    embedding_records_by_type: dict[str, list[ChildDescriptionRecord]] = {}
    for record in embedding_records:
        embedding_records_by_type.setdefault(_type_key(record), []).append(record)

    clusters_by_type: dict[str, list[ChildDescriptionCluster]] = {
        resource_type: [] for resource_type in child_count_by_type
    }
    for resource_type, bucket_records in embedding_records_by_type.items():
        bucket_vectors = [vectors_by_key[_record_key(record)] for record in bucket_records]
        member_groups = _cluster_with_numpy(bucket_records, bucket_vectors, threshold)
        clusters_by_type[resource_type].extend(
            _build_cluster(group, vectors_by_key)
            for group in member_groups
        )

    if len(records) > len(embedding_records):
        for record in records[len(embedding_records):]:
            clusters_by_type.setdefault(_type_key(record), []).append(
                _build_cluster([record], vectors_by_key)
            )

    for clusters in clusters_by_type.values():
        clusters.sort(key=lambda cluster: (-cluster.count, cluster.representative.task_id))

    type_summaries, selected = _select_type_clusters(clusters_by_type, child_count_by_type)
    clustering_seconds = time.perf_counter() - clustering_started
    semantic_cluster_count = sum(len(clusters) for clusters in clusters_by_type.values())
    omitted_cluster_count = sum(summary.omitted_cluster_count for summary in type_summaries)
    omitted_child_count = sum(summary.omitted_child_count for summary in type_summaries)

    return PackDescriptionSummary(
        child_description_count=len(records),
        embedding_input_count=len(embedding_records),
        semantic_cluster_count=semantic_cluster_count,
        selected_clusters=selected,
        type_summaries=type_summaries,
        omitted_cluster_count=omitted_cluster_count,
        omitted_child_count=omitted_child_count,
        embedding_model=get_model_version(),
        embedding_seconds=embedding_seconds,
        clustering_seconds=clustering_seconds,
    )


def _compact(value: str, max_chars: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 1].rstrip() + "…"


def _format_counter(counter: Counter[str], limit: int = 12) -> str:
    parts = [
        f"{key or 'unknown'} {count}"
        for key, count in counter.most_common(limit)
    ]
    return ", ".join(parts)


def _format_sample(record: ChildDescriptionRecord, *, prefix: str) -> list[str]:
    label = _compact(_record_label(record), 120)
    return [
        f"   {prefix}: {label}",
        f"     主体: {_compact(record.main_content, 180)}",
        f"     细节: {_compact(record.detail_content, 220)}",
    ]


def build_pack_prompt_context(
    entity: ResourceProcessingEntity,
    summary: PackDescriptionSummary,
) -> str:
    declared_children = entity.child_resource_count or entity.member_count
    lines = [
        f"资源类型: {PACK_RESOURCE_TYPE}",
    ]
    if entity.title:
        lines.append(f"资源标题: {entity.title}")
    if entity.pack_name:
        lines.append(f"资源包: {entity.pack_name}")
    if entity.source:
        lines.append(f"来源站点: {entity.source}")
    if entity.category:
        lines.append(f"来源分类: {entity.category}")
    if entity.tags:
        lines.append(f"来源标签: {', '.join(entity.tags[:20])}")
    if entity.source_description:
        lines.append(f"来源描述: {_compact(entity.source_description, 500)}")
    if declared_children:
        lines.append(f"声明子资源数: {declared_children}")
    if entity.contains_resource_types:
        lines.append(f"包含资源类型: {', '.join(entity.contains_resource_types)}")
    lines.extend(
        [
            f"有描述的子资源数: {summary.child_description_count}",
            f"embedding输入描述数: {summary.embedding_input_count}",
            f"embedding语义聚类数: {summary.semantic_cluster_count}",
            f"embedding模型: {summary.embedding_model}",
        ]
    )
    if summary.omitted_cluster_count:
        lines.append(
            f"未展开语义组: {summary.omitted_cluster_count} 组，覆盖 {summary.omitted_child_count} 个子资源"
        )
    lines.append(
        "聚合说明: 子资源先按 resource_type 分段，再在各类型内通过 embedding 相似度聚类；"
        "每组展示覆盖数量、标题示例、中心样本和差异样本。请总结整个包，不要逐条拼接。"
    )
    lines.append("子资源类型概览:")
    for type_summary in summary.type_summaries:
        lines.append(
            f"- {type_summary.resource_type}: 子资源 {type_summary.child_count} 个，"
            f"语义组 {type_summary.cluster_count} 个，展示 {len(type_summary.selected_clusters)} 组，"
            f"未展开 {type_summary.omitted_cluster_count} 组/覆盖 {type_summary.omitted_child_count} 个子资源"
        )

    for type_summary in summary.type_summaries:
        if not type_summary.selected_clusters:
            continue
        lines.append("")
        lines.append(f"类型: {type_summary.resource_type}")
        lines.append(
            f"子资源数: {type_summary.child_count}；语义组数: {type_summary.cluster_count}；"
            f"当前展开: {len(type_summary.selected_clusters)} 组"
        )
        lines.append("代表语义组:")
        for index, cluster in enumerate(type_summary.selected_clusters, start=1):
            examples = "；".join(_compact(item, 80) for item in cluster.title_examples)
            lines.append(
                f"{index}. 覆盖 {cluster.count} 个相似子资源；标题示例: {examples}"
            )
            lines.extend(_format_sample(cluster.representative, prefix="中心样本"))
            if cluster.diverse_samples:
                lines.append("   差异样本:")
                for sample in cluster.diverse_samples:
                    label = _compact(_record_label(sample), 100)
                    main = _compact(sample.main_content, 140)
                    detail = _compact(sample.detail_content, 160)
                    lines.append(f"   - {label}: {main}；{detail}")

    return "\n".join(lines)


async def build_pack_description_input(
    cache,
    task_id: int,
    entity: ResourceProcessingEntity,
    *,
    embedder: Embedder | None = None,
) -> DescriptionInput | None:
    if entity.resource_type != PACK_RESOURCE_TYPE:
        return None

    total_started = time.perf_counter()
    query_started = time.perf_counter()
    rows = cache.get_pack_child_description_rows(
        task_id,
        pack_source_resource_id=entity.source_resource_id,
        child_source_ids=entity.child_resource_ids,
    )
    query_seconds = time.perf_counter() - query_started
    required_children = _required_child_description_count(entity)
    if len(rows) < required_children:
        _log_pack_timing(
            entity,
            task_id,
            {
                "status": "missing_child_descriptions",
                "required": required_children,
                "available": len(rows),
                "query": query_seconds,
                "total": time.perf_counter() - total_started,
            },
        )
        return None

    summary = await summarize_child_descriptions(rows, embedder=embedder)
    if summary.child_description_count < required_children:
        _log_pack_timing(
            entity,
            task_id,
            {
                "status": "empty_child_description_text",
                "required": required_children,
                "available": summary.child_description_count,
                "query": query_seconds,
                "total": time.perf_counter() - total_started,
            },
        )
        return None

    prompt_started = time.perf_counter()
    base_input = build_description_input(entity)
    context = build_pack_prompt_context(entity, summary)
    prompt_seconds = time.perf_counter() - prompt_started
    prompt_version_tag = os.environ.get(
        "PACK_DESCRIPTION_PROMPT_VERSION",
        "pack_child_embedding_summary_v1",
    ).strip()
    _log_pack_timing(
        entity,
        task_id,
        {
            "status": "ready",
            "children": summary.child_description_count,
            "embedding_inputs": summary.embedding_input_count,
            "types": len(summary.type_summaries),
            "clusters": summary.semantic_cluster_count,
            "selected": len(summary.selected_clusters),
            "query": query_seconds,
            "embedding": summary.embedding_seconds,
            "cluster": summary.clustering_seconds,
            "prompt": prompt_seconds,
            "total": time.perf_counter() - total_started,
        },
    )
    return replace(
        base_input,
        prompt_context_override=context,
        description_prompt_env="LLM_PACK_DESCRIPTION_PROMPT",
        prompt_version_tag=prompt_version_tag,
        attach_llm_media=False,
        llm_input_path="",
        llm_input_paths=[],
        llm_input_type="text",
    )


async def build_description_input_for_generation(
    cache,
    task_id: int,
    entity: ResourceProcessingEntity,
) -> DescriptionInput:
    pack_input = await build_pack_description_input(cache, task_id, entity)
    if pack_input is not None:
        return pack_input
    if entity.resource_type == PACK_RESOURCE_TYPE:
        raise PackChildDescriptionsNotReadyError(
            "pack child descriptions are not ready; generate child resource descriptions before pack description"
        )
    return build_description_input(entity)
