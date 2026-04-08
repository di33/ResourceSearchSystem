"""Resource browser: serves the single-page HTML UI and proxies S3 objects."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_s3
from app.config import settings
from app.models.tables import ResourceFile, ResourceTask

router = APIRouter(tags=["browse"])

_HTML_PATH = Path(__file__).resolve().parent.parent / "static" / "browse.html"


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
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "public, max-age=3600",
    })
