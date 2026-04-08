"""Resource management endpoints: register / upload-batch / previews / commit / list."""

from __future__ import annotations

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
    content_md5: str
    resource_type: str
    process_state: str
    source_directory: str = ""
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

def _build_client(session: AsyncSession) -> PgCloudClient:
    return PgCloudClient(session, KS3Storage(get_s3()), milvus_client=get_milvus())


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
    session: AsyncSession = Depends(get_db),
):
    """Upload multiple resource files at once."""
    client = _build_client(session)
    total_uploaded = 0
    for file in files:
        result = await client.upload_file_obj(
            resource_id,
            file.filename or "upload",
            file.file,
            file.content_type or "application/octet-stream",
        )
        if not result.success:
            await session.commit()
            return UploadBatchOut(
                success=False,
                uploaded_bytes=total_uploaded,
                file_count=len(files),
                error_message=f"Failed to upload {file.filename}: {result.error_message}",
            )
        total_uploaded += result.uploaded_bytes

    await session.commit()
    return UploadBatchOut(
        success=True,
        uploaded_bytes=total_uploaded,
        file_count=len(files),
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
    await session.commit()
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
        content_md5=task.content_md5,
        resource_type=task.resource_type,
        process_state=task.process_state,
        source_directory=task.source_directory,
        created_at=_ts(task.created_at),
        updated_at=_ts(task.updated_at),
        files=files,
        previews=previews,
        description=desc,
        embedding=embed,
        last_error=task.last_error_message,
    )
