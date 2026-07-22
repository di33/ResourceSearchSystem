from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from resource_contracts.resource_types import CANONICAL_RESOURCE_TYPES  # noqa: E402


# ---------------------------------------------------------------------------
# Search data classes
# ---------------------------------------------------------------------------

@dataclass
class SearchRequest:
    query_text: str
    resource_type: Optional[str] = None  # None = all types
    format_filter: Optional[List[str]] = None
    top_k: int = 10
    similarity_threshold: float = 0.5
    # --- BM25 / Hybrid ---
    search_mode: str = "hybrid"  # "vector" | "bm25" | "hybrid"
    bm25_weight: float = 0.5     # RRF weight for BM25 (0-1)
    # --- Reranker ---
    enable_reranker: Optional[bool] = None  # None = use server default

@dataclass
class FileStructure:
    source: str = "processor"
    state: str = "complete"
    source_object_checksum: str = ""
    entry_count: int = 0
    total_size: int = 0

@dataclass
class SearchResultItem:
    resource_id: str
    resource_type: str
    score: float
    primary_preview_url: str
    description_summary: str
    file_format: str
    file_size: int
    status: str
    preview_available: bool
    file_download_url: str = ""
    # Multi-file support
    file_count: int = 0
    other_preview_urls: List[str] = field(default_factory=list)
    title: str = ""
    source_resource_id: str = ""
    package_download_url: str = ""
    file_structure: FileStructure = field(default_factory=FileStructure)
    # --- BM25 / Hybrid scores ---
    vector_score: float = 0.0
    bm25_score: float = 0.0
    rrf_score: float = 0.0
    reranker_score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

@dataclass
class SearchSuggestion:
    rewrite_queries: List[str] = field(default_factory=list)
    relaxable_filters: List[str] = field(default_factory=list)
    suggested_threshold: Optional[float] = None
    try_cross_type: bool = False

@dataclass
class SearchResponse:
    results: List[SearchResultItem] = field(default_factory=list)
    total_count: int = 0
    suggestion: Optional[SearchSuggestion] = None

    def to_dict(self) -> dict:
        d = {
            "results": [r.to_dict() for r in self.results],
            "total_count": self.total_count,
        }
        if self.suggestion:
            d["suggestion"] = asdict(self.suggestion)
        return d

# ---------------------------------------------------------------------------
# Download link data classes
# ---------------------------------------------------------------------------

@dataclass
class DownloadLinkRequest:
    resource_id: str
    expire_seconds: int = 3600
    return_base64: bool = False

@dataclass
class DownloadLinkResponse:
    download_url: str
    expires_at: str
    file_name: str
    file_size: int
    content_type: str
    base64_content: Optional[str] = None
    error_code: str = ""
    error_message: str = ""

    @property
    def success(self) -> bool:
        return self.error_code == ""


# ---------------------------------------------------------------------------
# Abstract search client
# ---------------------------------------------------------------------------

class BaseSearchClient(ABC):

    @abstractmethod
    async def search(self, request: SearchRequest) -> SearchResponse:
        ...

    @abstractmethod
    async def get_download_link(self, request: DownloadLinkRequest) -> DownloadLinkResponse:
        ...


# ---------------------------------------------------------------------------
# Mock search client
# ---------------------------------------------------------------------------

class MockSearchClient(BaseSearchClient):
    """Mock 检索客户端用于测试。"""

    def __init__(self):
        self.search_calls: list[SearchRequest] = []
        self.download_calls: list[DownloadLinkRequest] = []
        self._mock_results: list[SearchResultItem] = []

    def set_mock_results(self, results: list[SearchResultItem]):
        self._mock_results = results

    async def search(self, request: SearchRequest) -> SearchResponse:
        self.search_calls.append(request)
        results = self._mock_results
        if request.resource_type:
            results = [r for r in results if r.resource_type == request.resource_type]
        if request.format_filter:
            allowed_formats = {
                fmt.strip().lower().lstrip(".")
                for fmt in request.format_filter
                if isinstance(fmt, str) and fmt.strip()
            }
            if allowed_formats:
                results = [
                    r for r in results
                    if str(r.file_format or "").strip().lower().lstrip(".") in allowed_formats
                ]
        results = [r for r in results if r.score >= request.similarity_threshold]
        results = sorted(results, key=lambda r: r.score, reverse=True)[:request.top_k]

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
        self.download_calls.append(request)
        return DownloadLinkResponse(
            download_url=f"https://storage.example.com/{request.resource_id}?expires={request.expire_seconds}",
            expires_at="2026-12-31T23:59:59Z",
            file_name=f"{request.resource_id}.png",
            file_size=12345,
            content_type="image/png",
            base64_content="base64data==" if request.return_base64 else None,
        )


# ---------------------------------------------------------------------------
# Agent tool contracts
# ---------------------------------------------------------------------------

@dataclass
class AgentSearchToolInput:
    """Agent 检索预览工具的输入契约。"""
    query_text: str
    resource_type: Optional[str] = None
    format_filter: Optional[List[str]] = None
    top_k: int = 5
    similarity_threshold: float = 0.6

@dataclass
class AgentSearchToolOutput:
    """Agent 检索预览工具的输出契约。"""
    results: List[dict] = field(default_factory=list)
    total_count: int = 0
    rewrite_suggestions: List[str] = field(default_factory=list)
    has_more: bool = False

@dataclass
class AgentDownloadToolInput:
    """Agent 下载工具的输入契约。"""
    resource_id: str
    expire_seconds: int = 3600
    return_base64: bool = False

@dataclass
class AgentDownloadToolOutput:
    """Agent 下载工具的输出契约。"""
    download_url: str = ""
    expires_at: str = ""
    file_name: str = ""
    file_size: int = 0
    error_code: str = ""
    error_message: str = ""
