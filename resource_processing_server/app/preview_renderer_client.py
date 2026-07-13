from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

import httpx

from resource_processing_server.app.config import settings
from resource_processing_server.app.models import ChildResourceManifest


@dataclass(frozen=True)
class RenderedPreviewFile:
    path: Path
    role: str = "primary"
    file_name: str = ""
    content_type: str = "application/octet-stream"
    width: int | None = None
    height: int | None = None
    size: int | None = None
    checksum: str = ""
    strategy: str = "static"
    origin: str = "generated"
    renderer: str = "preview-renderer"


_FILENAME_RE = re.compile(r"filename\*?=(?P<value>[^;]+)", re.IGNORECASE)
_CONTENT_TYPE_EXT = {
    "image/webp": ".webp",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
}


def _payload(client_id: str, manifest: ChildResourceManifest, *, source_object_url: str) -> dict:
    return {
        "client_resource_id": manifest.client_resource_id,
        "resource_type": manifest.resource_type,
        "source_object": manifest.source_object.model_dump(),
        "source_object_url": source_object_url,
        "source_files": [item.model_dump() for item in manifest.source_files],
    }


def _safe_file_name(value: str, fallback: str = "preview.webp") -> str:
    name = Path(str(value or "")).name.strip().strip(" .")
    for char in '<>:"/\\|?*\x00':
        name = name.replace(char, "_")
    if not name or name in {".", ".."}:
        name = fallback
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"unsafe preview file name: {value}")
    return name


def _filename_from_content_disposition(value: str) -> str:
    match = _FILENAME_RE.search(value or "")
    if not match:
        return ""
    raw = match.group("value").strip().strip('"')
    if raw.lower().startswith("utf-8''"):
        raw = unquote(raw[7:])
    return raw


def _metadata_to_preview(path: Path, metadata: dict) -> RenderedPreviewFile:
    return RenderedPreviewFile(
        path=path,
        role=str(metadata.get("role") or "primary"),
        file_name=str(metadata.get("file_name") or path.name),
        content_type=str(metadata.get("content_type") or "application/octet-stream"),
        width=metadata.get("width"),
        height=metadata.get("height"),
        size=metadata.get("size") or path.stat().st_size,
        checksum=str(metadata.get("checksum") or ""),
        strategy=str(metadata.get("strategy") or "static"),
        origin=str(metadata.get("origin") or "generated"),
        renderer=str(metadata.get("renderer") or "preview-renderer"),
    )


class PreviewRendererClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        self.base_url = (base_url if base_url is not None else settings.preview_renderer_url).strip().rstrip("/")
        self.timeout = timeout if timeout is not None else settings.preview_renderer_timeout

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    async def render_previews(
        self,
        *,
        client_id: str,
        manifest: ChildResourceManifest,
        source_object_url: str,
        output_dir: Path,
    ) -> list[RenderedPreviewFile]:
        if not self.enabled:
            raise RuntimeError("preview renderer URL is not configured")
        headers = {"X-Client-Id": client_id, "Accept": "application/zip"}
        if settings.preview_renderer_api_key:
            headers["X-API-Key"] = settings.preview_renderer_api_key
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/previews/render",
                json=_payload(client_id, manifest, source_object_url=source_object_url),
                headers=headers,
            )
            response.raise_for_status()
        return self.extract_preview_zip(response.content, output_dir)

    async def render_primary_preview(
        self,
        *,
        client_id: str,
        manifest: ChildResourceManifest,
        source_object_url: str,
        output_dir: Path,
    ) -> RenderedPreviewFile:
        if not self.enabled:
            raise RuntimeError("preview renderer URL is not configured")
        headers = {"X-Client-Id": client_id}
        if settings.preview_renderer_api_key:
            headers["X-API-Key"] = settings.preview_renderer_api_key
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/previews/render/primary",
                json=_payload(client_id, manifest, source_object_url=source_object_url),
                headers=headers,
            )
            response.raise_for_status()
        return self.save_primary_response(
            response.content,
            output_dir,
            content_type=response.headers.get("content-type", ""),
            content_disposition=response.headers.get("content-disposition", ""),
            metadata_header=response.headers.get("x-preview-metadata", ""),
        )

    def extract_preview_zip(self, content: bytes, output_dir: Path) -> list[RenderedPreviewFile]:
        output_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            try:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            except KeyError as exc:
                raise ValueError("preview renderer zip did not include manifest.json") from exc
            previews = []
            for item in manifest.get("previews") or []:
                if not isinstance(item, dict):
                    continue
                file_name = _safe_file_name(str(item.get("file_name") or ""), "preview.webp")
                target = output_dir / file_name
                try:
                    data = archive.read(file_name)
                except KeyError as exc:
                    raise ValueError(f"preview file listed in manifest is missing: {file_name}") from exc
                target.write_bytes(data)
                item = dict(item)
                item["file_name"] = file_name
                previews.append(_metadata_to_preview(target, item))
        if not previews:
            raise ValueError("preview renderer zip contained no preview files")
        return previews

    def save_primary_response(
        self,
        content: bytes,
        output_dir: Path,
        *,
        content_type: str = "",
        content_disposition: str = "",
        metadata_header: str = "",
    ) -> RenderedPreviewFile:
        output_dir.mkdir(parents=True, exist_ok=True)
        metadata = {}
        if metadata_header:
            metadata = json.loads(metadata_header)
        content_type = (content_type or metadata.get("content_type") or "application/octet-stream").split(";")[0]
        fallback = f"primary{_CONTENT_TYPE_EXT.get(content_type, '.webp')}"
        file_name = (
            metadata.get("file_name")
            or _filename_from_content_disposition(content_disposition)
            or fallback
        )
        file_name = _safe_file_name(str(file_name), fallback)
        target = output_dir / file_name
        target.write_bytes(content)
        metadata = dict(metadata)
        metadata.setdefault("role", "primary")
        metadata.setdefault("file_name", file_name)
        metadata.setdefault("content_type", content_type)
        metadata.setdefault("origin", "generated")
        metadata.setdefault("renderer", "preview-renderer")
        return _metadata_to_preview(target, metadata)
