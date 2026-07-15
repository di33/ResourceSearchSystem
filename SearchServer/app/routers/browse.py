"""Resource browser: serves the single-page HTML UI and browser list data."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.models.tables import ResourceDescription, ResourceEmbedding, ResourceFile, ResourcePreview, ResourceTask
from app.services.display_titles import display_title_for_task
from app.services.object_urls import ObjectUrlGenerator
from resource_contracts.resource_types import OTHER_RESOURCE_TYPE

router = APIRouter(tags=["browse"])

_HTML_PATH = Path(__file__).resolve().parent.parent / "static" / "browse.html"


@dataclass
class _BrowseTitleTask:
    resource_type: str
    client_metadata_json: str
    title: str
    source_resource_id: str
    resource_id: str
    content_md5: str
    source_directory: str = ""
    pack_name: str = ""
    source_description: str = ""
    source_object_file_name: str = ""


class BrowseResourceOut(BaseModel):
    resource_id: str
    source_resource_id: str = ""
    title: str = ""
    display_title: str = ""
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


def _preview_object_key(resource_id: str, object_key: str = "", path: str = "") -> str:
    if object_key:
        return object_key
    name = str(path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
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
):
    filters = []
    if resource_type:
        filters.append(ResourceTask.resource_type == resource_type)
    if state:
        filters.append(ResourceTask.process_state == state)
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
    include_counts: bool = Query(True),
    session: AsyncSession = Depends(get_db),
):
    """Paginated resource listing for the browser grid UI."""
    filters = _browse_filters(resource_type=resource_type, state=state)
    type_filters = _browse_filters(state=state)
    state_filters = _browse_filters(resource_type=resource_type)

    total = (
        await session.execute(
            select(func.count()).select_from(ResourceTask).where(*filters)
        )
    ).scalar() or 0

    rows = (
        await session.execute(
            select(
                ResourceTask.id,
                ResourceTask.resource_id,
                ResourceTask.source_resource_id,
                ResourceTask.title,
                ResourceTask.client_metadata_json,
                ResourceTask.content_md5,
                ResourceTask.resource_type,
                ResourceTask.process_state,
                ResourceTask.updated_at,
            )
            .where(*filters)
            .order_by(ResourceTask.updated_at.desc(), ResourceTask.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).mappings().all()
    task_ids = [int(row["id"]) for row in rows]

    file_counts = {}
    preview_counts = {}
    description_ids = set()
    embedding_ids = set()
    previews_by_task = {}
    if task_ids:
        file_counts = {
            int(task_id): int(count or 0)
            for task_id, count in (
                await session.execute(
                    select(ResourceFile.task_id, func.count(ResourceFile.id))
                    .where(ResourceFile.task_id.in_(task_ids))
                    .group_by(ResourceFile.task_id)
                )
            ).all()
        }
        preview_counts = {
            int(task_id): int(count or 0)
            for task_id, count in (
                await session.execute(
                    select(ResourcePreview.task_id, func.count(ResourcePreview.id))
                    .where(ResourcePreview.task_id.in_(task_ids))
                    .group_by(ResourcePreview.task_id)
                )
            ).all()
        }
        description_ids = {
            int(task_id)
            for (task_id,) in (
                await session.execute(
                    select(ResourceDescription.task_id)
                    .where(ResourceDescription.task_id.in_(task_ids))
                    .group_by(ResourceDescription.task_id)
                )
            ).all()
        }
        embedding_ids = {
            int(task_id)
            for (task_id,) in (
                await session.execute(
                    select(ResourceEmbedding.task_id)
                    .where(ResourceEmbedding.task_id.in_(task_ids))
                    .group_by(ResourceEmbedding.task_id)
                )
            ).all()
        }
        preview_rows = (
            await session.execute(
                select(
                    ResourcePreview.task_id,
                    ResourcePreview.role,
                    ResourcePreview.path,
                    ResourcePreview.storage_profile_id,
                    ResourcePreview.object_key,
                    ResourcePreview.id,
                )
                .where(ResourcePreview.task_id.in_(task_ids))
                .order_by(ResourcePreview.task_id, ResourcePreview.role.desc(), ResourcePreview.id)
            )
        ).mappings().all()
        for preview in preview_rows:
            task_id = int(preview["task_id"])
            if task_id not in previews_by_task or preview["role"] == "primary":
                previews_by_task[task_id] = preview

    urls = ObjectUrlGenerator()
    resources = []
    for row in rows:
        task_id = int(row["id"])
        rid = row["resource_id"] or row["content_md5"]
        preview = previews_by_task.get(task_id) or {}
        preview_key = _preview_object_key(
            rid,
            str(preview.get("object_key") or ""),
            str(preview.get("path") or ""),
        )
        preview_url = ""
        if preview_key:
            preview_url = _object_url(
                urls,
                preview_key,
                str(preview.get("storage_profile_id") or ""),
            )
        resources.append(BrowseResourceOut(
            resource_id=rid,
            source_resource_id=row["source_resource_id"],
            title=row["title"],
            display_title=display_title_for_task(_BrowseTitleTask(
                resource_type=row["resource_type"],
                client_metadata_json=row["client_metadata_json"],
                title=row["title"],
                source_resource_id=row["source_resource_id"],
                resource_id=rid,
                content_md5=row["content_md5"],
            )),
            content_md5=row["content_md5"],
            resource_type=row["resource_type"],
            process_state=row["process_state"],
            file_count=file_counts.get(task_id, 0),
            preview_count=preview_counts.get(task_id, 0),
            has_description=task_id in description_ids,
            has_embedding=task_id in embedding_ids,
            preview_url=preview_url,
            updated_at=_ts(row["updated_at"]),
        ))

    return BrowseResourceListOut(
        total=total,
        page=page,
        page_size=page_size,
        type_counts=await _count_by(session, ResourceTask.resource_type, type_filters) if include_counts else {},
        state_counts=await _count_by(session, ResourceTask.process_state, state_filters) if include_counts else {},
        resources=resources,
    )
