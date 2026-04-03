from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import List, Optional


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

@dataclass
class SearchResultItem:
    resource_id: str
    resource_type: str
    score: float
    preview_url: str
    description_summary: str
    file_format: str
    file_size: int
    status: str
    preview_available: bool

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
