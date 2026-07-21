from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator
from pydantic import field_validator
from resource_contracts.file_structure import validate_file_structure
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


class FileStructureEntry(BaseModel):
    path: str
    name: str
    type: str = "file"
    size: int = 0
    format: str = ""
    checksum: str = ""
    is_primary: bool = False


class FileStructure(BaseModel):
    source: str = "client"
    state: str = "complete"
    source_object_checksum: str = ""
    entry_count: int = 0
    total_size: int = 0
    entries: list[FileStructureEntry] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, value):
        return validate_file_structure(value)


class PreviewRenderRequest(BaseModel):
    client_resource_id: str
    resource_type: str
    source_object: ObjectRef
    source_object_url: str
    file_structure: FileStructure
    client_metadata: Any | None = None

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_source_files(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        legacy = data.pop("source_files", None)
        if data.get("file_structure") is None and legacy:
            data["file_structure"] = {"source": "processor", "entries": legacy}
        return data

    @model_validator(mode="after")
    def _has_file_structure(self):
        if not self.file_structure.entries:
            raise ValueError("file_structure.entries must not be empty")
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
