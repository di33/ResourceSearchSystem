"""Preview metadata and local processing state data structures."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


class PreviewStrategy(str, Enum):
    STATIC = "static"
    GIF = "gif"
    CONTACT_SHEET = "contact_sheet"


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
    content_md5: str
    resource_type: str
    source_path: str
    source_name: str
    source_size: int
    source_format: str

    process_state: ProcessState = ProcessState.DISCOVERED
    preview: Optional[PreviewInfo] = None
    resource_id: Optional[str] = None

    description_main: str = ""
    description_detail: str = ""
    description_full: str = ""
    prompt_version: str = ""
    description_quality_score: Optional[float] = None

    retry_count: int = 0
    last_error_code: str = ""
    last_error_message: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["process_state"] = self.process_state.value
        if self.preview is not None:
            d["preview"] = self.preview.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ResourceProcessingEntity:
        d = dict(d)
        raw_state = d.get("process_state")
        if raw_state is not None and not isinstance(raw_state, ProcessState):
            d["process_state"] = ProcessState(raw_state)
        raw_preview = d.get("preview")
        if isinstance(raw_preview, dict):
            d["preview"] = PreviewInfo.from_dict(raw_preview)
        return cls(**d)
