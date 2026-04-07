"""Preview metadata and local processing state data structures."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import List, Optional


class PreviewStrategy(str, Enum):
    STATIC = "static"
    GIF = "gif"
    CONTACT_SHEET = "contact_sheet"


@dataclass
class FileInfo:
    file_path: str
    file_name: str
    file_size: int
    file_format: str
    content_md5: str
    file_role: str = "main"
    is_primary: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FileInfo":
        return cls(**d)


class ProcessState(str, Enum):
    DISCOVERED = "discovered"
    PREVIEW_READY = "preview_ready"
    PREVIEW_FAILED = "preview_failed"
    DESCRIPTION_READY = "description_ready"
    DESCRIPTION_FAILED = "description_failed"
    EMBEDDING_READY = "embedding_ready"
    EMBEDDING_FAILED = "embedding_failed"
    PACKAGE_READY = "package_ready"
    REGISTERED = "registered"
    UPLOADED = "uploaded"
    COMMITTED = "committed"
    SYNCED = "synced"


@dataclass
class PreviewInfo:
    strategy: PreviewStrategy
    role: str = "primary"
    path: Optional[str] = None
    format: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    size: Optional[int] = None
    renderer: Optional[str] = None
    used_placeholder: bool = False
    fail_reason: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["strategy"] = self.strategy.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> PreviewInfo:
        d = dict(d)
        raw = d.get("strategy")
        if raw is not None and not isinstance(raw, PreviewStrategy):
            d["strategy"] = PreviewStrategy(raw)
        return cls(**d)


@dataclass
class ResourceProcessingEntity:
    """Represents a resource that can contain multiple files and multiple previews."""
    resource_type: str
    source_directory: str
    files: List[FileInfo] = field(default_factory=list)
    content_md5: str = ""  # composite fingerprint: MD5(sorted individual file MD5s)

    process_state: ProcessState = ProcessState.DISCOVERED
    previews: List[PreviewInfo] = field(default_factory=list)
    resource_id: Optional[str] = None

    description_main: str = ""
    description_detail: str = ""
    description_full: str = ""
    prompt_version: str = ""
    description_quality_score: Optional[float] = None

    embedding_dimension: int = 0
    embedding_checksum: str = ""
    embedding_generate_time: float = 0.0
    embedding_model_version: str = ""

    retry_count: int = 0
    last_error_code: str = ""
    last_error_message: str = ""
    updated_at: str = ""

    @property
    def primary_file(self) -> Optional[FileInfo]:
        for f in self.files:
            if f.is_primary:
                return f
        return self.files[0] if self.files else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["process_state"] = self.process_state.value
        d["files"] = [f.to_dict() for f in self.files]
        d["previews"] = [p.to_dict() for p in self.previews]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ResourceProcessingEntity:
        d = dict(d)
        raw_state = d.get("process_state")
        if raw_state is not None and not isinstance(raw_state, ProcessState):
            d["process_state"] = ProcessState(raw_state)
        raw_files = d.get("files")
        if isinstance(raw_files, list):
            d["files"] = [FileInfo.from_dict(f) for f in raw_files]
        raw_previews = d.get("previews")
        if isinstance(raw_previews, list):
            d["previews"] = [PreviewInfo.from_dict(p) for p in raw_previews]
        # Backward compat: migrate old single-file format
        if not d.get("files"):
            old_path = d.pop("source_path", "")
            old_name = d.pop("source_name", "")
            old_size = d.pop("source_size", 0)
            old_fmt = d.pop("source_format", "")
            if old_path:
                d["files"] = [FileInfo(
                    file_path=old_path,
                    file_name=old_name,
                    file_size=old_size,
                    file_format=old_fmt,
                    content_md5=d.get("content_md5", ""),
                    is_primary=True,
                )]
        # Backward compat: migrate old single preview
        if not d.get("previews"):
            old_preview = d.pop("preview", None)
            if isinstance(old_preview, dict):
                d["previews"] = [PreviewInfo.from_dict(old_preview)]
            else:
                d.pop("preview", None)
        return cls(**d)
