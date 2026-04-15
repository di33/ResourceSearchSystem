"""Resource management endpoints: register / upload-batch / previews / commit / list."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, Form
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select

from app.deps import get_db, get_milvus, get_s3
from app.middleware.auth import require_auth
from app.services.ks3_storage import KS3Storage
from app.services.pg_cloud_client import PgCloudClient
from app.models.tables import (
    ResourceTask,
    ResourceFile,
    ResourcePreview,
    ResourceDescription,
    ResourceEmbedding,
)

router = APIRouter(prefix="/resources", tags=["resources"], dependencies=[Depends(require_auth)])

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class FileInfoIn(BaseModel):
    file_name: str
    file_size: int
    file_format: str
    content_md5: str
    file_role: str = "main"
    is_primary: bool = False


class RegisterBody(BaseModel):
    content_md5: str
    resource_type: str
    files: List[FileInfoIn]
    source_resource_id: str = ""
    parent_source_resource_id: str = ""
    child_source_resource_ids: List[str] = []
    child_resource_count: int = 0
    contains_resource_types: List[str] = []
    title: str = ""
    source: str = ""
    pack_name: str = ""
    resource_path: str = ""
    source_url: str = ""
    original_download_url: str = ""
    category: str = ""
    license_name: str = ""
    source_description: str = ""
    tags: List[str] = []
    download_file_name: str = ""
    download_content_type: str = ""
    download_file_size: int = 0
    idempotency_key: str = ""


class RegisterOut(BaseModel):
    resource_id: str
    exists: bool
    upload_mode: str
    multipart_chunk_size: int
    state: str


class CommitBody(BaseModel):
    resource_type: str
    description_main: str
    description_detail: str
    description_full: str
    idempotency_key: str = ""


class CommitOut(BaseModel):
    resource_id: str
    state: str
    error_message: str = ""


class UploadBatchOut(BaseModel):
    success: bool
    uploaded_bytes: int = 0
    file_count: int = 0
    error_message: str = ""


class PreviewUploadOut(BaseModel):
    success: bool
    uploaded_bytes: int = 0
    preview_count: int = 0
    error_message: str = ""


class ResourceFileOut(BaseModel):
    file_name: str
    file_format: str
    file_size: int
    content_md5: str
    file_role: str
    ks3_key: Optional[str] = None


class ResourcePreviewOut(BaseModel):
    role: str
    strategy: str
    format: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


class ResourceDescriptionOut(BaseModel):
    main_content: str
    detail_content: str
    full_description: str


class ResourceEmbeddingOut(BaseModel):
    dimension: int
    checksum: str
    model_version: str


class ResourceSummaryOut(BaseModel):
    resource_id: Optional[str] = None
    source_resource_id: str = ""
    title: str = ""
    content_md5: str
    resource_type: str
    process_state: str
    file_count: int = 0
    preview_count: int = 0
    has_description: bool = False
    has_embedding: bool = False
    created_at: str = ""
    updated_at: str = ""


class ResourceListOut(BaseModel):
    total: int
    page: int
    page_size: int
    resources: List[ResourceSummaryOut]


class ResourceDetailOut(BaseModel):
    resource_id: Optional[str] = None
    source_resource_id: str = ""
    content_md5: str
    resource_type: str
    process_state: str
    source_directory: str = ""
    source: str = ""
    pack_name: str = ""
    title: str = ""
    resource_path: str = ""
    source_url: str = ""
    original_download_url: str = ""
    category: str = ""
    license_name: str = ""
    source_description: str = ""
    tags: List[str] = []
    parent_resource_id: Optional[str] = None
    child_resource_ids: List[str] = []
    child_resource_count: int = 0
    contains_resource_types: List[str] = []
    download_object_key: str = ""
    download_file_name: str = ""
    download_content_type: str = ""
    download_file_size: int = 0
    created_at: str = ""
    updated_at: str = ""
    files: List[ResourceFileOut] = []
    previews: List[ResourcePreviewOut] = []
    description: Optional[ResourceDescriptionOut] = None
    embedding: Optional[ResourceEmbeddingOut] = None
    last_error: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _MD5Tracker:
    """Wraps a file-like object and computes MD5 during reads."""

    def __init__(self, fp):
        self._fp = fp
        self._hasher = hashlib.md5()

    def read(self, size: int = -1) -> bytes:
        data = self._fp.read(size)
        if data:
            self._hasher.update(data)
        return data

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._fp.seek(offset, whence)

    def tell(self) -> int:
        return self._fp.tell()

    def close(self) -> None:
        if hasattr(self._fp, "close"):
            self._fp.close()

    @property
    def md5_hex(self) -> str:
        return self._hasher.hexdigest()


def _build_client(session: AsyncSession) -> PgCloudClient:
    return PgCloudClient(session, KS3Storage(get_s3()), milvus_client=get_milvus())


def _loads_json_list(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/register", response_model=RegisterOut)
async def register_resource(body: RegisterBody, session: AsyncSession = Depends(get_db)):
    """Register a new resource with multiple files."""
    from CloudService.cloud_client import RegisterRequest, FileInfo

    client = _build_client(session)
    file_infos = [
        FileInfo(
            file_path=f.file_name,
            file_name=f.file_name,
            file_size=f.file_size,
            file_format=f.file_format,
            content_md5=f.content_md5,
            file_role=f.file_role,
            is_primary=f.is_primary,
        )
        for f in body.files
    ]
    req = RegisterRequest(
        content_md5=body.content_md5,
        resource_type=body.resource_type,
        files=file_infos,
        source_resource_id=body.source_resource_id,
        parent_source_resource_id=body.parent_source_resource_id,
        child_source_resource_ids=body.child_source_resource_ids,
        child_resource_count=body.child_resource_count,
        contains_resource_types=body.contains_resource_types,
        title=body.title,
        source=body.source,
        pack_name=body.pack_name,
        resource_path=body.resource_path,
        source_url=body.source_url,
        original_download_url=body.original_download_url,
        category=body.category,
        license_name=body.license_name,
        source_description=body.source_description,
        tags=body.tags,
        download_file_name=body.download_file_name,
        download_content_type=body.download_content_type,
        download_file_size=body.download_file_size,
        idempotency_key=body.idempotency_key,
    )
    resp = await client.register(req)
    await session.commit()
    return RegisterOut(
        resource_id=resp.resource_id,
        exists=resp.exists,
        upload_mode=resp.upload_mode,
        multipart_chunk_size=resp.multipart_chunk_size,
        state=resp.state,
    )


@router.post("/{resource_id}/upload-batch", response_model=UploadBatchOut)
async def upload_files_batch(
    resource_id: str,
    files: List[UploadFile] = File(...),
    download_file: Optional[UploadFile] = File(None),
    session: AsyncSession = Depends(get_db),
):
    """Upload multiple resource files at once with MD5 validation."""
    client = _build_client(session)

    # Fetch registered file records for MD5 lookup
    task = (
        await session.execute(
            select(ResourceTask).where(ResourceTask.resource_id == resource_id)
        )
    ).scalar_one_or_none()
    registered_files = {}
    if task:
        for f in (
            await session.execute(select(ResourceFile).where(ResourceFile.task_id == task.id))
        ).scalars().all():
            registered_files[f.file_name] = f.content_md5

    total_uploaded = 0
    uploaded_file_count = 0
    for file in files:
        filename = file.filename or "upload"
        result = await client.upload_file_obj(
            resource_id,
            filename,
            file.file,
            file.content_type or "application/octet-stream",
        )
        if not result.success:
            await session.commit()
            return UploadBatchOut(
                success=False,
                uploaded_bytes=total_uploaded,
                file_count=uploaded_file_count,
                error_message=f"Failed to upload {filename}: {result.error_message}",
            )

        # MD5 validation via S3 ETag (more reliable than tracking reads)
        expected_md5 = registered_files.get(filename)
        if expected_md5 and result.s3_etag:
            # S3 ETag for non-multipart uploads is the MD5 of the content
            s3_md5 = result.s3_etag.strip('"').split("-")[0]
            if s3_md5 and len(s3_md5) == 32 and s3_md5 != expected_md5:
                await session.commit()
                return UploadBatchOut(
                    success=False,
                    uploaded_bytes=total_uploaded,
                    file_count=uploaded_file_count,
                    error_message=f"MD5 mismatch for {filename}: expected={expected_md5}, actual={s3_md5}",
                )
        total_uploaded += result.uploaded_bytes
        uploaded_file_count += 1

    if download_file is not None:
        result = await client.upload_download_obj(
            resource_id,
            download_file.filename or "download.zip",
            download_file.file,
            download_file.content_type or "application/octet-stream",
        )
        if not result.success:
            await session.commit()
            return UploadBatchOut(
                success=False,
                uploaded_bytes=total_uploaded,
                file_count=uploaded_file_count,
                error_message=f"Failed to upload download object {download_file.filename}: {result.error_message}",
            )
        total_uploaded += result.uploaded_bytes
        uploaded_file_count += 1

    await session.commit()
    return UploadBatchOut(
        success=True,
        uploaded_bytes=total_uploaded,
        file_count=uploaded_file_count,
    )


@router.post("/{resource_id}/previews", response_model=PreviewUploadOut)
async def upload_previews_batch(
    resource_id: str,
    files: List[UploadFile] = File(...),
    roles: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_db),
):
    """Upload multiple preview files at once.

    *roles* is an optional comma-separated list of roles (e.g. "primary,gallery,detail").
    If not provided or fewer roles than files, defaults to "primary" for the first
    file and "gallery" for the rest.
    """
    client = _build_client(session)
    role_list = [r.strip() for r in roles.split(",")] if roles else []
    total_uploaded = 0

    for i, file in enumerate(files):
        result = await client.upload_preview_obj(
            resource_id,
            file.filename or f"preview_{i}",
            file.file,
            file.content_type or "image/webp",
        )
        if not result.success:
            await session.commit()
            return PreviewUploadOut(
                success=False,
                uploaded_bytes=total_uploaded,
                preview_count=len(files),
                error_message=f"Failed to upload preview {file.filename}: {result.error_message}",
            )
        total_uploaded += result.uploaded_bytes

        # Persist preview record
        task = (
            await session.execute(
                select(ResourceTask).where(ResourceTask.resource_id == resource_id)
            )
        ).scalar_one_or_none()
        if task:
            role = role_list[i] if i < len(role_list) else ("primary" if i == 0 else "gallery")
            preview_rec = ResourcePreview(
                task_id=task.id,
                strategy="static",
                role=role,
                path=file.filename or f"preview_{i}",
                renderer="upload",
            )
            session.add(preview_rec)

    await session.commit()
    return PreviewUploadOut(
        success=True,
        uploaded_bytes=total_uploaded,
        preview_count=len(files),
    )


@router.post("/{resource_id}/commit", response_model=CommitOut)
async def commit_resource(
    resource_id: str,
    body: CommitBody,
    session: AsyncSession = Depends(get_db),
):
    """Commit a resource after all files and previews are uploaded."""
    from CloudService.cloud_client import CommitRequest

    client = _build_client(session)
    req = CommitRequest(
        resource_id=resource_id,
        resource_type=body.resource_type,
        description_main=body.description_main,
        description_detail=body.description_detail,
        description_full=body.description_full,
        idempotency_key=body.idempotency_key,
    )
    resp = await client.commit(req)
    if resp.state == "committed":
        await session.commit()
    else:
        await session.rollback()
    return CommitOut(resource_id=resp.resource_id, state=resp.state, error_message=resp.error_message)


# ---------------------------------------------------------------------------
# List / Detail
# ---------------------------------------------------------------------------

def _ts(dt) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@router.get("", response_model=ResourceListOut)
async def list_resources(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    state: Optional[str] = Query(None, description="Filter by process_state"),
    resource_type: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_db),
):
    """List all resources with summary information."""
    q = select(ResourceTask)
    count_q = select(func.count()).select_from(ResourceTask)

    if state:
        q = q.where(ResourceTask.process_state == state)
        count_q = count_q.where(ResourceTask.process_state == state)
    if resource_type:
        q = q.where(ResourceTask.resource_type == resource_type)
        count_q = count_q.where(ResourceTask.resource_type == resource_type)

    total = (await session.execute(count_q)).scalar() or 0

    q = q.order_by(ResourceTask.updated_at.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await session.execute(q)).scalars().all()

    items = []
    for t in rows:
        items.append(ResourceSummaryOut(
            resource_id=t.resource_id,
            source_resource_id=t.source_resource_id,
            title=t.title,
            content_md5=t.content_md5,
            resource_type=t.resource_type,
            process_state=t.process_state,
            file_count=len(t.files),
            preview_count=len(t.previews),
            has_description=len(t.descriptions) > 0,
            has_embedding=len(t.embeddings) > 0,
            created_at=_ts(t.created_at),
            updated_at=_ts(t.updated_at),
        ))

    return ResourceListOut(total=total, page=page, page_size=page_size, resources=items)


@router.get("/{resource_id}", response_model=ResourceDetailOut)
async def get_resource_detail(resource_id: str, session: AsyncSession = Depends(get_db)):
    """Get detailed information about a specific resource."""
    task = (
        await session.execute(
            select(ResourceTask).where(ResourceTask.resource_id == resource_id)
        )
    ).scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail=f"Resource {resource_id} not found")

    files = [
        ResourceFileOut(
            file_name=f.file_name,
            file_format=f.file_format,
            file_size=f.file_size,
            content_md5=f.content_md5,
            file_role=f.file_role,
            ks3_key=f.ks3_key,
        )
        for f in task.files
    ]
    previews = [
        ResourcePreviewOut(
            role=p.role,
            strategy=p.strategy,
            format=p.format,
            width=p.width,
            height=p.height,
        )
        for p in task.previews
    ]
    desc = None
    if task.descriptions:
        d = task.descriptions[0]
        desc = ResourceDescriptionOut(
            main_content=d.main_content,
            detail_content=d.detail_content,
            full_description=d.full_description,
        )
    embed = None
    if task.embeddings:
        e = task.embeddings[0]
        embed = ResourceEmbeddingOut(
            dimension=e.dimension,
            checksum=e.checksum,
            model_version=e.model_version,
        )

    return ResourceDetailOut(
        resource_id=task.resource_id,
        source_resource_id=task.source_resource_id,
        content_md5=task.content_md5,
        resource_type=task.resource_type,
        process_state=task.process_state,
        source_directory=task.source_directory,
        source=task.source,
        pack_name=task.pack_name,
        title=task.title,
        resource_path=task.resource_path,
        source_url=task.source_url,
        original_download_url=task.original_download_url,
        category=task.category,
        license_name=task.license_name,
        source_description=task.source_description,
        tags=_loads_json_list(task.tags_json),
        parent_resource_id=task.parent_resource_id,
        child_resource_ids=_loads_json_list(task.child_resource_ids_json),
        child_resource_count=task.child_resource_count,
        contains_resource_types=_loads_json_list(task.contains_resource_types_json),
        download_object_key=task.download_object_key,
        download_file_name=task.download_file_name,
        download_content_type=task.download_content_type,
        download_file_size=task.download_file_size,
        created_at=_ts(task.created_at),
        updated_at=_ts(task.updated_at),
        files=files,
        previews=previews,
        description=desc,
        embedding=embed,
        last_error=task.last_error_message,
    )
