from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from ResourceProcessor.search_client import (
    BaseSearchClient,
    DownloadLinkRequest,
    DownloadLinkResponse,
    AgentDownloadToolInput,
    AgentDownloadToolOutput,
)


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

class DownloadErrorCode:
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    RESOURCE_NOT_AVAILABLE = "RESOURCE_NOT_AVAILABLE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    LINK_GENERATE_FAILED = "LINK_GENERATE_FAILED"
    INVALID_REQUEST = "INVALID_REQUEST"


# ---------------------------------------------------------------------------
# Download service configuration
# ---------------------------------------------------------------------------

@dataclass
class DownloadConfig:
    max_expire_seconds: int = 86400
    default_expire_seconds: int = 3600
    base64_size_threshold: int = 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# Download service
# ---------------------------------------------------------------------------

class DownloadService:
    """Download service that wraps the search client with validation,
    error handling, and Agent tool adaptation."""

    def __init__(self, search_client: BaseSearchClient, config: Optional[DownloadConfig] = None):
        self.client = search_client
        self.config = config or DownloadConfig()

    def validate_request(self, resource_id: str, expire_seconds: int) -> Optional[DownloadLinkResponse]:
        """Validate request parameters. Returns error response if invalid, None if OK."""
        if not resource_id or not resource_id.strip():
            return DownloadLinkResponse(
                download_url="", expires_at="", file_name="", file_size=0,
                content_type="",
                error_code=DownloadErrorCode.INVALID_REQUEST,
                error_message="resource_id 不能为空",
            )
        if expire_seconds <= 0:
            return DownloadLinkResponse(
                download_url="", expires_at="", file_name="", file_size=0,
                content_type="",
                error_code=DownloadErrorCode.INVALID_REQUEST,
                error_message="expire_seconds 必须大于 0",
            )
        if expire_seconds > self.config.max_expire_seconds:
            return DownloadLinkResponse(
                download_url="", expires_at="", file_name="", file_size=0,
                content_type="",
                error_code=DownloadErrorCode.INVALID_REQUEST,
                error_message=f"expire_seconds 不能超过 {self.config.max_expire_seconds}",
            )
        return None

    async def get_download_link(
        self,
        resource_id: str,
        expire_seconds: Optional[int] = None,
        return_base64: bool = False,
    ) -> DownloadLinkResponse:
        """Get a download link with validation and error handling."""
        if expire_seconds is None:
            expire_seconds = self.config.default_expire_seconds

        error = self.validate_request(resource_id, expire_seconds)
        if error:
            return error

        try:
            request = DownloadLinkRequest(
                resource_id=resource_id,
                expire_seconds=expire_seconds,
                return_base64=return_base64,
            )
            response = await self.client.get_download_link(request)
            return response
        except Exception as exc:
            logging.error("下载链接生成失败 (resource_id=%s): %s", resource_id, exc)
            return DownloadLinkResponse(
                download_url="", expires_at="", file_name="", file_size=0,
                content_type="",
                error_code=DownloadErrorCode.LINK_GENERATE_FAILED,
                error_message=str(exc),
            )

    def should_return_base64(self, file_size: int, explicit_request: bool) -> bool:
        """Determine if base64 content should be returned based on file size and request."""
        if not explicit_request:
            return False
        return file_size <= self.config.base64_size_threshold


# ---------------------------------------------------------------------------
# Agent tool adapter
# ---------------------------------------------------------------------------

class AgentDownloadToolAdapter:
    """Adapter that converts Agent tool inputs/outputs to/from download service calls."""

    def __init__(self, download_service: DownloadService):
        self.service = download_service

    async def execute(self, tool_input: AgentDownloadToolInput) -> AgentDownloadToolOutput:
        """Execute the Agent download tool."""
        response = await self.service.get_download_link(
            resource_id=tool_input.resource_id,
            expire_seconds=tool_input.expire_seconds,
            return_base64=tool_input.return_base64,
        )

        if not response.success:
            return AgentDownloadToolOutput(
                error_code=response.error_code,
                error_message=response.error_message,
            )

        return AgentDownloadToolOutput(
            download_url=response.download_url,
            expires_at=response.expires_at,
            file_name=response.file_name,
            file_size=response.file_size,
        )
