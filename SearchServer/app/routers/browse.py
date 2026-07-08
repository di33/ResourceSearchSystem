"""Resource browser: serves the single-page HTML UI and browser list data."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.middleware.auth import require_read_auth
from app.models.tables import ResourcePreview, ResourceTask
from app.services.object_urls import ObjectUrlGenerator
from resource_contracts.resource_types import OTHER_RESOURCE_TYPE

router = APIRouter(tags=["browse"], dependencies=[Depends(require_read_auth)])

_HTML_PATH = Path(__file__).resolve().parent.parent / "static" / "browse.html"


class BrowseResourceOut(BaseModel):
    resource_id: str
    source_resource_id: str = ""
    title: str = ""
    content_md5: str
    resource_type: str
    process_state: str
    file_count: int = 0
    preview_count: int = 0
    has_description: bool = False
    has_embedding: bool = False
    preview_url: str = ""
    updated_at: str = ""


class BrowseResourceListOut(BaseModel):
    total: int
    page: int
    page_size: int
    type_counts: dict[str, int] = Field(default_factory=dict)
    state_counts: dict[str, int] = Field(default_factory=dict)
    resources: list[BrowseResourceOut]


@router.get("/browse", response_class=HTMLResponse, include_in_schema=False)
async def browse_page():
    return HTMLResponse(_HTML_PATH.read_text(encoding="utf-8"))


def _ts(dt) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _preview_object_key(resource_id: str, preview: ResourcePreview) -> str:
    if preview.object_key:
        return preview.object_key
    name = str(preview.path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return f"previews/{resource_id}/{name}" if name else ""


def _object_url(urls: ObjectUrlGenerator, key: str, storage_profile_id: str = "") -> str:
    if not key:
        return ""
    try:
        return urls.generate_download_url(key, storage_profile_id=storage_profile_id)
    except Exception:
        return ""


def _browse_filters(
    *,
    resource_type: str | None = None,
    state: str | None = None,
    q: str | None = None,
):
    filters = []
    if resource_type:
        filters.append(ResourceTask.resource_type == resource_type)
    if state:
        filters.append(ResourceTask.process_state == state)
    if q:
        pattern = f"%{q.strip()}%"
        filters.append(or_(
            ResourceTask.resource_id.ilike(pattern),
            ResourceTask.source_resource_id.ilike(pattern),
            ResourceTask.title.ilike(pattern),
            ResourceTask.content_md5.ilike(pattern),
            ResourceTask.resource_type.ilike(pattern),
            ResourceTask.source_directory.ilike(pattern),
            ResourceTask.pack_name.ilike(pattern),
            ResourceTask.resource_path.ilike(pattern),
            ResourceTask.category.ilike(pattern),
            ResourceTask.tags_json.ilike(pattern),
        ))
    return filters


async def _count_by(session: AsyncSession, column, filters) -> dict[str, int]:
    rows = (
        await session.execute(
            select(column, func.count()).where(*filters).group_by(column)
        )
    ).all()
    return {str(key or OTHER_RESOURCE_TYPE): int(count or 0) for key, count in rows}


@router.get("/browse/resources", response_model=BrowseResourceListOut)
async def browse_resources(
    page: int = Query(1, ge=1),
    page_size: int = Query(72, ge=1, le=120),
    resource_type: str | None = Query(None),
    state: str | None = Query(None),
    q: str | None = Query(None),
    session: AsyncSession = Depends(get_db),
):
    """Paginated resource listing for the browser grid UI."""
    query_text = q.strip() if q else None
    filters = _browse_filters(resource_type=resource_type, state=state, q=query_text)
    type_filters = _browse_filters(state=state, q=query_text)
    state_filters = _browse_filters(resource_type=resource_type, q=query_text)

    total = (
        await session.execute(
            select(func.count()).select_from(ResourceTask).where(*filters)
        )
    ).scalar() or 0

    rows = (
        await session.execute(
            select(ResourceTask)
            .where(*filters)
            .order_by(ResourceTask.updated_at.desc(), ResourceTask.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    urls = ObjectUrlGenerator()
    resources = []
    for task in rows:
        rid = task.resource_id or task.content_md5
        previews = list(task.previews)
        preview_url = ""
        if previews:
            preview = previews[0]
            preview_url = _object_url(
                urls,
                _preview_object_key(rid, preview),
                preview.storage_profile_id,
            )
        resources.append(BrowseResourceOut(
            resource_id=rid,
            source_resource_id=task.source_resource_id,
            title=task.title,
            content_md5=task.content_md5,
            resource_type=task.resource_type,
            process_state=task.process_state,
            file_count=len(task.files),
            preview_count=len(previews),
            has_description=len(task.descriptions) > 0,
            has_embedding=len(task.embeddings) > 0,
            preview_url=preview_url,
            updated_at=_ts(task.updated_at),
        ))

    return BrowseResourceListOut(
        total=total,
        page=page,
        page_size=page_size,
        type_counts=await _count_by(session, ResourceTask.resource_type, type_filters),
        state_counts=await _count_by(session, ResourceTask.process_state, state_filters),
        resources=resources,
    )
