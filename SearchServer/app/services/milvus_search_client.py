"""Real implementation of BaseSearchClient backed by Milvus + PostgreSQL."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from pymilvus import DataType, MilvusClient
from sqlalchemy import select, text
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
from app.deps import get_reranker
from app.models.tables import ResourceDescription, ResourceFile, ResourcePreview, ResourceTask
from app.services.embedding_client import generate_embedding
from app.services.object_urls import ObjectUrlGenerator
from resource_contracts.resource_types import PACK_RESOURCE_TYPE, normalize_resource_type

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


async def _embed_query(text: str) -> List[float]:
    """Vectorise a search query using the server-side embedding provider.

    Raises RuntimeError on failure — the caller should return an error to the
    client instead of silently falling back to a zero vector.
    """
    return await generate_embedding(text)


def _normalize_resource_type(value: Optional[str]) -> str:
    return normalize_resource_type(value)


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

    def __init__(self, milvus: MilvusClient, session: AsyncSession, urls: ObjectUrlGenerator):
        self.milvus = milvus
        self.session = session
        self.urls = urls

    @staticmethod
    def _file_object_key(resource_id: str, file_record: ResourceFile) -> str:
        return file_record.object_key or f"files/{resource_id}/{file_record.file_name}"

    @staticmethod
    def _preview_object_key(resource_id: str, preview_record: ResourcePreview) -> str:
        if preview_record.object_key:
            return preview_record.object_key
        name = str(preview_record.path or "").split("/")[-1]
        return f"previews/{resource_id}/{name}" if name else ""

    def _download_url(self, key: str, storage_profile_id: str = "", expires: int | None = None) -> str:
        if not key:
            return ""
        return self.urls.generate_download_url(
            key,
            expires,
            storage_profile_id=storage_profile_id,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(self, request: SearchRequest) -> SearchResponse:
        normalized_resource_type = _normalize_resource_type(request.resource_type)
        normalized_format_filter = _normalize_format_filter(request.format_filter)

        if request.search_mode == "bm25":
            return await self._search_bm25_only(request, normalized_resource_type, normalized_format_filter)
        elif request.search_mode == "hybrid":
            return await self._search_hybrid(request, normalized_resource_type, normalized_format_filter)
        else:  # "vector" or fallback
            search_limit = request.top_k
            vector_hits = await self._vector_search(
                request.query_text, normalized_resource_type,
                request.similarity_threshold, search_limit,
            )
            if not vector_hits:
                return self._empty_response(request)
            fused = [(rid, rtype, score, 0.0, score, score) for rid, rtype, score in vector_hits]
            return await self._build_search_results(fused[:request.top_k], request, normalized_format_filter)

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

        if task.package_object_key:
            key = task.package_object_key
            profile_id = task.package_storage_profile_id
            file_name = task.package_object_key.rstrip("/").rsplit("/", 1)[-1] or task.title or "package.zip"
            file_size = 0
        elif task.source_object_key:
            key = task.source_object_key
            profile_id = task.source_storage_profile_id
            file_name = task.source_object_file_name or task.title or "resource"
            file_size = task.source_object_file_size
        elif files:
            primary_file = files[0]
            key = self._file_object_key(request.resource_id, primary_file)
            profile_id = primary_file.storage_profile_id
            file_name = primary_file.file_name
            file_size = primary_file.file_size
        else:
            key = f"files/{request.resource_id}/"
            profile_id = ""
            file_name = task.title or "resource"
            file_size = 0

        download_url = self._download_url(key, profile_id, request.expire_seconds)

        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=request.expire_seconds)).isoformat(
            timespec="seconds"
        )

        return DownloadLinkResponse(
            download_url=download_url,
            expires_at=expires_at,
            file_name=file_name,
            file_size=file_size,
            content_type="application/octet-stream",
        )

    # ------------------------------------------------------------------
    # Search mode dispatchers
    # ------------------------------------------------------------------

    async def _search_hybrid(
        self,
        request: SearchRequest,
        normalized_resource_type: str,
        normalized_format_filter: set[str],
    ) -> SearchResponse:
        """Hybrid search: vector + BM25 + RRF fusion."""
        search_limit = max(request.top_k * 3, 30)

        vector_hits, bm25_hits = await asyncio.gather(
            self._vector_search(
                request.query_text, normalized_resource_type,
                request.similarity_threshold, search_limit,
            ),
            self._bm25_search(
                request.query_text, normalized_resource_type, search_limit,
            ),
        )

        if not vector_hits and not bm25_hits:
            return self._empty_response(request)

        fused = self._rrf_fusion(vector_hits, bm25_hits, request.bm25_weight)
        fused = fused[:request.top_k]

        # --- Reranking (optional) ---
        enable = request.enable_reranker if request.enable_reranker is not None else settings.reranker_enabled
        rerank_score_map: dict[str, float] = {}
        if enable:
            try:
                fused, rerank_score_map = await self._apply_reranking(
                    request.query_text, fused, settings.reranker_weight,
                )
            except Exception:
                logger.warning("Reranking failed, falling back to RRF-only", exc_info=True)

        return await self._build_search_results(fused, request, normalized_format_filter, rerank_score_map)

    async def _search_bm25_only(
        self,
        request: SearchRequest,
        normalized_resource_type: str,
        normalized_format_filter: set[str],
    ) -> SearchResponse:
        """BM25-only search mode."""
        search_limit = max(request.top_k * 3, 30)

        bm25_hits = await self._bm25_search(
            request.query_text, normalized_resource_type, search_limit,
        )

        if not bm25_hits:
            return self._empty_response(request)

        fused = [(rid, rtype, 0.0, score, score, score) for rid, rtype, score in bm25_hits[:request.top_k]]
        return await self._build_search_results(fused, request, normalized_format_filter)

    # ------------------------------------------------------------------
    # Core search primitives
    # ------------------------------------------------------------------

    _TSQUERY_SPECIAL = re.compile(r"[!():*&|'\"`~<>=+\-]")

    @staticmethod
    def _sanitize_tsquery_input(text: str) -> str:
        """Strip tsquery operator characters so jieba can tokenise naturally."""
        text = MilvusSearchClient._TSQUERY_SPECIAL.sub(" ", text)
        return " ".join(text.split()).strip()

    async def _vector_search(
        self,
        query_text: str,
        normalized_resource_type: str,
        similarity_threshold: float,
        limit: int,
    ) -> list[tuple[str, str, float]]:
        """Vector search via Milvus. Returns list of (resource_id, resource_type, score)."""
        query_vector = await _embed_query(query_text)

        search_filter = ""
        if normalized_resource_type:
            search_filter = f'resource_type == "{normalized_resource_type}"'

        hits = self.milvus.search(
            collection_name=settings.milvus_collection,
            data=[query_vector],
            limit=limit,
            output_fields=["resource_id", "resource_type"],
            filter=search_filter or "",
            search_params={"metric_type": "COSINE", "params": {"nprobe": 16}},
        )

        scored_hits: list[tuple[str, str, float]] = []
        for hit_group in hits:
            for hit in hit_group:
                score = hit.get("distance", 0.0)
                if score < similarity_threshold:
                    continue
                rid = hit["entity"].get("resource_id", "")
                rtype = hit["entity"].get("resource_type", "")
                scored_hits.append((rid, rtype, score))

        return scored_hits

    async def _bm25_search(
        self,
        query_text: str,
        normalized_resource_type: str,
        limit: int,
    ) -> list[tuple[str, str, float]]:
        """BM25 full-text search via resource_description + pg_jieba.

        Returns list of (resource_id, resource_type, bm25_score).
        Returns empty list if the underlying database does not support FTS.

        Uses OR semantics between terms for better recall (e.g. "卡通小人"
        matches resources containing "卡通" OR "小人", ranked by relevance).
        """
        # Sanitize input: strip tsquery operator characters
        query_text = self._sanitize_tsquery_input(query_text)
        if not query_text:
            return []

        # CTE computes the tsquery once; reused in both ts_rank_cd and WHERE.
        # websearch_to_tsquery produces AND; we convert to OR for better recall.
        # (plainto_tsquery was replaced because it emits empty lexemes for spaces,
        #  which caused the OR query to match ~87% of all documents.)
        sql = f"""
            WITH ts AS (
                SELECT to_tsquery(:text_config,
                    replace(websearch_to_tsquery(:text_config, :query_text)::text, '&', '|')
                ) AS q
            )
            SELECT rt.resource_id, rt.resource_type,
                   ts_rank_cd(rd.search_vector, ts.q, 32) AS rank
            FROM resource_description rd
            JOIN resource_task rt ON rd.task_id = rt.id
            CROSS JOIN ts
            WHERE rd.search_vector @@ ts.q
              AND rt.process_state = 'committed'
              {"AND rt.resource_type = :resource_type" if normalized_resource_type else ""}
            ORDER BY rank DESC
            LIMIT :limit
        """
        params: dict = {
            "text_config": settings.search_text_config,
            "query_text": query_text,
            "limit": limit,
        }
        if normalized_resource_type:
            params["resource_type"] = normalized_resource_type

        try:
            result = await self.session.execute(text(sql), params)
            rows = result.fetchall()
        except Exception:
            logger.warning("BM25 search failed, returning empty results", exc_info=True)
            return []

        hits: list[tuple[str, str, float]] = []
        for row in rows:
            rid, rtype, rank = row[0], row[1], float(row[2])
            if rank > 0:
                hits.append((rid, rtype, rank))

        return hits

    # ------------------------------------------------------------------
    # RRF Fusion
    # ------------------------------------------------------------------

    def _rrf_fusion(
        self,
        vector_hits: list[tuple[str, str, float]],
        bm25_hits: list[tuple[str, str, float]],
        bm25_weight: float,
        k: int = 60,
        penalty_rank: int = 100,
    ) -> list[tuple[str, str, float, float, float, float]]:
        """Reciprocal Rank Fusion of vector and BM25 results.

        Returns list of (resource_id, resource_type, vector_score, bm25_score, rrf_score, final_score)
        sorted by final_score desc.
        """
        vector_weight = 1.0 - bm25_weight

        vector_rank_map: dict[str, tuple[int, float]] = {}
        for rank, (rid, rtype, score) in enumerate(vector_hits, start=1):
            vector_rank_map[rid] = (rank, score)

        bm25_rank_map: dict[str, tuple[int, float]] = {}
        for rank, (rid, rtype, score) in enumerate(bm25_hits, start=1):
            bm25_rank_map[rid] = (rank, score)

        all_rids = set(vector_rank_map.keys()) | set(bm25_rank_map.keys())

        rtype_map: dict[str, str] = {}
        for rid, rtype, _ in vector_hits:
            rtype_map[rid] = rtype
        for rid, rtype, _ in bm25_hits:
            rtype_map[rid] = rtype

        fused: list[tuple[str, str, float, float, float, float]] = []
        for rid in all_rids:
            v_rank, v_score = vector_rank_map.get(rid, (penalty_rank, 0.0))
            b_rank, b_score = bm25_rank_map.get(rid, (penalty_rank, 0.0))

            rrf_vector = 1.0 / (k + v_rank)
            rrf_bm25 = 1.0 / (k + b_rank)
            final_score = vector_weight * rrf_vector + bm25_weight * rrf_bm25

            fused.append((rid, rtype_map.get(rid, ""), v_score, b_score, rrf_vector + rrf_bm25, final_score))

        fused.sort(key=lambda x: x[5], reverse=True)
        return fused

    # ------------------------------------------------------------------
    # Reranking
    # ------------------------------------------------------------------

    async def _fetch_rerank_texts(self, resource_ids: list[str]) -> dict[str, str]:
        """Fetch lightweight text for reranking: title + main_content.

        Returns {resource_id: text} for each candidate.
        Falls back to title when no description exists.
        """
        if not resource_ids:
            return {}

        rows = (
            await self.session.execute(
                select(
                    ResourceTask.resource_id,
                    ResourceTask.title,
                    ResourceDescription.main_content,
                )
                .outerjoin(ResourceDescription, ResourceDescription.task_id == ResourceTask.id)
                .where(ResourceTask.resource_id.in_(resource_ids))
            )
        ).fetchall()

        result: dict[str, str] = {}
        for rid, title, main_content in rows:
            text = main_content or title or ""
            if text:
                result[rid] = text
        return result

    async def _apply_reranking(
        self,
        query_text: str,
        fused: list[tuple[str, str, float, float, float, float]],
        reranker_weight: float,
    ) -> tuple[list[tuple[str, str, float, float, float, float]], dict[str, float]]:
        """Apply cross-encoder reranking to fused results.

        Returns (re-sorted fused list, {resource_id: reranker_score} map).
        On failure, returns the original fused list with empty map.
        """
        if not fused:
            return fused, {}

        resource_ids = [f[0] for f in fused]
        text_map = await self._fetch_rerank_texts(resource_ids)

        # Build document list aligned with fused order
        documents: list[str] = []
        valid_indices: list[int] = []
        for i, rid in enumerate(resource_ids):
            txt = text_map.get(rid, "")
            if txt:
                documents.append(txt)
                valid_indices.append(i)

        if not documents:
            return fused, {}

        reranker = get_reranker()
        pairs = await reranker.rerank(query_text, documents, top_k=len(documents))

        # Build score map: resource_id -> reranker_score
        score_map: dict[str, float] = {}
        for pair in pairs:
            orig_idx = valid_indices[pair.index]
            score_map[resource_ids[orig_idx]] = pair.score

        # Normalize rrf_final scores to [0, 1] so they are comparable with reranker scores
        rrf_scores = [item[5] for item in fused]
        rrf_min = min(rrf_scores)
        rrf_max = max(rrf_scores)
        rrf_range = rrf_max - rrf_min if rrf_max > rrf_min else 1.0

        # Re-sort using weighted combination and update final_score
        resorted: list[tuple[str, str, float, float, float, float]] = []
        for item in fused:
            rid = item[0]
            rrf_normalized = (item[5] - rrf_min) / rrf_range
            rerank_s = score_map.get(rid, 0.0)
            combined = (1.0 - reranker_weight) * rrf_normalized + reranker_weight * rerank_s
            # Update final_score (index 5) to reflect reranker contribution
            resorted.append((item[0], item[1], item[2], item[3], item[4], combined))
        resorted.sort(key=lambda x: x[5], reverse=True)
        return resorted, score_map

    # ------------------------------------------------------------------
    # Result building
    # ------------------------------------------------------------------

    async def _build_search_results(
        self,
        fused: list[tuple[str, str, float, float, float, float]],
        request: SearchRequest,
        normalized_format_filter: set[str],
        rerank_score_map: dict[str, float] | None = None,
    ) -> SearchResponse:
        """Build SearchResponse from fused results.

        fused items: (rid, rtype, vector_score, bm25_score, rrf_score, final_score)
        """
        resource_ids = [f[0] for f in fused]
        if not resource_ids:
            return self._empty_response(request)

        # Batch load tasks
        tasks_raw = (
            await self.session.execute(
                select(ResourceTask).where(ResourceTask.resource_id.in_(resource_ids))
            )
        ).scalars().all()
        task_by_rid = {t.resource_id: t for t in tasks_raw}
        task_ids = [t.id for t in tasks_raw]

        # Batch load descriptions
        descs_by_task: dict[int, str] = {}
        if task_ids:
            for d in (
                await self.session.execute(
                    select(ResourceDescription).where(ResourceDescription.task_id.in_(task_ids))
                )
            ).scalars().all():
                descs_by_task[d.task_id] = d.main_content

        # Batch load files
        files_by_task: dict[int, list] = {}
        if task_ids:
            for f in (
                await self.session.execute(
                    select(ResourceFile).where(ResourceFile.task_id.in_(task_ids))
                )
            ).scalars().all():
                files_by_task.setdefault(f.task_id, []).append(f)

        # Batch load previews
        previews_by_task: dict[int, list] = {}
        if task_ids:
            for p in (
                await self.session.execute(
                    select(ResourcePreview).where(ResourcePreview.task_id.in_(task_ids))
                    .order_by(ResourcePreview.id)
                )
            ).scalars().all():
                previews_by_task.setdefault(p.task_id, []).append(p)

        # Build results
        results: list[SearchResultItem] = []
        for rid, rtype, v_score, b_score, rrf_score, final_score in fused:
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
                preview_key = self._preview_object_key(rid, pr)
                if preview_key:
                    preview_urls.append(self._download_url(preview_key, pr.storage_profile_id))
            preview_urls = list(dict.fromkeys(preview_urls))

            file_download_url = ""
            if task.source_object_key:
                file_download_url = self._download_url(task.source_object_key, task.source_storage_profile_id)
            elif files and task.resource_type != PACK_RESOURCE_TYPE:
                primary_file = files[0]
                file_key = self._file_object_key(rid, primary_file)
                file_download_url = self._download_url(file_key, primary_file.storage_profile_id)

            package_download_url = self._download_url(
                task.package_object_key,
                task.package_storage_profile_id,
            )

            results.append(SearchResultItem(
                resource_id=rid,
                resource_type=rtype,
                score=final_score,
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
                package_download_url=package_download_url,
                vector_score=v_score,
                bm25_score=b_score,
                rrf_score=rrf_score,
                reranker_score=(rerank_score_map or {}).get(rid, 0.0),
            ))
            if len(results) >= request.top_k:
                break

        return SearchResponse(
            results=results,
            total_count=len(results),
            suggestion=None if results else self._make_suggestion(request),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _empty_response(self, request: SearchRequest) -> SearchResponse:
        return SearchResponse(results=[], total_count=0, suggestion=self._make_suggestion(request))

    def _make_suggestion(self, request: SearchRequest) -> SearchSuggestion:
        return SearchSuggestion(
            rewrite_queries=[f"{request.query_text} 高清", f"{request.query_text} 素材"],
            relaxable_filters=["resource_type", "format_filter"],
            suggested_threshold=max(0.1, request.similarity_threshold - 0.2),
            try_cross_type=True,
        )
