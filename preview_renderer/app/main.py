from __future__ import annotations

import json
import logging

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from resource_contracts.auth import (
    ClientAuthConfig,
    extract_api_key,
    parse_client_api_keys,
    split_csv,
    validate_client_api_key,
)
from resource_contracts.path_safety import safe_file_name, safe_path_part
from resource_contracts.url_validation import UrlValidationError
from preview_renderer.app.config import settings

from preview_renderer.app.models import PreviewRenderRequest
from preview_renderer.app.service import PreviewRendererService

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Preview Renderer",
    description="Generates resource previews from object-storage manifests and returns rendered preview files.",
    version="0.1.0",
    debug=settings.debug,
)

service = PreviewRendererService()


def _client_auth_config() -> ClientAuthConfig:
    return ClientAuthConfig(
        client_keys=parse_client_api_keys(settings.client_api_keys),
        admin_keys=split_csv(settings.admin_api_keys),
        debug=settings.debug,
    )


async def require_client_id(
    x_client_id: str | None = Header(default=None, alias="X-Client-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str:
    client_id = (x_client_id or "").strip()
    if not client_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-Client-Id header is required")
    api_key = extract_api_key(authorization=authorization, x_api_key=x_api_key)
    if not validate_client_api_key(client_id, api_key, _client_auth_config()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key for X-Client-Id",
        )
    return client_id


@app.get("/health")
async def health():
    return {"status": "ok"}


def _cleanup_task(work_root):
    return BackgroundTask(service.cleanup_work_root, work_root)


def _error_detail(exc: Exception) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


@app.post(
    "/previews/render",
    responses={200: {"content": {"application/zip": {}}}},
)
async def render_previews(
    request: PreviewRenderRequest,
    client_id: str = Depends(require_client_id),
):
    try:
        rendered = await service.render(client_id=client_id, request=request)
        return FileResponse(
            rendered.zip_path,
            media_type="application/zip",
            filename=safe_file_name(f"{safe_path_part(request.client_resource_id)}_previews.zip", "previews.zip"),
            background=_cleanup_task(rendered.work_root),
        )
    except UrlValidationError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(exc)) from exc
    except Exception as exc:
        logger.exception("Preview render failed for %s", request.client_resource_id)
        raise HTTPException(status_code=500, detail=_error_detail(exc)) from exc


@app.post(
    "/previews/render/primary",
    responses={200: {"content": {"image/webp": {}, "image/png": {}, "image/gif": {}, "image/jpeg": {}}}},
)
async def render_primary_preview(
    request: PreviewRenderRequest,
    client_id: str = Depends(require_client_id),
):
    try:
        rendered = await service.render(client_id=client_id, request=request)
        metadata = rendered.primary_preview.model_dump()
        return FileResponse(
            rendered.primary_path,
            media_type=rendered.primary_preview.content_type,
            filename=rendered.primary_preview.file_name,
            headers={
                "X-Preview-Metadata": json.dumps(metadata, ensure_ascii=True, separators=(",", ":")),
            },
            background=_cleanup_task(rendered.work_root),
        )
    except UrlValidationError as exc:
        raise HTTPException(status_code=400, detail=_error_detail(exc)) from exc
    except Exception as exc:
        logger.exception("Primary preview render failed for %s", request.client_resource_id)
        raise HTTPException(status_code=500, detail=_error_detail(exc)) from exc
