from __future__ import annotations

import uuid
import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# Data classes for file and preview metadata
# ---------------------------------------------------------------------------

@dataclass
class FileInfo:
    """Metadata for a single file belonging to a resource."""
    file_path: str
    file_name: str
    file_size: int
    file_format: str
    content_md5: str
    file_role: str = "main"
    is_primary: bool = False


@dataclass
class PreviewFileInfo:
    """Metadata for a single preview file."""
    file_path: str
    file_name: str
    content_type: str
    role: str = "primary"


# ---------------------------------------------------------------------------
# Data classes for API request/response
# ---------------------------------------------------------------------------

@dataclass
class RegisterRequest:
    content_md5: str
    resource_type: str
    files: List[FileInfo]
    source_resource_id: str = ""
    parent_source_resource_id: str = ""
    child_source_resource_ids: List[str] = field(default_factory=list)
    child_resource_count: int = 0
    contains_resource_types: List[str] = field(default_factory=list)
    title: str = ""
    source: str = ""
    pack_name: str = ""
    resource_path: str = ""
    source_url: str = ""
    original_download_url: str = ""
    category: str = ""
    license_name: str = ""
    source_description: str = ""
    tags: List[str] = field(default_factory=list)
    download_file_name: str = ""
    download_content_type: str = ""
    download_file_size: int = 0
    idempotency_key: str = ""

    def __post_init__(self):
        if not self.idempotency_key:
            self.idempotency_key = f"register-{uuid.uuid4().hex[:12]}"

    @property
    def total_size(self) -> int:
        return sum(f.file_size for f in self.files)

    @property
    def primary_file(self) -> Optional[FileInfo]:
        for f in self.files:
            if f.is_primary:
                return f
        return self.files[0] if self.files else None

@dataclass
class RegisterResponse:
    resource_id: str
    exists: bool
    upload_mode: str  # "direct" or "multipart"
    multipart_chunk_size: int
    state: str  # "registered"

@dataclass
class UploadResult:
    success: bool
    uploaded_bytes: int = 0
    s3_etag: str = ""
    error_message: str = ""

@dataclass
class CommitRequest:
    resource_id: str
    resource_type: str
    description_main: str
    description_detail: str
    description_full: str
    idempotency_key: str = ""

    def __post_init__(self):
        if not self.idempotency_key:
            self.idempotency_key = f"commit-{uuid.uuid4().hex[:12]}"

@dataclass
class CommitResponse:
    resource_id: str
    state: str  # "committed" or "failed"
    error_message: str = ""


# ---------------------------------------------------------------------------
# Abstract cloud client
# ---------------------------------------------------------------------------

MULTIPART_THRESHOLD = 100 * 1024 * 1024  # 100MB

class BaseCloudClient(ABC):
    """Cloud API client base class."""

    @abstractmethod
    async def register(self, request: RegisterRequest) -> RegisterResponse:
        ...

    @abstractmethod
    async def upload_files(self, resource_id: str, files: List[FileInfo]) -> UploadResult:
        """Upload all files belonging to a resource. Returns aggregated result."""
        ...

    @abstractmethod
    async def upload_previews(self, resource_id: str, previews: List[PreviewFileInfo]) -> UploadResult:
        """Upload all preview files belonging to a resource. Returns aggregated result."""
        ...

    @abstractmethod
    async def commit(self, request: CommitRequest) -> CommitResponse:
        ...

    def determine_upload_mode(self, file_size: int) -> str:
        return "multipart" if file_size > MULTIPART_THRESHOLD else "direct"


# ---------------------------------------------------------------------------
# Mock cloud client
# ---------------------------------------------------------------------------

class MockCloudClient(BaseCloudClient):
    """Mock client for testing. Records all calls for assertion."""

    def __init__(self):
        self.register_calls: list[RegisterRequest] = []
        self.upload_files_calls: list[tuple] = []
        self.upload_previews_calls: list[tuple] = []
        self.commit_calls: list[CommitRequest] = []
        self._registered: dict[str, RegisterResponse] = {}

    async def register(self, request: RegisterRequest) -> RegisterResponse:
        self.register_calls.append(request)
        resource_id = f"res-{uuid.uuid4().hex[:8]}"
        upload_mode = self.determine_upload_mode(request.total_size)
        resp = RegisterResponse(
            resource_id=resource_id,
            exists=False,
            upload_mode=upload_mode,
            multipart_chunk_size=10 * 1024 * 1024,
            state="registered",
        )
        self._registered[resource_id] = resp
        return resp

    async def upload_files(self, resource_id: str, files: List[FileInfo]) -> UploadResult:
        self.upload_files_calls.append((resource_id, files))
        total = sum(f.file_size for f in files)
        return UploadResult(success=True, uploaded_bytes=total)

    async def upload_previews(self, resource_id: str, previews: List[PreviewFileInfo]) -> UploadResult:
        self.upload_previews_calls.append((resource_id, previews))
        return UploadResult(success=True, uploaded_bytes=len(previews) * 1024)

    async def commit(self, request: CommitRequest) -> CommitResponse:
        self.commit_calls.append(request)
        return CommitResponse(
            resource_id=request.resource_id,
            state="committed",
        )
