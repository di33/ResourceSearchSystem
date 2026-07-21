"""Processed-resource ingestion endpoints used by the processing server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from resource_contracts.file_structure import validate_file_structure
from sqlalchemy import delete, func, insert, literal_column, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.config import settings
from app.deps import async_session_factory, get_db, get_milvus
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
from app.services.embedding_client import generate_embedding, generate_embeddings, get_model_version
from resource_contracts.resource_types import is_search_indexable_resource_type, normalize_resource_type

logger = logging.getLogger(__name__)
_vector_sync_worker_tasks: list[asyncio.Task] = []
_vector_sync_worker_stop: asyncio.Event | None = None
_fts_worker_task: asyncio.Task | None = None
_fts_worker_stop: asyncio.Event | None = None

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


class FileStructureEntryIn(BaseModel):
    path: str
    name: str
    type: str = "file"
    size: int = 0
    format: str = ""
    checksum: str = ""
    is_primary: bool = False


class FileStructureIn(BaseModel):
    source: str = "processor"
    state: str = "complete"
    source_object_checksum: str = ""
    entry_count: int = 0
    total_size: int = 0
    entries: list[FileStructureEntryIn] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, value):
        return validate_file_structure(value)


class PreviewRefIn(BaseModel):
    role: str = "primary"
    storage_profile_id: str = ""
    object_key: str
    width: int | None = None
    height: int | None = None
    size: int | None = None
    checksum: str = ""
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
    title: str = ""
    client_metadata: dict[str, Any] = Field(default_factory=dict)
    source_object: ObjectRefIn
    file_structure: FileStructureIn
    package_object: ObjectRefIn | None = None
    previews: list[PreviewRefIn] = Field(default_factory=list)
    description: DescriptionIn = Field(default_factory=DescriptionIn)
    classification: ClassificationIn = Field(default_factory=ClassificationIn)
    processing: ProcessingIn = Field(default_factory=ProcessingIn)

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


def _source_file_name(ref: FileStructureEntryIn) -> str:
    return ref.name or "resource"


def _source_file_format(ref: FileStructureEntryIn) -> str:
    if ref.format:
        return ref.format.lower().lstrip(".")
    name = _source_file_name(ref)
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _content_fingerprint(source_object: ObjectRefIn, file_structure: FileStructureIn) -> str:
    payload = {
        "source_object": {
            "storage_profile_id": source_object.storage_profile_id,
            "object_key": source_object.object_key,
            "checksum": source_object.checksum,
            "size": source_object.size,
        },
        "file_structure": [
            {
                "file_name": item.name,
                "path": item.path,
                "checksum": item.checksum,
                "file_size": item.size,
            }
            for item in file_structure.entries
        ],
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _embedding_text(body: UpsertResourceIn) -> str:
    parts = [
        body.title,
        str(body.client_metadata.get("display_title") or ""),
        str(body.client_metadata.get("action_name") or ""),
        str(body.client_metadata.get("resource_path") or ""),
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
    *,
    load_relationships: bool = True,
) -> ResourceTask | None:
    query = (
        select(ResourceTask)
        .where(
            ResourceTask.source == client_id,
            ResourceTask.source_resource_id == client_resource_id,
        )
        .order_by(ResourceTask.updated_at.desc(), ResourceTask.id.desc())
    )
    if not load_relationships:
        query = query.options(
            noload(ResourceTask.files),
            noload(ResourceTask.previews),
            noload(ResourceTask.descriptions),
            noload(ResourceTask.embeddings),
            noload(ResourceTask.logs),
        )
    return (await session.execute(query)).scalars().first()


def _write_vector(resource_id: str, resource_type: str, vector: list[float]) -> str:
    return _write_vectors([{
        "resource_id": resource_id,
        "vector": vector,
        "resource_type": resource_type,
    }])


def _write_vectors(data: list[dict[str, Any]]) -> str:
    if not data:
        return ""
    milvus = get_milvus()
    try:
        if hasattr(milvus, "upsert"):
            milvus.upsert(collection_name=settings.milvus_collection, data=data)
        else:
            if hasattr(milvus, "delete"):
                for item in data:
                    resource_id = str(item.get("resource_id") or "")
                    milvus.delete(
                        collection_name=settings.milvus_collection,
                        filter=f'resource_id == "{resource_id}"',
                    )
            milvus.insert(collection_name=settings.milvus_collection, data=data)
    except Exception as exc:
        logger.error("Milvus vector write failed for %d item(s): %s", len(data), exc)
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
    embedding_text: str = "",
) -> VectorSyncJob:
    job = VectorSyncJob(
        resource_id=resource_id,
        action=action,
        resource_type=resource_type,
        vector_json=json.dumps(vector or [], separators=(",", ":")),
        embedding_text=embedding_text,
        state="pending",
    )
    session.add(job)
    return job


async def _supersede_pending_upsert_jobs(session: AsyncSession, resource_id: str) -> None:
    if not resource_id:
        return
    jobs = (
        await session.execute(
            select(VectorSyncJob)
            .where(VectorSyncJob.resource_id == resource_id)
            .where(VectorSyncJob.action == "upsert")
            .where(VectorSyncJob.state.in_(["pending", "running", "failed"]))
            .order_by(VectorSyncJob.id)
        )
    ).scalars().all()
    for existing in jobs:
        existing.state = "superseded"
        existing.last_error = ""


async def _is_latest_upsert_job(session: AsyncSession, job: VectorSyncJob) -> bool:
    if job.action != "upsert":
        return True
    latest_id = (
        await session.execute(
            select(VectorSyncJob.id)
            .where(VectorSyncJob.resource_id == job.resource_id)
            .where(VectorSyncJob.action == "upsert")
            .order_by(VectorSyncJob.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return latest_id is None or int(job.id or 0) >= int(latest_id)


async def _fail_vector_sync_job(
    session: AsyncSession,
    job: VectorSyncJob,
    error: str,
    *,
    raise_on_error: bool,
) -> bool:
    job.state = "failed"
    job.last_error = error[:2000]
    job.retry_after = _utcnow() + timedelta(seconds=max(1, settings.vector_sync_failed_retry_seconds))
    await _update_task_vector_state(
        session,
        job.resource_id,
        "failed",
        error,
        last_error_code="VECTOR_SYNC_FAILED",
        last_error_message=error,
    )
    await session.commit()
    if raise_on_error:
        raise HTTPException(status_code=502, detail=error)
    return False


async def _mark_vector_sync_superseded(session: AsyncSession, job: VectorSyncJob) -> bool:
    job.state = "superseded"
    job.last_error = ""
    job.retry_after = _utcnow()
    await session.commit()
    return True


async def _resource_task_id(session: AsyncSession, resource_id: str) -> int | None:
    return (
        await session.execute(
            select(ResourceTask.id).where(ResourceTask.resource_id == resource_id)
        )
    ).scalar_one_or_none()


async def _update_task_vector_state(
    session: AsyncSession,
    resource_id: str,
    state: str,
    error: str = "",
    *,
    task_id: int | None = None,
    last_error_code: str | None = None,
    last_error_message: str | None = None,
) -> None:
    values: dict[str, Any] = {
        "vector_state": state,
        "vector_error": error[:2000],
        "updated_at": _utcnow(),
    }
    # A successful vector sync supersedes any earlier transient failure. Keep
    # all task-level error fields consistent so the resource detail page does
    # not continue to display a stale VECTOR_SYNC_FAILED message.
    if state == "synced":
        values["last_error_code"] = ""
        values["last_error_message"] = ""
    if last_error_code is not None:
        values["last_error_code"] = last_error_code
    if last_error_message is not None:
        values["last_error_message"] = last_error_message[:2000]

    query = update(ResourceTask).values(**values)
    if task_id is not None:
        query = query.where(ResourceTask.id == task_id)
    else:
        query = query.where(ResourceTask.resource_id == resource_id)
    await session.execute(query)


async def _run_vector_sync_job(
    session: AsyncSession,
    job: VectorSyncJob,
    *,
    raise_on_error: bool = True,
) -> bool:
    error = ""
    embedding_checksum = ""
    if job.action == "upsert":
        if not await _is_latest_upsert_job(session, job):
            return await _mark_vector_sync_superseded(session, job)

        task_id = await _resource_task_id(session, job.resource_id)
        if task_id is None:
            return await _mark_vector_sync_superseded(session, job)

        job.state = "running"
        job.last_error = ""
        await _update_task_vector_state(session, job.resource_id, "running", "", task_id=task_id)
        await session.commit()

        embedding_text = str(job.embedding_text or "").strip()
        if embedding_text:
            try:
                vector = await generate_embedding(embedding_text)
            except Exception as exc:
                logger.error("Embedding generation failed for vector job %s: %s", job.id, exc)
                return await _fail_vector_sync_job(
                    session,
                    job,
                    f"embedding failed: {exc}",
                    raise_on_error=raise_on_error,
                )
            embedding_checksum = hashlib.sha256(embedding_text.encode("utf-8")).hexdigest()
        else:
            try:
                vector = json.loads(job.vector_json or "[]")
            except Exception as exc:
                return await _fail_vector_sync_job(
                    session,
                    job,
                    f"stored vector is invalid: {exc}",
                    raise_on_error=raise_on_error,
                )
            if not vector:
                return await _fail_vector_sync_job(
                    session,
                    job,
                    "embedding_text and stored vector are empty",
                    raise_on_error=raise_on_error,
                )
            embedding_checksum = hashlib.sha256(
                json.dumps(vector, separators=(",", ":")).encode("utf-8")
            ).hexdigest()

        if len(vector) != settings.embedding_dimension:
            return await _fail_vector_sync_job(
                session,
                job,
                f"dimension mismatch: expected {settings.embedding_dimension}, got {len(vector)}",
                raise_on_error=raise_on_error,
            )

        if not await _is_latest_upsert_job(session, job):
            return await _mark_vector_sync_superseded(session, job)

        error = await asyncio.to_thread(_write_vector, job.resource_id, job.resource_type, vector)
    elif job.action == "delete":
        error = await asyncio.to_thread(_delete_vector, job.resource_id)
    else:
        error = f"unsupported vector sync action: {job.action}"

    if error:
        return await _fail_vector_sync_job(
            session,
            job,
            error,
            raise_on_error=raise_on_error,
        )

    job.state = "completed"
    job.last_error = ""
    task_id = await _resource_task_id(session, job.resource_id)
    if task_id is not None and job.action == "upsert":
        await session.execute(delete(ResourceEmbedding).where(ResourceEmbedding.task_id == task_id))
        session.add(ResourceEmbedding(
            task_id=task_id,
            dimension=settings.embedding_dimension,
            checksum=embedding_checksum,
            model_version=get_model_version(),
        ))
        await _update_task_vector_state(session, job.resource_id, "synced", "", task_id=task_id)
    await session.commit()
    return True


async def _run_vector_sync_job_by_id(job_id: int) -> None:
    async with async_session_factory() as session:
        job = await _claim_vector_sync_job(session, job_id)
        if job is None:
            return
        try:
            await _run_vector_sync_job(session, job, raise_on_error=False)
        except Exception:
            logger.exception("Unhandled vector sync background failure for job %s", job_id)


async def _run_vector_sync_jobs_batch(job_ids: list[int]) -> int:
    if not job_ids:
        return 0

    async with async_session_factory() as session:
        jobs = (
            await session.execute(
                select(VectorSyncJob)
                .where(VectorSyncJob.id.in_(job_ids))
                .order_by(VectorSyncJob.id)
            )
        ).scalars().all()

        batch_items: list[tuple[VectorSyncJob, int, str]] = []
        fallback_ids: list[int] = []
        for job in jobs:
            if job.action != "upsert" or not str(job.embedding_text or "").strip():
                fallback_ids.append(int(job.id))
                continue
            if not await _is_latest_upsert_job(session, job):
                await _mark_vector_sync_superseded(session, job)
                continue
            task_id = await _resource_task_id(session, job.resource_id)
            if task_id is None:
                await _mark_vector_sync_superseded(session, job)
                continue

            text = str(job.embedding_text or "").strip()
            job.state = "running"
            job.last_error = ""
            await _update_task_vector_state(session, job.resource_id, "running", "", task_id=task_id)
            batch_items.append((job, task_id, text))
        await session.commit()

    processed = 0
    if batch_items:
        texts = [item[2] for item in batch_items]
        try:
            vectors = await generate_embeddings(texts)
        except Exception as exc:
            logger.error("Batch embedding generation failed for %d vector jobs: %s", len(batch_items), exc)
            async with async_session_factory() as session:
                for job, _task_id, _text in batch_items:
                    db_job = await session.get(VectorSyncJob, int(job.id))
                    if db_job is not None:
                        await _fail_vector_sync_job(
                            session,
                            db_job,
                            f"embedding failed: {exc}",
                            raise_on_error=False,
                        )
            return 0

        valid_rows: list[tuple[VectorSyncJob, int, str, list[float]]] = []
        async with async_session_factory() as session:
            for (job, _task_id, text), vector in zip(batch_items, vectors):
                db_job = await session.get(VectorSyncJob, int(job.id))
                if db_job is None:
                    continue
                task_id = await _resource_task_id(session, db_job.resource_id)
                if task_id is None:
                    await _mark_vector_sync_superseded(session, db_job)
                    continue
                if len(vector) != settings.embedding_dimension:
                    await _fail_vector_sync_job(
                        session,
                        db_job,
                        f"dimension mismatch: expected {settings.embedding_dimension}, got {len(vector)}",
                        raise_on_error=False,
                    )
                    continue
                if not await _is_latest_upsert_job(session, db_job):
                    await _mark_vector_sync_superseded(session, db_job)
                    continue
                valid_rows.append((db_job, task_id, text, vector))

            error = await asyncio.to_thread(
                _write_vectors,
                [
                    {
                        "resource_id": job.resource_id,
                        "resource_type": job.resource_type,
                        "vector": vector,
                    }
                    for job, _task_id, _text, vector in valid_rows
                ],
            )
            if error:
                for job, _task_id, _text, _vector in valid_rows:
                    await _fail_vector_sync_job(session, job, error, raise_on_error=False)
                return 0

            for job, task_id, text, _vector in valid_rows:
                job.state = "completed"
                job.last_error = ""
                await session.execute(delete(ResourceEmbedding).where(ResourceEmbedding.task_id == task_id))
                session.add(ResourceEmbedding(
                    task_id=task_id,
                    dimension=settings.embedding_dimension,
                    checksum=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    model_version=get_model_version(),
                ))
                await _update_task_vector_state(session, job.resource_id, "synced", "", task_id=task_id)
                processed += 1
            await session.commit()

    for job_id in fallback_ids:
        async with async_session_factory() as session:
            job = await session.get(VectorSyncJob, job_id)
            if job is None:
                continue
            try:
                if await _run_vector_sync_job(session, job, raise_on_error=False):
                    processed += 1
            except Exception:
                logger.exception("Unhandled vector sync worker failure for job %s", job_id)
    return processed


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _claim_vector_sync_job(session: AsyncSession, job_id: int) -> VectorSyncJob | None:
    now = _utcnow()
    stale_before = now - timedelta(seconds=max(1, settings.vector_sync_worker_stale_seconds))
    result = await session.execute(
        update(VectorSyncJob)
        .where(VectorSyncJob.id == job_id)
        .where(
            (VectorSyncJob.state == "pending")
            | ((VectorSyncJob.state == "failed") & (VectorSyncJob.retry_after <= now))
            | ((VectorSyncJob.state == "running") & (VectorSyncJob.updated_at < stale_before))
        )
        .values(state="running", last_error="", updated_at=now)
        .returning(VectorSyncJob.id)
    )
    claimed_id = result.scalar_one_or_none()
    await session.commit()
    if claimed_id is None:
        return None
    return await session.get(VectorSyncJob, int(claimed_id))


async def _claim_next_vector_sync_jobs(session: AsyncSession, limit: int) -> list[int]:
    now = _utcnow()
    stale_before = now - timedelta(seconds=max(1, settings.vector_sync_worker_stale_seconds))
    batch_limit = max(1, limit)
    job_ids: list[int] = []

    # Keep the hot path index-friendly. Folding stale running jobs into this
    # query with OR makes Postgres scan past every completed row on large queues.
    pending_result = await session.execute(
        select(VectorSyncJob.id)
        .where(VectorSyncJob.state == "pending")
        .order_by(VectorSyncJob.id)
        .limit(batch_limit)
        .with_for_update(skip_locked=True)
    )
    job_ids.extend(int(job_id) for job_id in pending_result.scalars().all())

    remaining = batch_limit - len(job_ids)
    if remaining > 0:
        failed_result = await session.execute(
            select(VectorSyncJob.id)
            .where(VectorSyncJob.state == "failed")
            .where(VectorSyncJob.retry_after <= now)
            .order_by(VectorSyncJob.retry_after, VectorSyncJob.id)
            .limit(remaining)
            .with_for_update(skip_locked=True)
        )
        job_ids.extend(int(job_id) for job_id in failed_result.scalars().all())

    remaining = batch_limit - len(job_ids)
    if remaining > 0:
        stale_query = (
            select(VectorSyncJob.id)
            .where(VectorSyncJob.state == "running")
            .where(VectorSyncJob.updated_at < stale_before)
            .order_by(VectorSyncJob.id)
            .limit(remaining)
            .with_for_update(skip_locked=True)
        )
        if job_ids:
            stale_query = stale_query.where(VectorSyncJob.id.not_in(job_ids))
        stale_result = await session.execute(stale_query)
        job_ids.extend(int(job_id) for job_id in stale_result.scalars().all())

    if not job_ids:
        await session.rollback()
        return []

    await session.execute(
        update(VectorSyncJob)
        .where(VectorSyncJob.id.in_(job_ids))
        .values(state="running", last_error="", updated_at=now)
    )
    await session.commit()
    return job_ids


async def _drain_vector_sync_jobs_once(limit: int | None = None) -> int:
    batch_size = max(1, int(limit or settings.vector_sync_worker_batch_size))
    async with async_session_factory() as session:
        job_ids = await _claim_next_vector_sync_jobs(session, batch_size)
    return await _run_vector_sync_jobs_batch(job_ids)


async def _backfill_fts_once(limit: int | None = None, session: AsyncSession | None = None) -> int:
    batch_size = max(1, int(limit or settings.fts_worker_batch_size))
    if session is not None:
        result = await session.execute(
            select(ResourceDescription.id)
            .where(ResourceDescription.search_vector.is_(None))
            .order_by(ResourceDescription.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        ids = [int(row_id) for row_id in result.scalars().all()]
        if not ids:
            await session.rollback()
            return 0
        dialect_name = session.bind.dialect.name if session.bind is not None else ""
        if dialect_name == "postgresql":
            values = {
                "search_vector": func.setweight(
                    func.to_tsvector(
                        settings.search_text_config,
                        func.coalesce(ResourceDescription.full_description, ""),
                    ),
                    literal_column("'A'"),
                )
            }
        else:
            values = {"search_vector": ResourceDescription.full_description}
        await session.execute(
            update(ResourceDescription)
            .where(ResourceDescription.id.in_(ids))
            .values(**values)
        )
        await session.commit()
        return len(ids)
    async with async_session_factory() as owned_session:
        return await _backfill_fts_once(batch_size, owned_session)


async def _fts_worker_loop(stop_event: asyncio.Event) -> None:
    interval = max(0.2, float(settings.fts_worker_interval))
    logger.info(
        "FTS worker started: interval=%.2fs batch=%d",
        interval,
        settings.fts_worker_batch_size,
    )
    try:
        while not stop_event.is_set():
            try:
                processed = await _backfill_fts_once()
                if processed:
                    logger.info("FTS worker processed %d row(s)", processed)
            except Exception:
                logger.exception("FTS worker loop failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("FTS worker stopped")


async def _vector_sync_worker_loop(stop_event: asyncio.Event, worker_id: int = 1) -> None:
    interval = max(0.2, float(settings.vector_sync_worker_interval))
    logger.info(
        "Vector sync worker %d started: interval=%.2fs batch=%d stale=%ds",
        worker_id,
        interval,
        settings.vector_sync_worker_batch_size,
        settings.vector_sync_worker_stale_seconds,
    )
    try:
        while not stop_event.is_set():
            try:
                processed = await _drain_vector_sync_jobs_once()
                if processed:
                    logger.info("Vector sync worker %d processed %d job(s)", worker_id, processed)
            except Exception:
                logger.exception("Vector sync worker %d loop failed", worker_id)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("Vector sync worker %d stopped", worker_id)


def start_vector_sync_worker() -> None:
    global _vector_sync_worker_tasks, _vector_sync_worker_stop
    if not settings.vector_sync_worker_enabled:
        logger.info("Vector sync worker disabled")
        return
    if any(not task.done() for task in _vector_sync_worker_tasks):
        return
    _vector_sync_worker_stop = asyncio.Event()
    worker_count = max(1, int(settings.vector_sync_worker_concurrency))
    _vector_sync_worker_tasks = [
        asyncio.create_task(_vector_sync_worker_loop(_vector_sync_worker_stop, worker_id))
        for worker_id in range(1, worker_count + 1)
    ]


def start_fts_worker() -> None:
    global _fts_worker_task, _fts_worker_stop
    if not settings.fts_worker_enabled:
        logger.info("FTS worker disabled")
        return
    if _fts_worker_task is not None and not _fts_worker_task.done():
        return
    _fts_worker_stop = asyncio.Event()
    _fts_worker_task = asyncio.create_task(_fts_worker_loop(_fts_worker_stop))


async def stop_vector_sync_worker() -> None:
    global _vector_sync_worker_tasks, _vector_sync_worker_stop
    tasks = list(_vector_sync_worker_tasks)
    stop_event = _vector_sync_worker_stop
    _vector_sync_worker_tasks = []
    _vector_sync_worker_stop = None
    if not tasks:
        return
    if stop_event is not None:
        stop_event.set()
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def stop_fts_worker() -> None:
    global _fts_worker_task, _fts_worker_stop
    task = _fts_worker_task
    stop_event = _fts_worker_stop
    _fts_worker_task = None
    _fts_worker_stop = None
    if task is None:
        return
    if stop_event is not None:
        stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=10)
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
            if await _run_vector_sync_job(session, job):
                synced += 1
            else:
                failed += 1
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
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
):
    started_at = time.perf_counter()
    timings: dict[str, float] = {}

    def mark(name: str) -> None:
        timings[name] = round((time.perf_counter() - started_at) * 1000, 1)

    if not is_search_indexable_resource_type(body.resource_type):
        raise HTTPException(status_code=400, detail=f"resource_type {body.resource_type!r} is not search-indexable")
    if not body.file_structure.entries:
        raise HTTPException(status_code=400, detail="file_structure.entries must not be empty")

    embedding_text = _embedding_text(body)
    if not embedding_text:
        raise HTTPException(status_code=400, detail="description or title is required for embedding")

    task = await _find_task(session, body.client_id, body.client_resource_id, load_relationships=False)
    mark("find_task")
    created = task is None
    if task is None:
        task = ResourceTask(
            resource_id=f"res-{uuid.uuid4().hex[:16]}",
            process_state="committed",
            idempotency_key=body.idempotency_key or f"upsert-{uuid.uuid4().hex[:12]}",
            content_md5=_content_fingerprint(body.source_object, body.file_structure),
            resource_type=body.resource_type,
            source=body.client_id,
            source_resource_id=body.client_resource_id,
        )
        session.add(task)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            task = await _find_task(session, body.client_id, body.client_resource_id, load_relationships=False)
            if task is None:
                raise
            created = False
    mark("ensure_task")

    task.content_md5 = _content_fingerprint(body.source_object, body.file_structure)
    task.resource_type = body.resource_type
    task.source = body.client_id
    task.source_resource_id = body.client_resource_id
    task.title = _first_non_empty(body.title, _object_file_name(body.source_object), task.title)
    task.category = body.classification.category
    task.tags_json = _json(body.classification.tags)
    task.client_metadata_json = _json(body.client_metadata or None)
    task.process_state = "committed"
    task.source_storage_profile_id = body.source_object.storage_profile_id
    task.source_object_key = body.source_object.object_key
    task.source_object_file_name = _object_file_name(body.source_object)
    task.source_object_file_format = _file_format(body.source_object)
    task.source_object_file_size = int(body.source_object.size or 0)
    task.source_object_checksum = body.source_object.checksum
    task.source_object_etag = ""
    task.file_structure_source = body.file_structure.source
    task.file_structure_state = body.file_structure.state
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
    mark("delete_relations")

    file_rows = []
    for index, item in enumerate(body.file_structure.entries):
        name = _source_file_name(item)
        file_rows.append({
            "task_id": task.id,
            "file_path": item.path or name,
            "file_name": name,
            "file_size": int(item.size or 0),
            "file_format": _source_file_format(item),
            "content_md5": item.checksum or "",
            "file_role": "main",
            "storage_profile_id": "",
            "object_key": "",
            "path_in_package": item.path,
            "content_type": "",
            "etag": "",
            "is_primary": item.is_primary or index == 0,
        })
    if file_rows:
        await session.execute(insert(ResourceFile), file_rows)
    mark("insert_files")

    preview_rows = [
        {
            "task_id": task.id,
            "strategy": item.strategy,
            "role": item.role,
            "path": item.object_key,
            "format": (item.object_key.rsplit(".", 1)[-1].lower() if "." in item.object_key else ""),
            "width": item.width,
            "height": item.height,
            "size": item.size,
            "storage_profile_id": item.storage_profile_id,
            "object_key": item.object_key,
            "content_type": "",
            "origin": item.origin,
            "renderer": item.renderer,
        }
        for item in body.previews
    ]
    if preview_rows:
        await session.execute(insert(ResourcePreview), preview_rows)
    mark("insert_previews")

    prompt_version = _first_non_empty(
        body.processing.description_prompt_version,
        body.processing.pipeline_version,
    )
    await session.execute(insert(ResourceDescription).values(
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
    mark("insert_description")

    await session.execute(insert(ProcessLog).values(
        task_id=task.id,
        event="upserted" if not created else "upsert_created",
        detail=f"client_id={body.client_id}, client_resource_id={body.client_resource_id}",
    ))
    mark("insert_log")

    resource_id = task.resource_id or ""
    await _supersede_pending_upsert_jobs(session, resource_id)
    mark("supersede_vectors")
    _add_vector_sync_job(
        session,
        resource_id=resource_id,
        action="upsert",
        resource_type=body.resource_type,
        embedding_text=embedding_text,
    )
    await session.commit()
    mark("commit")
    total_ms = timings["commit"]
    if total_ms >= 1000:
        logger.info(
            "Slow resource upsert %.1fms client_resource_id=%s timings=%s",
            total_ms,
            body.client_resource_id,
            timings,
        )
    return UpsertResourceOut(
        resource_id=resource_id,
        state="committed",
        embedding_model=get_model_version(),
    )
