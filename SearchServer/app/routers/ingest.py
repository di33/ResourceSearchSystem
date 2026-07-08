"""Processed-resource ingestion endpoints used by the processing server."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.deps import get_db, get_milvus
from app.middleware.auth import require_ingest_auth
from app.models.tables import (
    ProcessLog,
    ResourceDescription,
    ResourceEmbedding,
    ResourceFile,
    ResourcePreview,
    ResourceTask,
    VectorSyncJob,
)
from app.services.embedding_client import generate_embedding, get_model_version
from resource_contracts.resource_types import normalize_resource_type

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/resources",
    tags=["resources"],
    dependencies=[Depends(require_ingest_auth)],
)


class ObjectRefIn(BaseModel):
    storage_profile_id: str = ""
    object_key: str
    file_name: str = ""
    file_format: str = ""
    size: int = 0
    checksum: str = ""
    etag: str = ""
    is_primary: bool = False


class SourceFileIn(BaseModel):
    file_name: str
    file_format: str = ""
    file_size: int = 0
    checksum: str = ""
    path_in_package: str = ""
    is_primary: bool = False


class PreviewRefIn(BaseModel):
    role: str = "primary"
    storage_profile_id: str = ""
    object_key: str
    width: int | None = None
    height: int | None = None
    size: int | None = None
    checksum: str = ""
    etag: str = ""
    strategy: str = "static"
    origin: str = ""
    renderer: str = ""


class DescriptionIn(BaseModel):
    summary: str = ""
    detail: str = ""
    full: str = ""


class ClassificationIn(BaseModel):
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    style: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)
    usage_space: str = ""
    usage_category: str = ""
    usage_subcategories: list[str] = Field(default_factory=list)
    usage_classification_reason: str = ""
    usage_classification_suggestion: dict[str, Any] = Field(default_factory=dict)
    usage_classification_version: str = ""


class ProcessingIn(BaseModel):
    pipeline_version: str = ""
    description_model: str = ""
    description_prompt_version: str = ""
    preview_policy: str = ""


class UpsertResourceIn(BaseModel):
    idempotency_key: str = ""
    client_id: str
    client_resource_id: str
    resource_type: str
    client_metadata: Any | None = None
    title: str = ""
    source_object: ObjectRefIn
    source_files: list[SourceFileIn]
    package_object: ObjectRefIn | None = None
    previews: list[PreviewRefIn] = Field(default_factory=list)
    description: DescriptionIn = Field(default_factory=DescriptionIn)
    classification: ClassificationIn = Field(default_factory=ClassificationIn)
    processing: ProcessingIn = Field(default_factory=ProcessingIn)

    @field_validator("resource_type")
    @classmethod
    def _normalize_resource_type(cls, value: str) -> str:
        normalized = normalize_resource_type(value, allow_unknown=True)
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class UpsertResourceOut(BaseModel):
    resource_id: str
    state: str
    embedding_model: str = ""
    error_message: str = ""


class ObjectRefOut(BaseModel):
    storage_profile_id: str = ""
    object_key: str
    kind: str = ""
    origin: str = ""
    renderer: str = ""


class DeleteResourceIn(BaseModel):
    idempotency_key: str = ""
    client_id: str = ""
    client_resource_id: str = ""
    resource_id: str = ""
    mode: str = "hard"
    delete_objects: bool = False
    reason: str = ""


class DeleteResourceOut(BaseModel):
    resource_id: str = ""
    state: str
    deleted: bool = False
    vector_deleted: bool = False
    object_refs: list[ObjectRefOut] = Field(default_factory=list)
    error_message: str = ""


class RetryVectorSyncOut(BaseModel):
    total: int = 0
    synced: int = 0
    failed: int = 0


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _first_non_empty(*values: str) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _object_file_name(ref: ObjectRefIn) -> str:
    if ref.file_name:
        return ref.file_name
    return ref.object_key.rstrip("/").rsplit("/", 1)[-1] or "resource"


def _file_format(ref: ObjectRefIn) -> str:
    if ref.file_format:
        return ref.file_format.lower().lstrip(".")
    name = _object_file_name(ref)
    if "." in name:
        return name.rsplit(".", 1)[-1].lower()
    return ""


def _source_file_name(ref: SourceFileIn) -> str:
    return ref.file_name or "resource"


def _source_file_format(ref: SourceFileIn) -> str:
    if ref.file_format:
        return ref.file_format.lower().lstrip(".")
    name = _source_file_name(ref)
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _metadata_title(value: Any | None) -> str:
    if isinstance(value, dict):
        return str(value.get("title") or "").strip()
    return ""


def _content_fingerprint(source_object: ObjectRefIn, source_files: list[SourceFileIn]) -> str:
    payload = {
        "source_object": {
            "storage_profile_id": source_object.storage_profile_id,
            "object_key": source_object.object_key,
            "checksum": source_object.checksum,
            "etag": source_object.etag,
            "size": source_object.size,
        },
        "source_files": [
            {
                "file_name": item.file_name,
                "path_in_package": item.path_in_package,
                "checksum": item.checksum,
                "file_size": item.file_size,
            }
            for item in source_files
        ],
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _embedding_text(body: UpsertResourceIn) -> str:
    parts = [
        body.title,
        body.description.summary,
        body.description.detail,
        body.description.full,
        body.classification.category,
        " ".join(body.classification.tags),
        " ".join(body.classification.style),
        " ".join(body.classification.materials),
        " ".join(body.classification.use_cases),
    ]
    return "\n".join(part for part in parts if str(part or "").strip()).strip()


async def _find_task(
    session: AsyncSession,
    client_id: str,
    client_resource_id: str,
) -> ResourceTask | None:
    return (
        await session.execute(
            select(ResourceTask)
            .where(
                ResourceTask.source == client_id,
                ResourceTask.source_resource_id == client_resource_id,
            )
            .order_by(ResourceTask.updated_at.desc(), ResourceTask.id.desc())
        )
    ).scalars().first()


def _write_vector(resource_id: str, resource_type: str, vector: list[float]) -> str:
    milvus = get_milvus()
    data = [{
        "resource_id": resource_id,
        "vector": vector,
        "resource_type": resource_type,
    }]
    try:
        if hasattr(milvus, "upsert"):
            milvus.upsert(collection_name=settings.milvus_collection, data=data)
        else:
            if hasattr(milvus, "delete"):
                milvus.delete(
                    collection_name=settings.milvus_collection,
                    filter=f'resource_id == "{resource_id}"',
                )
            milvus.insert(collection_name=settings.milvus_collection, data=data)
    except Exception as exc:
        logger.error("Milvus vector write failed for %s: %s", resource_id, exc)
        return f"vector insert failed: {exc}"
    return ""


def _delete_vector(resource_id: str) -> str:
    if not resource_id:
        return ""
    milvus = get_milvus()
    if not hasattr(milvus, "delete"):
        return ""
    try:
        milvus.delete(
            collection_name=settings.milvus_collection,
            filter=f'resource_id == "{resource_id}"',
        )
    except Exception as exc:
        logger.error("Milvus vector delete failed for %s: %s", resource_id, exc)
        return f"vector delete failed: {exc}"
    return ""


def _add_vector_sync_job(
    session: AsyncSession,
    *,
    resource_id: str,
    action: str,
    resource_type: str = "",
    vector: list[float] | None = None,
) -> VectorSyncJob:
    job = VectorSyncJob(
        resource_id=resource_id,
        action=action,
        resource_type=resource_type,
        vector_json=json.dumps(vector or [], separators=(",", ":")),
        state="pending",
    )
    session.add(job)
    return job


async def _run_vector_sync_job(session: AsyncSession, job: VectorSyncJob) -> None:
    error = ""
    if job.action == "upsert":
        vector = json.loads(job.vector_json or "[]")
        error = _write_vector(job.resource_id, job.resource_type, vector)
    elif job.action == "delete":
        error = _delete_vector(job.resource_id)
    else:
        error = f"unsupported vector sync action: {job.action}"

    if error:
        job.state = "failed"
        job.last_error = error[:2000]
        task = (
            await session.execute(
                select(ResourceTask).where(ResourceTask.resource_id == job.resource_id)
            )
        ).scalar_one_or_none()
        if task is not None:
            task.vector_state = "failed"
            task.vector_error = error[:2000]
            task.last_error_code = "VECTOR_SYNC_FAILED"
            task.last_error_message = error[:2000]
        await session.commit()
        raise HTTPException(status_code=502, detail=error)

    job.state = "completed"
    job.last_error = ""
    task = (
        await session.execute(
            select(ResourceTask).where(ResourceTask.resource_id == job.resource_id)
        )
    ).scalar_one_or_none()
    if task is not None and job.action == "upsert":
        task.vector_state = "synced"
        task.vector_error = ""
    await session.commit()


def _append_object_ref(
    refs: list[ObjectRefOut],
    *,
    storage_profile_id: str = "",
    object_key: str = "",
    kind: str = "",
    origin: str = "",
    renderer: str = "",
) -> None:
    key = str(object_key or "").strip()
    if not key:
        return
    refs.append(ObjectRefOut(
        storage_profile_id=str(storage_profile_id or ""),
        object_key=key,
        kind=kind,
        origin=origin,
        renderer=renderer,
    ))


def _collect_object_refs(task: ResourceTask) -> list[ObjectRefOut]:
    refs: list[ObjectRefOut] = []
    _append_object_ref(
        refs,
        storage_profile_id=task.source_storage_profile_id,
        object_key=task.source_object_key,
        kind="source_object",
    )

    _append_object_ref(
        refs,
        storage_profile_id=task.package_storage_profile_id,
        object_key=task.package_object_key,
        kind="package_object",
    )

    for file in task.files:
        _append_object_ref(
            refs,
            storage_profile_id=file.storage_profile_id,
            object_key=file.object_key,
            kind="source_file",
        )

    for preview in task.previews:
        preview_key = preview.object_key
        if not preview_key and preview.path and task.resource_id:
            preview_name = str(preview.path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            preview_key = f"previews/{task.resource_id}/{preview_name}" if preview_name else ""
        _append_object_ref(
            refs,
            storage_profile_id=preview.storage_profile_id,
            object_key=preview_key or "",
            kind="preview",
            origin=preview.origin,
            renderer=preview.renderer or "",
        )

    deduped: dict[tuple[str, str], ObjectRefOut] = {}
    for ref in refs:
        deduped.setdefault((ref.storage_profile_id, ref.object_key), ref)
    return list(deduped.values())


async def _find_task_for_delete(session: AsyncSession, body: DeleteResourceIn) -> ResourceTask | None:
    client_id = str(body.client_id or "").strip()
    client_resource_id = str(body.client_resource_id or "").strip()
    if body.resource_id:
        task = (
            await session.execute(
                select(ResourceTask).where(ResourceTask.resource_id == body.resource_id)
            )
        ).scalar_one_or_none()
        if task is not None:
            if client_id and task.source != client_id:
                raise HTTPException(status_code=403, detail="resource does not belong to client_id")
            if client_resource_id and task.source_resource_id != client_resource_id:
                raise HTTPException(status_code=403, detail="resource does not match client_resource_id")
            return task

    if not client_id or not client_resource_id:
        return None
    return await _find_task(session, client_id, client_resource_id)


@router.post("/vector-sync/retry", response_model=RetryVectorSyncOut)
async def retry_vector_sync_jobs(
    limit: int = Query(default=100, ge=1, le=1000),
    session: AsyncSession = Depends(get_db),
):
    jobs = (
        await session.execute(
            select(VectorSyncJob)
            .where(VectorSyncJob.state.in_(["pending", "failed"]))
            .order_by(VectorSyncJob.id)
            .limit(limit)
        )
    ).scalars().all()
    synced = 0
    failed = 0
    for job in jobs:
        try:
            await _run_vector_sync_job(session, job)
            synced += 1
        except HTTPException:
            failed += 1
    return RetryVectorSyncOut(total=len(jobs), synced=synced, failed=failed)


@router.post("/delete", response_model=DeleteResourceOut)
async def delete_processed_resource(
    body: DeleteResourceIn,
    session: AsyncSession = Depends(get_db),
):
    if body.mode != "hard":
        raise HTTPException(status_code=400, detail="only hard delete is currently supported")
    if not body.resource_id and not (body.client_id and body.client_resource_id):
        raise HTTPException(status_code=400, detail="resource_id or client_id + client_resource_id is required")

    task = await _find_task_for_delete(session, body)
    if task is None:
        return DeleteResourceOut(
            resource_id=body.resource_id,
            state="not_found",
            deleted=False,
            vector_deleted=False,
            object_refs=[],
        )

    resource_id = task.resource_id or ""
    object_refs = _collect_object_refs(task)
    vector_job = _add_vector_sync_job(session, resource_id=resource_id, action="delete")

    await session.delete(task)
    await session.commit()
    await _run_vector_sync_job(session, vector_job)
    return DeleteResourceOut(
        resource_id=resource_id,
        state="deleted",
        deleted=True,
        vector_deleted=True,
        object_refs=object_refs,
    )


@router.post("/upsert", response_model=UpsertResourceOut)
async def upsert_processed_resource(
    body: UpsertResourceIn,
    session: AsyncSession = Depends(get_db),
):
    if not body.source_files:
        raise HTTPException(status_code=400, detail="source_files must not be empty")

    embedding_text = _embedding_text(body)
    if not embedding_text:
        raise HTTPException(status_code=400, detail="description or title is required for embedding")

    try:
        vector = await generate_embedding(embedding_text)
    except Exception as exc:
        logger.error("Embedding generation failed for %s/%s: %s", body.client_id, body.client_resource_id, exc)
        raise HTTPException(status_code=502, detail=f"embedding failed: {exc}") from exc

    if len(vector) != settings.embedding_dimension:
        raise HTTPException(
            status_code=502,
            detail=f"dimension mismatch: expected {settings.embedding_dimension}, got {len(vector)}",
        )

    task = await _find_task(session, body.client_id, body.client_resource_id)
    created = task is None
    if task is None:
        task = ResourceTask(
            resource_id=f"res-{uuid.uuid4().hex[:16]}",
            process_state="committed",
            idempotency_key=body.idempotency_key or f"upsert-{uuid.uuid4().hex[:12]}",
            content_md5=_content_fingerprint(body.source_object, body.source_files),
            resource_type=body.resource_type,
            source=body.client_id,
            source_resource_id=body.client_resource_id,
        )
        session.add(task)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            task = await _find_task(session, body.client_id, body.client_resource_id)
            if task is None:
                raise
            created = False

    task.content_md5 = _content_fingerprint(body.source_object, body.source_files)
    task.resource_type = body.resource_type
    task.source = body.client_id
    task.source_resource_id = body.client_resource_id
    task.title = _first_non_empty(body.title, _metadata_title(body.client_metadata), _object_file_name(body.source_object), task.title)
    task.category = body.classification.category
    task.tags_json = _json(body.classification.tags)
    task.client_metadata_json = _json(body.client_metadata)
    task.process_state = "committed"
    task.source_storage_profile_id = body.source_object.storage_profile_id
    task.source_object_key = body.source_object.object_key
    task.source_object_file_name = _object_file_name(body.source_object)
    task.source_object_file_format = _file_format(body.source_object)
    task.source_object_file_size = int(body.source_object.size or 0)
    task.source_object_checksum = body.source_object.checksum
    task.source_object_etag = body.source_object.etag
    if body.package_object is not None:
        task.package_storage_profile_id = body.package_object.storage_profile_id
        task.package_object_key = body.package_object.object_key
    else:
        task.package_storage_profile_id = ""
        task.package_object_key = ""
    task.last_error_code = ""
    task.last_error_message = ""
    task.vector_state = "pending"
    task.vector_error = ""

    await session.execute(delete(ResourceFile).where(ResourceFile.task_id == task.id))
    await session.execute(delete(ResourcePreview).where(ResourcePreview.task_id == task.id))
    await session.execute(delete(ResourceDescription).where(ResourceDescription.task_id == task.id))
    await session.execute(delete(ResourceEmbedding).where(ResourceEmbedding.task_id == task.id))

    for index, item in enumerate(body.source_files):
        name = _source_file_name(item)
        session.add(ResourceFile(
            task_id=task.id,
            file_path=item.path_in_package or name,
            file_name=name,
            file_size=int(item.file_size or 0),
            file_format=_source_file_format(item),
            content_md5=item.checksum or "",
            file_role="main",
            storage_profile_id="",
            object_key="",
            path_in_package=item.path_in_package,
            content_type="",
            etag="",
            is_primary=item.is_primary or index == 0,
        ))

    for item in body.previews:
        session.add(ResourcePreview(
            task_id=task.id,
            strategy=item.strategy,
            role=item.role,
            path=item.object_key,
            format=(item.object_key.rsplit(".", 1)[-1].lower() if "." in item.object_key else ""),
            width=item.width,
            height=item.height,
            size=item.size,
            storage_profile_id=item.storage_profile_id,
            object_key=item.object_key,
            content_type="",
            origin=item.origin,
            renderer=item.renderer,
        ))

    prompt_version = _first_non_empty(
        body.processing.description_prompt_version,
        body.processing.pipeline_version,
    )
    session.add(ResourceDescription(
        task_id=task.id,
        main_content=body.description.summary,
        detail_content=body.description.detail,
        full_description=body.description.full,
        prompt_version=prompt_version,
        usage_space=body.classification.usage_space,
        usage_category=body.classification.usage_category,
        usage_subcategories_json=_json(body.classification.usage_subcategories),
        usage_classification_reason=body.classification.usage_classification_reason,
        usage_classification_suggestion_json=_json(body.classification.usage_classification_suggestion),
        usage_classification_version=body.classification.usage_classification_version,
    ))

    model_version = get_model_version()
    session.add(ResourceEmbedding(
        task_id=task.id,
        dimension=len(vector),
        checksum=hashlib.sha256(embedding_text.encode("utf-8")).hexdigest(),
        model_version=model_version,
    ))

    session.add(ProcessLog(
        task_id=task.id,
        event="upserted" if not created else "upsert_created",
        detail=f"client_id={body.client_id}, client_resource_id={body.client_resource_id}",
    ))
    await session.flush()

    resource_id = task.resource_id or ""
    vector_job = _add_vector_sync_job(
        session,
        resource_id=resource_id,
        action="upsert",
        resource_type=body.resource_type,
        vector=vector,
    )
    await session.commit()
    await _run_vector_sync_job(session, vector_job)
    return UpsertResourceOut(
        resource_id=resource_id,
        state="committed",
        embedding_model=model_version,
    )
