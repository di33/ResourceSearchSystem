"""Search and download endpoints."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_milvus
from app.services.object_urls import ObjectUrlGenerator
from app.services.milvus_search_client import MilvusSearchClient
from CloudService.search_client import DownloadLinkRequest, SearchRequest

router = APIRouter(tags=["search"])


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class SearchBody(BaseModel):
    query_text: str
    resource_type: Optional[str] = None
    format_filter: Optional[List[str]] = None
    top_k: int = 10
    similarity_threshold: float = 0.5
    # --- BM25 / Hybrid ---
    search_mode: str = Field(default="hybrid", pattern="^(vector|bm25|hybrid)$")
    bm25_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    # --- Reranker ---
    enable_reranker: Optional[bool] = None

class FileStructureOut(BaseModel):
    source: str = "processor"
    state: str = "complete"
    source_object_checksum: str = ""
    entry_count: int = 0
    total_size: int = 0

class SearchResultOut(BaseModel):
    resource_id: str
    resource_type: str
    score: float
    primary_preview_url: str
    other_preview_urls: List[str] = Field(default_factory=list)
    file_download_url: str
    description_summary: str
    file_format: str
    file_size: int
    status: str
    preview_available: bool
    file_count: int = 0
    title: str = ""
    source_resource_id: str = ""
    package_download_url: str = ""
    file_structure: FileStructureOut = Field(default_factory=FileStructureOut)
    # --- BM25 / Hybrid scores ---
    vector_score: float = 0.0
    bm25_score: float = 0.0
    rrf_score: float = 0.0
    reranker_score: float = 0.0

class SuggestionOut(BaseModel):
    rewrite_queries: List[str] = Field(default_factory=list)
    relaxable_filters: List[str] = Field(default_factory=list)
    suggested_threshold: Optional[float] = None
    try_cross_type: bool = False

class SearchOut(BaseModel):
    results: List[SearchResultOut]
    total_count: int
    suggestion: Optional[SuggestionOut] = None

class DownloadBody(BaseModel):
    resource_id: str
    expire_seconds: int = 3600
    return_base64: bool = False

class DownloadOut(BaseModel):
    download_url: str
    expires_at: str
    file_name: str
    file_size: int
    content_type: str
    base64_content: Optional[str] = None
    error_code: str = ""
    error_message: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_search_client(session: AsyncSession) -> MilvusSearchClient:
    return MilvusSearchClient(get_milvus(), session, ObjectUrlGenerator())


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/search", response_model=SearchOut)
async def search_resources(body: SearchBody, session: AsyncSession = Depends(get_db)):
    client = _build_search_client(session)
    req = SearchRequest(
        query_text=body.query_text,
        resource_type=body.resource_type,
        format_filter=body.format_filter,
        top_k=body.top_k,
        similarity_threshold=body.similarity_threshold,
        search_mode=body.search_mode,
        bm25_weight=body.bm25_weight,
        enable_reranker=body.enable_reranker,
    )
    resp = await client.search(req)

    suggestion = None
    if resp.suggestion:
        suggestion = SuggestionOut(
            rewrite_queries=resp.suggestion.rewrite_queries,
            relaxable_filters=resp.suggestion.relaxable_filters,
            suggested_threshold=resp.suggestion.suggested_threshold,
            try_cross_type=resp.suggestion.try_cross_type,
        )

    return SearchOut(
        results=[
            SearchResultOut(
                resource_id=r.resource_id,
                resource_type=r.resource_type,
                score=r.score,
                primary_preview_url=r.primary_preview_url,
                other_preview_urls=r.other_preview_urls,
                file_download_url=r.file_download_url,
                description_summary=r.description_summary,
                file_format=r.file_format,
                file_size=r.file_size,
                status=r.status,
                preview_available=r.preview_available,
                file_count=r.file_count,
                title=r.title,
                source_resource_id=r.source_resource_id,
                package_download_url=r.package_download_url,
                file_structure=FileStructureOut(
                    source=r.file_structure.source,
                    state=r.file_structure.state,
                    source_object_checksum=r.file_structure.source_object_checksum,
                    entry_count=r.file_structure.entry_count,
                    total_size=r.file_structure.total_size,
                ),
                vector_score=r.vector_score,
                bm25_score=r.bm25_score,
                rrf_score=r.rrf_score,
                reranker_score=r.reranker_score,
            )
            for r in resp.results
        ],
        total_count=resp.total_count,
        suggestion=suggestion,
    )


@router.post("/download", response_model=DownloadOut)
async def download_resource(body: DownloadBody, session: AsyncSession = Depends(get_db)):
    if body.return_base64:
        raise HTTPException(
            status_code=400,
            detail="Base64 downloads are disabled; use the returned download URL or streaming download endpoint.",
        )
    client = _build_search_client(session)
    req = DownloadLinkRequest(
        resource_id=body.resource_id,
        expire_seconds=body.expire_seconds,
        return_base64=body.return_base64,
    )
    resp = await client.get_download_link(req)

    return DownloadOut(
        download_url=resp.download_url,
        expires_at=resp.expires_at,
        file_name=resp.file_name,
        file_size=resp.file_size,
        content_type=resp.content_type,
        base64_content=resp.base64_content,
        error_code=resp.error_code,
        error_message=resp.error_message,
    )
