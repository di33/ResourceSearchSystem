from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from resource_contracts.resource_types import normalize_resource_type


def _normalize_resource_type_value(value: str) -> str:
    normalized = normalize_resource_type(value, allow_unknown=True)
    if not normalized:
        raise ValueError("must not be blank")
    return normalized


class ObjectRef(BaseModel):
    storage_profile_id: str = ""
    object_key: str
    file_name: str = ""
    file_format: str = ""
    size: int = 0
    checksum: str = ""
    etag: str = ""
    is_primary: bool = False

    @field_validator("object_key")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SourceFileRef(BaseModel):
    file_name: str
    file_format: str = ""
    file_size: int = 0
    checksum: str = ""
    path_in_package: str = ""
    is_primary: bool = False

    @field_validator("file_name")
    @classmethod
    def _file_name_not_blank(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class PreviewRef(BaseModel):
    role: str = "primary"
    storage_profile_id: str = ""
    object_key: str
    width: int | None = None
    height: int | None = None
    size: int | None = None
    checksum: str = ""
    etag: str = ""
    strategy: str = "static"
    origin: str = "provided"
    renderer: str = "client"

    @field_validator("object_key")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ProvidedDescription(BaseModel):
    main_content: str = ""
    detail_content: str = ""
    prompt_version: str = "client-provided"
    description_quality_score: float | None = None
    usage_space: str = ""
    usage_category: str = ""
    usage_subcategories: list[str] = Field(default_factory=list)
    usage_classification_reason: str = ""
    usage_classification_suggestion: dict[str, Any] | None = None
    usage_classification_version: str = ""
    source: str = "client"

    @model_validator(mode="after")
    def _has_description_text(self):
        if not (self.main_content or self.detail_content):
            raise ValueError("provided_description must contain description text")
        return self


class ProcessingOptions(BaseModel):
    """Deprecated compatibility field.

    Client-supplied policies are intentionally ignored by the processing
    service. The server decides from provided_description/provided_previews.
    """

    preview_policy: str = "use_provided_or_generate"
    description_policy: str = "generate"


class ChildResourceManifest(BaseModel):
    client_resource_id: str
    resource_type: str
    source_object: ObjectRef
    source_files: list[SourceFileRef]
    package_object: ObjectRef | None = None
    provided_previews: list[PreviewRef] = Field(default_factory=list)
    provided_description: ProvidedDescription | None = None
    client_metadata: Any | None = None
    options: ProcessingOptions = Field(default_factory=ProcessingOptions)

    @model_validator(mode="after")
    def _has_source_files(self):
        if not self.source_files:
            raise ValueError("source_files must not be empty")
        return self

    @field_validator("resource_type")
    @classmethod
    def _normalize_resource_type(cls, value: str) -> str:
        return _normalize_resource_type_value(value)


class ResourceManifest(BaseModel):
    request_id: str = ""
    client_resource_id: str
    resource_type: str
    source_object: ObjectRef
    source_files: list[SourceFileRef]
    package_object: ObjectRef | None = None
    provided_previews: list[PreviewRef] = Field(default_factory=list)
    provided_description: ProvidedDescription | None = None
    client_metadata: Any | None = None
    options: ProcessingOptions = Field(default_factory=ProcessingOptions)

    @model_validator(mode="after")
    def _has_source_files(self):
        if not self.source_files:
            raise ValueError("source_files must not be empty")
        return self

    @field_validator("resource_type")
    @classmethod
    def _normalize_resource_type(cls, value: str) -> str:
        return _normalize_resource_type_value(value)


class ResourceBatchManifest(BaseModel):
    request_id: str = ""
    manifests: list[ResourceManifest]

    @model_validator(mode="after")
    def _has_manifests(self):
        if not self.manifests:
            raise ValueError("manifests must not be empty")
        return self


class JobState(str, Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    PREVIEWING = "previewing"
    DESCRIBING = "describing"
    SUBMITTING = "submitting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStep(BaseModel):
    name: str
    state: str
    duration_ms: int = 0
    error: str = ""


class ProcessingJob(BaseModel):
    job_id: str
    client_id: str
    client_resource_id: str
    state: JobState
    manifest: ResourceManifest
    batch_id: str = ""
    search_resource_id: str = ""
    steps: list[JobStep] = Field(default_factory=list)
    error: str | None = None


class CreateJobOut(BaseModel):
    job_id: str
    state: JobState
    resource_fingerprint: str = ""


class BatchJobOut(BaseModel):
    job_id: str
    client_resource_id: str
    state: JobState
    resource_fingerprint: str = ""


class CreateBatchOut(BaseModel):
    batch_id: str
    jobs: list[BatchJobOut]


class JobOut(BaseModel):
    job_id: str
    state: JobState
    client_resource_id: str
    search_resource_id: str = ""
    steps: list[JobStep] = Field(default_factory=list)
    error: str | None = None


class ReplaySnapshotOut(BaseModel):
    client_resource_id: str
    state: str
    search_resource_id: str = ""
    error_message: str = ""


class ReplaySnapshotsOut(BaseModel):
    total: int
    items: list[ReplaySnapshotOut]


class DeleteProcessedResourceIn(BaseModel):
    client_resource_id: str = ""
    resource_id: str = ""
    idempotency_key: str = ""
    delete_objects: bool = True
    reason: str = ""


class DeletedObjectRef(BaseModel):
    storage_profile_id: str = ""
    object_key: str
    kind: str = ""
    origin: str = ""
    renderer: str = ""


class DeleteProcessedResourceOut(BaseModel):
    client_resource_id: str = ""
    search_resource_id: str = ""
    state: str
    search_deleted: bool = False
    objects_deleted: int = 0
    snapshot_deleted: bool = False
    object_refs: list[DeletedObjectRef] = Field(default_factory=list)
    error_message: str = ""


ChildResourceManifest.model_rebuild()
