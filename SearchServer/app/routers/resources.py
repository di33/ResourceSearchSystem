"""Resource read endpoints.

Writes come from resource_processing_server through /resources/upsert.
SearchServer only returns metadata and URLs generated from storage profiles.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.middleware.auth import require_read_auth
from app.models.tables import (
    ResourceDescription,
    ResourceEmbedding,
    ResourceFile,
    ResourcePreview,
    ResourceTask,
)
from app.services.object_urls import ObjectUrlGenerator

router = APIRouter(prefix="/resources", tags=["resources"], dependencies=[Depends(require_read_auth)])


class ResourceFileOut(BaseModel):
    file_name: str
    file_format: str
    file_size: int
    content_md5: str
    file_role: str
    storage_profile_id: str = ""
    object_key: str = ""
    download_url: str = ""


class ResourcePreviewOut(BaseModel):
    role: str
    strategy: str
    format: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    storage_profile_id: str = ""
    object_key: str = ""
    preview_url: str = ""


class ResourceDescriptionOut(BaseModel):
    main_content: str
    detail_content: str
    full_description: str
    usage_space: str = ""
    usage_category: str = ""
    usage_subcategories: List[str] = []
    usage_classification_reason: str = ""
    usage_classification_suggestion: dict[str, Any] = {}
    usage_classification_version: str = ""


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
    client_metadata: Any | None = None
    tags: List[str] = []
    source_storage_profile_id: str = ""
    source_object_key: str = ""
    source_download_url: str = ""
    package_storage_profile_id: str = ""
    package_object_key: str = ""
    package_download_url: str = ""
    created_at: str = ""
    updated_at: str = ""
    files: List[ResourceFileOut] = []
    previews: List[ResourcePreviewOut] = []
    description: Optional[ResourceDescriptionOut] = None
    embedding: Optional[ResourceEmbeddingOut] = None
    last_error: str = ""


def _ts(dt) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _loads_json_list(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _loads_json_dict(raw: Optional[str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _loads_json_any(raw: Optional[str]) -> Any | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _object_url(urls: ObjectUrlGenerator, key: str, storage_profile_id: str = "") -> str:
    if not key:
        return ""
    try:
        return urls.generate_download_url(key, storage_profile_id=storage_profile_id)
    except Exception:
        return ""


def _preview_object_key(resource_id: str, preview: ResourcePreview) -> str:
    if preview.object_key:
        return preview.object_key
    name = str(preview.path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return f"previews/{resource_id}/{name}" if name else ""


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

    return ResourceListOut(
        total=total,
        page=page,
        page_size=page_size,
        resources=[
            ResourceSummaryOut(
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
            )
            for t in rows
        ],
    )


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

    urls = ObjectUrlGenerator()
    rid = task.resource_id or ""

    files = []
    for f in task.files:
        key = f.object_key or ""
        files.append(ResourceFileOut(
            file_name=f.file_name,
            file_format=f.file_format,
            file_size=f.file_size,
            content_md5=f.content_md5,
            file_role=f.file_role,
            storage_profile_id=f.storage_profile_id,
            object_key=key,
            download_url=_object_url(urls, key, f.storage_profile_id),
        ))

    previews = []
    for p in task.previews:
        key = _preview_object_key(rid, p)
        previews.append(ResourcePreviewOut(
            role=p.role,
            strategy=p.strategy,
            format=p.format,
            width=p.width,
            height=p.height,
            storage_profile_id=p.storage_profile_id,
            object_key=key,
            preview_url=_object_url(urls, key, p.storage_profile_id),
        ))

    desc = None
    if task.descriptions:
        d = task.descriptions[0]
        desc = ResourceDescriptionOut(
            main_content=d.main_content,
            detail_content=d.detail_content,
            full_description=d.full_description,
            usage_space=d.usage_space,
            usage_category=d.usage_category,
            usage_subcategories=_loads_json_list(d.usage_subcategories_json),
            usage_classification_reason=d.usage_classification_reason,
            usage_classification_suggestion=_loads_json_dict(d.usage_classification_suggestion_json),
            usage_classification_version=d.usage_classification_version,
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
        client_metadata=_loads_json_any(task.client_metadata_json),
        tags=_loads_json_list(task.tags_json),
        source_storage_profile_id=task.source_storage_profile_id,
        source_object_key=task.source_object_key,
        source_download_url=_object_url(urls, task.source_object_key, task.source_storage_profile_id),
        package_storage_profile_id=task.package_storage_profile_id,
        package_object_key=task.package_object_key,
        package_download_url=_object_url(urls, task.package_object_key, task.package_storage_profile_id),
        created_at=_ts(task.created_at),
        updated_at=_ts(task.updated_at),
        files=files,
        previews=previews,
        description=desc,
        embedding=embed,
        last_error=task.last_error_message,
    )
