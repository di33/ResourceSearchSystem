"""Real implementation of BaseSearchClient backed by Milvus + PostgreSQL."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from pymilvus import CollectionSchema, DataType, FieldSchema, MilvusClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from CloudService.search_client import (
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
from app.services.ks3_storage import KS3Storage

logger = logging.getLogger(__name__)


def ensure_collection(milvus: MilvusClient) -> None:
    """Create the Milvus collection if it does not exist."""
    name = settings.milvus_collection
    if milvus.has_collection(name):
        return

    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=True)
    schema.add_field(field_name="id", datatype=DataType.INT64, is_primary=True)
    schema.add_field(field_name="resource_id", datatype=DataType.VARCHAR, max_length=64)
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


async def _embed_query(text: str) -> List[float]:
    """Vectorise a search query using the configured embedding provider.

    Falls back to a zero vector when the provider SDK is not installed,
    which is useful for local development without API keys.
    """
    try:
        from ResourceProcessor.embedding.embedding_generator import (
            generate_embedding_with_retry,
        )

        result, error = await generate_embedding_with_retry(
            text,
            provider_name=settings.embedding_provider,
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
        )
        if result:
            return result.vector_data
    except Exception as exc:
        logger.warning("Embedding generation failed, using zero vector: %s", exc)

    return [0.0] * settings.embedding_dimension


class MilvusSearchClient(BaseSearchClient):
    """Search client that queries Milvus for ANN and PostgreSQL for metadata."""

    def __init__(self, milvus: MilvusClient, session: AsyncSession, storage: KS3Storage):
        self.milvus = milvus
        self.session = session
        self.storage = storage

    async def search(self, request: SearchRequest) -> SearchResponse:
        query_vector = await _embed_query(request.query_text)

        search_filter = ""
        if request.resource_type:
            search_filter = f'resource_type == "{request.resource_type}"'

        hits = self.milvus.search(
            collection_name=settings.milvus_collection,
            data=[query_vector],
            limit=request.top_k,
            output_fields=["resource_id", "resource_type"],
            filter=search_filter or "",
            search_params={"metric_type": "COSINE", "params": {"nprobe": 16}},
        )

        results: list[SearchResultItem] = []
        for hit_group in hits:
            for hit in hit_group:
                score = hit.get("distance", 0.0)
                if score < request.similarity_threshold:
                    continue

                rid = hit["entity"].get("resource_id", "")
                rtype = hit["entity"].get("resource_type", "")

                task = (
                    await self.session.execute(
                        select(ResourceTask).where(ResourceTask.resource_id == rid)
                    )
                ).scalar_one_or_none()

                desc_summary = ""
                file_count = 0
                file_formats = []
                file_size_total = 0
                if task:
                    desc = (
                        await self.session.execute(
                            select(ResourceDescription)
                            .where(ResourceDescription.task_id == task.id)
                            .order_by(ResourceDescription.id.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if desc:
                        desc_summary = desc.main_content

                    files = (
                        await self.session.execute(
                            select(ResourceFile).where(ResourceFile.task_id == task.id)
                        )
                    ).scalars().all()
                    file_count = len(files)
                    for f in files:
                        file_formats.append(f.file_format)
                        file_size_total += f.file_size

                preview_urls = []
                other_preview_urls: list[str] = []
                primary_preview_url = ""
                if task:
                    preview_recs = (
                        await self.session.execute(
                            select(ResourcePreview)
                            .where(ResourcePreview.task_id == task.id)
                            .order_by(ResourcePreview.id)
                        )
                    ).scalars().all()
                    for pr in preview_recs:
                        if pr.path:
                            preview_urls.append(
                                self.storage.generate_presigned_download_url(f"previews/{rid}/{pr.path.split('/')[-1]}")
                            )

                if not preview_urls:
                    preview_prefix = f"previews/{rid}/"
                    preview_keys = self.storage.list_keys(preview_prefix, max_keys=20)
                    for key in preview_keys:
                        if key.endswith("/"):
                            continue
                        preview_urls.append(self.storage.generate_presigned_download_url(key))
                # Keep preview URL order stable while removing duplicates.
                preview_urls = list(dict.fromkeys(preview_urls))
                primary_preview_url = preview_urls[0] if preview_urls else ""
                other_preview_urls = preview_urls[1:] if len(preview_urls) > 1 else []

                file_download_url = ""
                if task:
                    ordered_files = (
                        await self.session.execute(
                            select(ResourceFile)
                            .where(ResourceFile.task_id == task.id)
                            .order_by(ResourceFile.is_primary.desc(), ResourceFile.id)
                        )
                    ).scalars().all()
                    if ordered_files:
                        primary_file = ordered_files[0]
                        file_key = primary_file.ks3_key or f"files/{rid}/{primary_file.file_name}"
                        file_download_url = self.storage.generate_presigned_download_url(file_key)

                primary_format = file_formats[0] if file_formats else (task.source_format if task else "")

                results.append(SearchResultItem(
                    resource_id=rid,
                    resource_type=rtype,
                    score=score,
                    primary_preview_url=primary_preview_url,
                    other_preview_urls=other_preview_urls,
                    file_download_url=file_download_url,
                    description_summary=desc_summary,
                    file_format=primary_format,
                    file_size=file_size_total,
                    status=task.process_state if task else "",
                    preview_available=bool(preview_urls),
                    file_count=file_count,
                ))

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

        if files:
            primary_file = files[0]
            key = primary_file.ks3_key or f"files/{request.resource_id}/{primary_file.file_name}"
            file_name = primary_file.file_name
            file_size = primary_file.file_size
        else:
            # Fallback to old schema
            key = f"files/{request.resource_id}/{task.source_name}" if hasattr(task, 'source_name') else f"files/{request.resource_id}/"
            file_name = getattr(task, 'source_name', 'resource')
            file_size = getattr(task, 'source_size', 0)

        download_url = self.storage.generate_presigned_download_url(key, request.expire_seconds)

        expires_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        return DownloadLinkResponse(
            download_url=download_url,
            expires_at=expires_at,
            file_name=file_name,
            file_size=file_size,
            content_type="application/octet-stream",
        )
