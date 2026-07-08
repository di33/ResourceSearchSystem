from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator
from pydantic import field_validator
from resource_contracts.resource_types import normalize_resource_type


class ObjectRef(BaseModel):
    storage_profile_id: str = ""
    object_key: str = ""
    file_name: str = ""
    file_format: str = ""
    size: int = 0
    checksum: str = ""
    etag: str = ""
    is_primary: bool = False


class SourceFileRef(BaseModel):
    file_name: str
    file_format: str = ""
    file_size: int = 0
    checksum: str = ""
    path_in_package: str = ""
    is_primary: bool = False


class PreviewRenderRequest(BaseModel):
    client_resource_id: str
    resource_type: str
    source_object: ObjectRef
    source_object_url: str
    source_files: list[SourceFileRef]
    client_metadata: Any | None = None

    @model_validator(mode="after")
    def _has_source_files(self):
        if not self.source_files:
            raise ValueError("source_files must not be empty")
        if not self.source_object_url.strip():
            raise ValueError("source_object_url must not be blank")
        return self

    @field_validator("resource_type")
    @classmethod
    def _normalize_resource_type(cls, value: str) -> str:
        normalized = normalize_resource_type(value, allow_unknown=True)
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class PreviewFileOut(BaseModel):
    role: str = "primary"
    file_name: str
    content_type: str = "application/octet-stream"
    width: int | None = None
    height: int | None = None
    size: int | None = None
    checksum: str = ""
    strategy: str = "static"
    mode: str = ""
    confidence: str = ""
    origin: str = "generated"
    renderer: str = "preview-renderer"
    used_placeholder: bool = False
    fail_reason: str = ""


class PreviewRenderManifest(BaseModel):
    client_resource_id: str
    previews: list[PreviewFileOut] = Field(default_factory=list)
    preview_count: int = 0


class ErrorOut(BaseModel):
    detail: str
