"""Real implementation of BaseCloudClient backed by PostgreSQL + KS3."""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from CloudService.cloud_client import (
    MULTIPART_THRESHOLD,
    BaseCloudClient,
    CommitRequest,
    CommitResponse,
    FileInfo,
    PreviewFileInfo,
    RegisterRequest,
    RegisterResponse,
    UploadResult,
)
from app.models.tables import (
    ProcessLog,
    ResourceDescription,
    ResourceEmbedding,
    ResourceFile,
    ResourcePreview,
    ResourceTask,
)
from app.services.ks3_storage import KS3Storage

logger = logging.getLogger(__name__)


class PgCloudClient(BaseCloudClient):
    """Cloud client that persists to PostgreSQL and stores blobs in KS3."""

    def __init__(self, session: AsyncSession, storage: KS3Storage, milvus_client=None):
        self.session = session
        self.storage = storage
        self.milvus = milvus_client

    def _merge_task_metadata(self, task: ResourceTask, request: RegisterRequest) -> None:
        if request.source_resource_id and not task.source_resource_id:
            task.source_resource_id = request.source_resource_id
        if request.source and not task.source:
            task.source = request.source
        if request.pack_name and not task.pack_name:
            task.pack_name = request.pack_name
        if request.title and not task.title:
            task.title = request.title
        if request.resource_path and not task.resource_path:
            task.resource_path = request.resource_path
        if request.source_url and not task.source_url:
            task.source_url = request.source_url
        if request.original_download_url and not task.original_download_url:
            task.original_download_url = request.original_download_url
        if request.category and not task.category:
            task.category = request.category
        if request.license_name and not task.license_name:
            task.license_name = request.license_name
        if request.source_description and not task.source_description:
            task.source_description = request.source_description
        if request.tags and task.tags_json in ("", "[]"):
            task.tags_json = json.dumps(request.tags, ensure_ascii=False)
        if request.parent_source_resource_id and not task.parent_source_resource_id:
            task.parent_source_resource_id = request.parent_source_resource_id
        if request.child_source_resource_ids and task.child_source_resource_ids_json in ("", "[]"):
            task.child_source_resource_ids_json = json.dumps(request.child_source_resource_ids, ensure_ascii=False)
        if request.child_resource_count and not task.child_resource_count:
            task.child_resource_count = request.child_resource_count
        if request.contains_resource_types and task.contains_resource_types_json in ("", "[]"):
            task.contains_resource_types_json = json.dumps(request.contains_resource_types, ensure_ascii=False)
        if request.download_file_name and not task.download_file_name:
            task.download_file_name = request.download_file_name
        if request.download_content_type and not task.download_content_type:
            task.download_content_type = request.download_content_type
        if request.download_file_size and not task.download_file_size:
            task.download_file_size = request.download_file_size

    async def _backfill_relationships(self, task: ResourceTask) -> None:
        if task.parent_source_resource_id:
            parent = (
                await self.session.execute(
                    select(ResourceTask).where(ResourceTask.source_resource_id == task.parent_source_resource_id)
                )
            ).scalar_one_or_none()
            task.parent_resource_id = parent.resource_id if parent else None

        child_ids: list[str] = []
        child_source_ids = []
        try:
            value = json.loads(task.child_source_resource_ids_json or "[]")
            if isinstance(value, list):
                child_source_ids = [str(v) for v in value]
        except json.JSONDecodeError:
            child_source_ids = []
        if child_source_ids:
            rows = (
                await self.session.execute(
                    select(ResourceTask).where(ResourceTask.source_resource_id.in_(child_source_ids))
                )
            ).scalars().all()
            mapping = {row.source_resource_id: row.resource_id for row in rows if row.resource_id}
            child_ids = [mapping[source_id] for source_id in child_source_ids if mapping.get(source_id)]
        task.child_resource_ids_json = json.dumps(child_ids, ensure_ascii=False)

        related = (
            await self.session.execute(
                select(ResourceTask).where(
                    (ResourceTask.parent_source_resource_id == task.source_resource_id)
                    | (ResourceTask.source_resource_id == task.parent_source_resource_id)
                )
            )
        ).scalars().all()
        for related_task in related:
            if related_task.id == task.id:
                continue
            if related_task.parent_source_resource_id == task.source_resource_id:
                related_task.parent_resource_id = task.resource_id
            related_child_source_ids = []
            try:
                value = json.loads(related_task.child_source_resource_ids_json or "[]")
                if isinstance(value, list):
                    related_child_source_ids = [str(v) for v in value]
            except json.JSONDecodeError:
                related_child_source_ids = []
            if related_child_source_ids:
                mapping_rows = (
                    await self.session.execute(
                        select(ResourceTask).where(ResourceTask.source_resource_id.in_(related_child_source_ids))
                    )
                ).scalars().all()
                mapping = {row.source_resource_id: row.resource_id for row in mapping_rows if row.resource_id}
                related_task.child_resource_ids_json = json.dumps(
                    [mapping[source_id] for source_id in related_child_source_ids if mapping.get(source_id)],
                    ensure_ascii=False,
                )

    async def register(self, request: RegisterRequest) -> RegisterResponse:
        # Idempotency: check if a task with the same key already exists
        existing = (
            await self.session.execute(
                select(ResourceTask).where(
                    ResourceTask.idempotency_key == request.idempotency_key
                )
            )
        ).scalar_one_or_none()

        if existing:
            self._merge_task_metadata(existing, request)
            await self._backfill_relationships(existing)
            await self.session.flush()
            return RegisterResponse(
                resource_id=existing.resource_id or "",
                exists=True,
                upload_mode=self.determine_upload_mode(request.total_size),
                multipart_chunk_size=10 * 1024 * 1024,
                state=existing.process_state,
            )

        # Check for content-level dedup using composite fingerprint
        dup = (
            await self.session.execute(
                select(ResourceTask).where(
                    ResourceTask.content_md5 == request.content_md5,
                    ResourceTask.process_state == "committed",
                )
            )
        ).scalars().first()

        if dup:
            self._merge_task_metadata(dup, request)
            await self._backfill_relationships(dup)
            await self.session.flush()
            return RegisterResponse(
                resource_id=dup.resource_id or "",
                exists=True,
                upload_mode="direct",
                multipart_chunk_size=10 * 1024 * 1024,
                state="committed",
            )

        resource_id = f"res-{uuid.uuid4().hex[:16]}"
        source_dir = request.files[0].file_path.rsplit("/", 1)[0] if request.files else ""
        if os.sep:
            source_dir = request.files[0].file_path.rsplit(os.sep, 1)[0] if request.files else ""

        task = ResourceTask(
            content_md5=request.content_md5,
            resource_type=request.resource_type,
            source_directory=source_dir,
            source_resource_id=request.source_resource_id,
            source=request.source,
            pack_name=request.pack_name,
            title=request.title,
            resource_path=request.resource_path,
            source_url=request.source_url,
            original_download_url=request.original_download_url,
            category=request.category,
            license_name=request.license_name,
            source_description=request.source_description,
            tags_json=json.dumps(request.tags, ensure_ascii=False),
            parent_source_resource_id=request.parent_source_resource_id,
            child_source_resource_ids_json=json.dumps(request.child_source_resource_ids, ensure_ascii=False),
            child_resource_count=request.child_resource_count,
            contains_resource_types_json=json.dumps(request.contains_resource_types, ensure_ascii=False),
            download_file_name=request.download_file_name,
            download_content_type=request.download_content_type,
            download_file_size=request.download_file_size,
            process_state="registered",
            resource_id=resource_id,
            idempotency_key=request.idempotency_key,
        )
        self.session.add(task)

        # Insert all files
        for file_info in request.files:
            ks3_key = f"files/{resource_id}/{file_info.file_name}"
            db_file = ResourceFile(
                task=task,
                file_path=file_info.file_path,
                file_name=file_info.file_name,
                file_size=file_info.file_size,
                file_format=file_info.file_format,
                content_md5=file_info.content_md5,
                file_role=file_info.file_role,
                ks3_key=ks3_key,
                is_primary=file_info.is_primary,
            )
            self.session.add(db_file)

        self.session.add(
            ProcessLog(task=task, event="registered", detail=f"resource_id={resource_id}, files={len(request.files)}")
        )
        await self.session.flush()
        await self._backfill_relationships(task)

        return RegisterResponse(
            resource_id=resource_id,
            exists=False,
            upload_mode=self.determine_upload_mode(request.total_size),
            multipart_chunk_size=10 * 1024 * 1024,
            state="registered",
        )

    async def upload_files(self, resource_id: str, files: List[FileInfo]) -> UploadResult:
        """Upload all files belonging to a resource to KS3."""
        total_uploaded = 0
        for file_info in files:
            key = f"files/{resource_id}/{file_info.file_name}"
            try:
                uploaded = self.storage.upload_file(key, file_info.file_path)
                total_uploaded += uploaded
            except Exception as exc:
                logger.error("upload_file failed for %s/%s: %s", resource_id, file_info.file_name, exc)
                return UploadResult(success=False, error_message=f"{file_info.file_name}: {exc}")
        return UploadResult(success=True, uploaded_bytes=total_uploaded)

    async def upload_file_obj(self, resource_id: str, filename: str, fileobj, content_type: str) -> UploadResult:
        """Upload from an in-memory file object (used by the HTTP endpoint)."""
        key = f"files/{resource_id}/{filename}"
        try:
            uploaded = self.storage.upload_fileobj(key, fileobj, content_type)
            return UploadResult(success=True, uploaded_bytes=uploaded)
        except Exception as exc:
            logger.error("upload_file_obj failed for %s: %s", resource_id, exc)
            return UploadResult(success=False, error_message=str(exc))

    async def upload_download_obj(self, resource_id: str, filename: str, fileobj, content_type: str) -> UploadResult:
        key = f"downloads/{resource_id}/{filename}"
        try:
            uploaded = self.storage.upload_fileobj(key, fileobj, content_type)
        except Exception as exc:
            logger.error("upload_download_obj failed for %s: %s", resource_id, exc)
            return UploadResult(success=False, error_message=str(exc))

        task = (
            await self.session.execute(
                select(ResourceTask).where(ResourceTask.resource_id == resource_id)
            )
        ).scalar_one_or_none()
        if task is not None:
            task.download_object_key = key
            task.download_file_name = filename
            task.download_content_type = content_type
            task.download_file_size = uploaded
        return UploadResult(success=True, uploaded_bytes=uploaded)

    async def upload_previews(self, resource_id: str, previews: List[PreviewFileInfo]) -> UploadResult:
        """Upload all preview files belonging to a resource to KS3."""
        total_uploaded = 0
        for preview in previews:
            key = f"previews/{resource_id}/{preview.file_name}"
            try:
                uploaded = self.storage.upload_file(key, preview.file_path)
                total_uploaded += uploaded
            except Exception as exc:
                logger.error("upload_preview failed for %s/%s: %s", resource_id, preview.file_name, exc)
                return UploadResult(success=False, error_message=f"{preview.file_name}: {exc}")
        return UploadResult(success=True, uploaded_bytes=total_uploaded)

    async def upload_preview_obj(self, resource_id: str, filename: str, fileobj, content_type: str) -> UploadResult:
        """Upload preview from an in-memory file object."""
        key = f"previews/{resource_id}/{filename}"
        try:
            uploaded = self.storage.upload_fileobj(key, fileobj, content_type)
            return UploadResult(success=True, uploaded_bytes=uploaded)
        except Exception as exc:
            logger.error("upload_preview_obj failed for %s: %s", resource_id, exc)
            return UploadResult(success=False, error_message=str(exc))

    async def commit(self, request: CommitRequest) -> CommitResponse:
        task = (
            await self.session.execute(
                select(ResourceTask).where(ResourceTask.resource_id == request.resource_id)
            )
        ).scalar_one_or_none()

        if task is None:
            return CommitResponse(
                resource_id=request.resource_id,
                state="failed",
                error_message="resource not found",
            )

        # Idempotency
        if task.process_state == "committed":
            return CommitResponse(resource_id=request.resource_id, state="committed")

        # Generate embedding vector on the server side.
        # IMPORTANT: only use main description for vectorization.
        from app.config import settings
        from app.services.embedding_client import generate_embedding, get_model_version

        model_ver = get_model_version()
        embedding_text = (request.description_main or "").strip()
        if not embedding_text:
            return CommitResponse(
                resource_id=request.resource_id,
                state="failed",
                error_message="description_main is required for embedding generation",
            )

        try:
            vector = await generate_embedding(embedding_text)
            if len(vector) != settings.embedding_dimension:
                logger.error(
                    "Embedding dimension mismatch: expected %d, got %d",
                    settings.embedding_dimension, len(vector),
                )
                return CommitResponse(
                    resource_id=request.resource_id,
                    state="failed",
                    error_message=f"dimension mismatch: expected {settings.embedding_dimension}, got {len(vector)}",
                )
        except Exception as exc:
            logger.error("Server-side embedding generation failed for %s: %s", request.resource_id, exc)
            return CommitResponse(
                resource_id=request.resource_id,
                state="failed",
                error_message=f"embedding failed: {exc}",
            )

        desc = ResourceDescription(
            task_id=task.id,
            main_content=request.description_main,
            detail_content=request.description_detail,
            full_description=request.description_full,
            prompt_version="",
        )
        self.session.add(desc)

        # Save embedding metadata
        emb = ResourceEmbedding(
            task_id=task.id,
            dimension=settings.embedding_dimension,
            model_version=model_ver,
        )
        self.session.add(emb)

        # Insert vector into Milvus
        if self.milvus is not None:
            try:
                self.milvus.insert(
                    collection_name=settings.milvus_collection,
                    data=[{
                        "id": task.id,
                        "resource_id": request.resource_id,
                        "vector": vector,
                        "resource_type": request.resource_type,
                    }],
                )
            except Exception as exc:
                logger.error("Milvus insert failed for %s: %s", request.resource_id, exc)
                return CommitResponse(
                    resource_id=request.resource_id,
                    state="failed",
                    error_message=f"vector insert failed: {exc}",
                )

        task.process_state = "committed"
        self.session.add(
            ProcessLog(task=task, event="committed", detail="description + embedding saved")
        )
        await self.session.flush()

        return CommitResponse(resource_id=request.resource_id, state="committed")
