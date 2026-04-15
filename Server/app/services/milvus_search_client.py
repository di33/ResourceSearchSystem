"""Real implementation of BaseSearchClient backed by Milvus + PostgreSQL."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from pymilvus import CollectionSchema, DataType, FieldSchema, MilvusClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from CloudService.search_client import (
    CANONICAL_RESOURCE_TYPES,
    BaseSearchClient,
    DownloadLinkRequest,
    DownloadLinkResponse,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    SearchSuggestion,
)
from app.config import settings
from app.models.tables import ResourceDescription, ResourceFile, ResourcePreview, ResourceTask
from app.services.embedding_client import generate_embedding
from app.services.ks3_storage import KS3Storage

logger = logging.getLogger(__name__)


def _create_new_collection(milvus: MilvusClient) -> None:
    """Create a collection with the new schema (resource_id as PK)."""
    name = settings.milvus_collection
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=True)
    schema.add_field(field_name="resource_id", datatype=DataType.VARCHAR, max_length=64, is_primary=True)
    schema.add_field(field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=settings.embedding_dimension)
    schema.add_field(field_name="resource_type", datatype=DataType.VARCHAR, max_length=32)

    index_params = milvus.prepare_index_params()
    index_params.add_index(field_name="vector", index_type="IVF_FLAT", metric_type="COSINE", params={"nlist": 256})

    milvus.create_collection(
        collection_name=name,
        schema=schema,
        index_params=index_params,
    )
    logger.info("Created Milvus collection '%s' (dim=%d)", name, settings.embedding_dimension)


def ensure_collection(milvus: MilvusClient) -> None:
    """Create the Milvus collection if it does not exist."""
    name = settings.milvus_collection
    if milvus.has_collection(name):
        return
    _create_new_collection(milvus)


def get_collection_schema_new() -> bool:
    """Return True if the collection uses the new schema (resource_id PK)."""
    return _collection_schema_new is True


async def _embed_query(text: str) -> List[float]:
    """Vectorise a search query using the server-side embedding provider.

    Raises RuntimeError on failure — the caller should return an error to the
    client instead of silently falling back to a zero vector.
    """
    return await generate_embedding(text)


def _loads_json_list(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _normalize_resource_type(value: Optional[str]) -> str:
    if not value:
        return ""
    normalized = str(value).strip().lower()
    return normalized if normalized in CANONICAL_RESOURCE_TYPES else ""


def _normalize_format_filter(values: Optional[list[str]]) -> set[str]:
    if not values:
        return set()
    return {
        str(value).strip().lower().lstrip(".")
        for value in values
        if isinstance(value, str) and str(value).strip()
    }


class MilvusSearchClient(BaseSearchClient):
    """Search client that queries Milvus for ANN and PostgreSQL for metadata."""

    def __init__(self, milvus: MilvusClient, session: AsyncSession, storage: KS3Storage):
        self.milvus = milvus
        self.session = session
        self.storage = storage

    async def search(self, request: SearchRequest) -> SearchResponse:
        query_vector = await _embed_query(request.query_text)
        normalized_resource_type = _normalize_resource_type(request.resource_type)
        normalized_format_filter = _normalize_format_filter(request.format_filter)

        search_filter = ""
        if normalized_resource_type:
            search_filter = f'resource_type == "{normalized_resource_type}"'

        search_limit = request.top_k
        if normalized_format_filter:
            search_limit = max(request.top_k * 3, 30)

        hits = self.milvus.search(
            collection_name=settings.milvus_collection,
            data=[query_vector],
            limit=search_limit,
            output_fields=["resource_id", "resource_type"],
            filter=search_filter or "",
            search_params={"metric_type": "COSINE", "params": {"nprobe": 16}},
        )

        # --- Collect hits above threshold ---
        scored_hits: list[tuple[str, str, float]] = []
        for hit_group in hits:
            for hit in hit_group:
                score = hit.get("distance", 0.0)
                if score < request.similarity_threshold:
                    continue
                rid = hit["entity"].get("resource_id", "")
                rtype = hit["entity"].get("resource_type", "")
                scored_hits.append((rid, rtype, score))

        if not scored_hits:
            suggestion = SearchSuggestion(
                rewrite_queries=[f"{request.query_text} 高清", f"{request.query_text} 素材"],
                relaxable_filters=["resource_type", "format_filter"],
                suggested_threshold=max(0.1, request.similarity_threshold - 0.2),
                try_cross_type=True,
            )
            return SearchResponse(results=[], total_count=0, suggestion=suggestion)

        # --- Batch load all related data ---
        resource_ids = [h[0] for h in scored_hits]

        tasks_raw = (
            await self.session.execute(
                select(ResourceTask).where(ResourceTask.resource_id.in_(resource_ids))
            )
        ).scalars().all()
        task_by_rid = {t.resource_id: t for t in tasks_raw}
        task_ids = [t.id for t in tasks_raw]

        descs_by_task: dict[int, str] = {}
        if task_ids:
            for d in (
                await self.session.execute(
                    select(ResourceDescription).where(ResourceDescription.task_id.in_(task_ids))
                )
            ).scalars().all():
                descs_by_task[d.task_id] = d.main_content

        files_by_task: dict[int, list] = {}
        if task_ids:
            for f in (
                await self.session.execute(
                    select(ResourceFile).where(ResourceFile.task_id.in_(task_ids))
                )
            ).scalars().all():
                files_by_task.setdefault(f.task_id, []).append(f)

        previews_by_task: dict[int, list] = {}
        if task_ids:
            for p in (
                await self.session.execute(
                    select(ResourcePreview).where(ResourcePreview.task_id.in_(task_ids))
                    .order_by(ResourcePreview.id)
                )
            ).scalars().all():
                previews_by_task.setdefault(p.task_id, []).append(p)

        # --- Batch load parent tasks ---
        parent_rids = {t.parent_resource_id for t in tasks_raw if t.parent_resource_id}
        parent_tasks_by_rid: dict[str, ResourceTask] = {}
        parent_previews_by_task: dict[int, list] = {}
        if parent_rids:
            parent_tasks_raw = (
                await self.session.execute(
                    select(ResourceTask).where(ResourceTask.resource_id.in_(list(parent_rids)))
                )
            ).scalars().all()
            parent_tasks_by_rid = {t.resource_id: t for t in parent_tasks_raw}
            parent_task_ids = [t.id for t in parent_tasks_raw]
            if parent_task_ids:
                for pp in (
                    await self.session.execute(
                        select(ResourcePreview).where(ResourcePreview.task_id.in_(parent_task_ids))
                        .order_by(ResourcePreview.id)
                    )
                ).scalars().all():
                    parent_previews_by_task.setdefault(pp.task_id, []).append(pp)

        # --- Build results ---
        results: list[SearchResultItem] = []
        for rid, rtype, score in scored_hits:
            task = task_by_rid.get(rid)
            if task is None:
                continue

            files = files_by_task.get(task.id, [])
            file_formats = []
            file_size_total = 0
            for f in files:
                fmt = str(f.file_format or "").strip().lower().lstrip(".")
                if fmt:
                    file_formats.append(fmt)
                file_size_total += f.file_size

            if normalized_format_filter and not (set(file_formats) & normalized_format_filter):
                continue

            preview_urls = []
            for pr in previews_by_task.get(task.id, []):
                if pr.path:
                    preview_urls.append(
                        self.storage.generate_presigned_download_url(
                            f"previews/{rid}/{pr.path.split('/')[-1]}"
                        )
                    )
            preview_urls = list(dict.fromkeys(preview_urls))

            # Download URL
            file_download_url = ""
            if task.download_object_key:
                file_download_url = self.storage.generate_presigned_download_url(task.download_object_key)
            elif files:
                primary_file = files[0]
                file_key = primary_file.ks3_key or f"files/{rid}/{primary_file.file_name}"
                file_download_url = self.storage.generate_presigned_download_url(file_key)

            # Parent info
            parent_resource_id = task.parent_resource_id or ""
            parent_title = ""
            parent_preview_url = ""
            parent_download_url = ""
            if parent_resource_id:
                parent_task = parent_tasks_by_rid.get(parent_resource_id)
                if parent_task is not None:
                    parent_title = parent_task.title
                    if parent_task.download_object_key:
                        parent_download_url = self.storage.generate_presigned_download_url(
                            parent_task.download_object_key
                        )
                    parent_previews = parent_previews_by_task.get(parent_task.id, [])
                    if parent_previews and parent_previews[0].path:
                        parent_preview_url = self.storage.generate_presigned_download_url(
                            f"previews/{parent_resource_id}/{parent_previews[0].path.split('/')[-1]}"
                        )

            results.append(SearchResultItem(
                resource_id=rid,
                resource_type=rtype,
                score=score,
                primary_preview_url=preview_urls[0] if preview_urls else "",
                other_preview_urls=preview_urls[1:] if len(preview_urls) > 1 else [],
                file_download_url=file_download_url,
                description_summary=descs_by_task.get(task.id, ""),
                file_format=file_formats[0] if file_formats else "",
                file_size=file_size_total,
                status=task.process_state,
                preview_available=bool(preview_urls),
                file_count=len(files),
                title=task.title,
                source_resource_id=task.source_resource_id,
                parent_resource_id=parent_resource_id,
                parent_title=parent_title,
                parent_preview_url=parent_preview_url,
                parent_download_url=parent_download_url,
                child_resource_count=task.child_resource_count,
                contains_resource_types=_loads_json_list(task.contains_resource_types_json),
            ))
            if len(results) >= request.top_k:
                break

        suggestion = None
        if not results:
            suggestion = SearchSuggestion(
                rewrite_queries=[f"{request.query_text} 高清", f"{request.query_text} 素材"],
                relaxable_filters=["resource_type", "format_filter"],
                suggested_threshold=max(0.1, request.similarity_threshold - 0.2),
                try_cross_type=True,
            )

        return SearchResponse(
            results=results,
            total_count=len(results),
            suggestion=suggestion,
        )

    async def get_download_link(self, request: DownloadLinkRequest) -> DownloadLinkResponse:
        task = (
            await self.session.execute(
                select(ResourceTask).where(ResourceTask.resource_id == request.resource_id)
            )
        ).scalar_one_or_none()

        if task is None:
            return DownloadLinkResponse(
                download_url="", expires_at="", file_name="", file_size=0,
                content_type="",
                error_code="RESOURCE_NOT_FOUND",
                error_message="resource not found",
            )

        # Fetch all files for this resource
        files = (
            await self.session.execute(
                select(ResourceFile)
                .where(ResourceFile.task_id == task.id)
                .order_by(ResourceFile.is_primary.desc(), ResourceFile.id)
            )
        ).scalars().all()

        if task.download_object_key:
            key = task.download_object_key
            file_name = task.download_file_name or "resource"
            file_size = task.download_file_size
        elif files:
            primary_file = files[0]
            key = primary_file.ks3_key or f"files/{request.resource_id}/{primary_file.file_name}"
            file_name = primary_file.file_name
            file_size = primary_file.file_size
        else:
            key = f"files/{request.resource_id}/"
            file_name = task.title or "resource"
            file_size = 0

        download_url = self.storage.generate_presigned_download_url(key, request.expire_seconds)

        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=request.expire_seconds)).isoformat(
            timespec="seconds"
        )

        return DownloadLinkResponse(
            download_url=download_url,
            expires_at=expires_at,
            file_name=file_name,
            file_size=file_size,
            content_type=task.download_content_type or "application/octet-stream",
        )
