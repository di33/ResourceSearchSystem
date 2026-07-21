from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator
from resource_contracts.file_structure import validate_file_structure
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

    @field_validator("object_key")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("must not be blank")
        return value


def _object_file_name(ref: ObjectRef) -> str:
    if ref.file_name:
        return ref.file_name
    return ref.object_key.rstrip("/").rsplit("/", 1)[-1] or "source"


def _object_file_format(ref: ObjectRef) -> str:
    if ref.file_format:
        return ref.file_format.lower().lstrip(".")
    name = _object_file_name(ref)
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


class FileStructureEntry(BaseModel):
    path: str
    name: str
    type: str = "file"
    size: int = 0
    format: str = ""
    checksum: str = ""
    is_primary: bool = False

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SourceFileRef(BaseModel):
    """Deprecated input-only compatibility DTO for legacy Python callers."""

    file_name: str
    file_format: str = ""
    file_size: int = 0
    checksum: str = ""
    path_in_package: str = ""
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


class PreviewRef(BaseModel):
    role: str = "primary"
    storage_profile_id: str = ""
    object_key: str
    width: int | None = None
    height: int | None = None
    size: int | None = None
    checksum: str = ""
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


class Description(BaseModel):
    summary: str = ""
    detail: str = ""
    prompt_version: str = "client-provided"
    description_quality_score: float | None = None
    source: str = "client"

    @model_validator(mode="after")
    def _has_description_text(self):
        if not (self.summary or self.detail):
            raise ValueError("description must contain text")
        return self


class Classification(BaseModel):
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    style: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)
    usage_space: str = ""
    usage_category: str = ""
    usage_subcategories: list[str] = Field(default_factory=list)
    usage_classification_reason: str = ""
    usage_classification_suggestion: dict[str, Any] | None = None
    usage_classification_version: str = ""

    def has_values(self) -> bool:
        for value in self.model_dump().values():
            if value not in ("", [], {}, None):
                return True
        return False


class _ManifestFields(BaseModel):
    client_resource_id: str
    resource_type: str
    source_object: ObjectRef
    file_structure: FileStructure | None = None
    package_object: ObjectRef | None = None
    previews: list[PreviewRef] = Field(default_factory=list)
    description: Description | None = None
    description_context: Any | None = None
    client_metadata: dict[str, Any] = Field(default_factory=dict)
    classification: Classification | None = None

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_source_files(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        legacy = data.pop("source_files", None)
        if data.get("file_structure") is None and legacy:
            source_object = data.get("source_object")
            source_checksum = (
                source_object.get("checksum")
                if isinstance(source_object, dict)
                else getattr(source_object, "checksum", "")
            )
            data["file_structure"] = {
                "source": "client",
                "source_object_checksum": str(source_checksum or ""),
                "entries": legacy,
            }
        return data

    @model_validator(mode="after")
    def _structure_matches_source_object(self):
        if self.file_structure is not None:
            declared = self.file_structure.source_object_checksum.strip()
            actual = self.source_object.checksum.strip()
            if declared and actual and declared != actual:
                raise ValueError("file_structure.source_object_checksum does not match source_object.checksum")
        return self

    @field_validator("client_resource_id")
    @classmethod
    def _client_resource_id_not_blank(cls, value: str) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("resource_type")
    @classmethod
    def _normalize_resource_type(cls, value: str) -> str:
        return _normalize_resource_type_value(value)


class ProcessingOptions(BaseModel):
    """Deprecated compatibility field.

    Client-supplied policies are intentionally ignored by the processing
    service. The server decides from description/previews.
    """

    preview_policy: str = "use_provided_or_generate"
    description_policy: str = "generate"


class ChildResourceManifest(_ManifestFields):
    options: ProcessingOptions = Field(default_factory=ProcessingOptions)


class ResourceManifest(_ManifestFields):
    request_id: str = ""
    options: ProcessingOptions = Field(default_factory=ProcessingOptions)


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


class JobStatusBatchIn(BaseModel):
    job_ids: list[str]

    @field_validator("job_ids")
    @classmethod
    def _valid_job_ids(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))
        if not normalized:
            raise ValueError("job_ids must not be empty")
        if len(normalized) > 1000:
            raise ValueError("job_ids must contain at most 1000 items")
        return normalized


class JobStatusOut(BaseModel):
    job_id: str
    state: JobState
    client_resource_id: str
    search_resource_id: str = ""
    error: str | None = None


class JobStatusBatchOut(BaseModel):
    jobs: list[JobStatusOut]
    missing_job_ids: list[str] = Field(default_factory=list)


class ReplaySnapshotOut(BaseModel):
    client_id: str = ""
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
