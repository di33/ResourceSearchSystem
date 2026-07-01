"""Resource browser: serves the single-page HTML UI and proxies S3 objects."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_s3
from app.config import settings
from app.models.tables import ResourceTask

router = APIRouter(tags=["browse"])

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


async def _get_task(resource_id: str, session: AsyncSession) -> ResourceTask:
    task = (
        await session.execute(
            select(ResourceTask).where(ResourceTask.resource_id == resource_id)
        )
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(404, f"Resource {resource_id} not found")
    return task


def _attachment_disposition(filename: str) -> str:
    fallback = filename.encode("ascii", "ignore").decode("ascii")
    fallback = fallback.replace('"', "").replace("/", "_").replace("\\", "_")
    if not fallback:
        fallback = "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _ts(dt) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


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
    return {str(key or "other"): int(count or 0) for key, count in rows}


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

    resources = []
    for task in rows:
        rid = task.resource_id or task.content_md5
        preview_count = len(task.previews)
        resources.append(BrowseResourceOut(
            resource_id=rid,
            source_resource_id=task.source_resource_id,
            title=task.title,
            content_md5=task.content_md5,
            resource_type=task.resource_type,
            process_state=task.process_state,
            file_count=len(task.files),
            preview_count=preview_count,
            has_description=len(task.descriptions) > 0,
            has_embedding=len(task.embeddings) > 0,
            preview_url=f"/browse/preview/{quote(rid, safe='')}/0" if preview_count else "",
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


@router.get("/browse/preview/{resource_id}/{index}")
async def proxy_preview(resource_id: str, index: int, session: AsyncSession = Depends(get_db)):
    """Stream a preview image from S3 back to the browser."""
    task = await _get_task(resource_id, session)
    if index < 0 or index >= len(task.previews):
        raise HTTPException(404, "Preview index out of range")

    s3 = get_s3()
    prefix = f"previews/{resource_id}/"
    resp = s3.list_objects_v2(Bucket=settings.ks3_bucket, Prefix=prefix, MaxKeys=100)
    keys = sorted(o["Key"] for o in resp.get("Contents", []))
    if index >= len(keys):
        raise HTTPException(404, "Preview file not found in S3")

    key = keys[index]
    obj = s3.get_object(Bucket=settings.ks3_bucket, Key=key)
    ct = obj.get("ContentType", "image/png")
    return StreamingResponse(obj["Body"], media_type=ct, headers={
        "Cache-Control": "public, max-age=3600",
    })


@router.get("/browse/file/{resource_id}/{filename}")
async def proxy_file(resource_id: str, filename: str, session: AsyncSession = Depends(get_db)):
    """Stream a resource file from S3 for download."""
    task = await _get_task(resource_id, session)
    if task.resource_type == "pack":
        raise HTTPException(404, "Pack members are metadata only; download the package zip")

    file_rec = None
    for f in task.files:
        if f.file_name == filename:
            file_rec = f
            break
    if not file_rec or not file_rec.ks3_key:
        raise HTTPException(404, f"File {filename} not found")

    s3 = get_s3()
    try:
        obj = s3.get_object(Bucket=settings.ks3_bucket, Key=file_rec.ks3_key)
    except Exception:
        raise HTTPException(404, "File not found in S3")

    ct = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return StreamingResponse(obj["Body"], media_type=ct, headers={
        "Content-Disposition": _attachment_disposition(filename),
        "Cache-Control": "public, max-age=3600",
    })


@router.get("/browse/download/{resource_id}")
async def proxy_download(resource_id: str, session: AsyncSession = Depends(get_db)):
    """Stream a resource package download from S3."""
    task = await _get_task(resource_id, session)
    if not task.download_object_key:
        raise HTTPException(404, "Package download not found")

    s3 = get_s3()
    try:
        obj = s3.get_object(Bucket=settings.ks3_bucket, Key=task.download_object_key)
    except Exception:
        raise HTTPException(404, "Package download not found in S3")

    filename = task.download_file_name or Path(task.download_object_key).name or f"{resource_id}.zip"
    ct = task.download_content_type or obj.get("ContentType") or "application/octet-stream"
    return StreamingResponse(obj["Body"], media_type=ct, headers={
        "Content-Disposition": _attachment_disposition(filename),
        "Cache-Control": "public, max-age=3600",
    })
