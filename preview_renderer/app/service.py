from __future__ import annotations

import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from preview_renderer.app.config import settings
from preview_renderer.app.legacy import ensure_resource_processor_imports
from resource_contracts.path_safety import safe_file_name, safe_join_under
from resource_contracts.resource_types import SPINE_SKELETON_RESOURCE_TYPE
from resource_contracts.source_files import resolve_local_source_files
from resource_contracts.url_validation import UrlValidationError, validate_signed_source_url

from preview_renderer.app.models import PreviewFileOut, PreviewRenderManifest, PreviewRenderRequest


_CONTENT_TYPES = {
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}


@dataclass(frozen=True)
class RenderedPreviewResult:
    work_root: Path
    zip_path: Path
    manifest: PreviewRenderManifest
    primary_path: Path
    primary_preview: PreviewFileOut


def _content_type(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _strategy_value(info) -> str:
    value = getattr(info, "strategy", "static")
    return getattr(value, "value", None) or str(value or "static")


def _file_name(ref) -> str:
    return ref.file_name or Path(ref.path_in_package).name or "resource"


def _file_format(ref, local_path: str = "") -> str:
    if ref.file_format:
        return ref.file_format.lower().lstrip(".")
    name = _file_name(ref)
    if "." in name:
        return name.rsplit(".", 1)[-1].lower()
    return Path(local_path).suffix.lower().lstrip(".")


def _metadata_context(client_metadata: Any | None) -> str:
    import json

    if client_metadata is None:
        return "null"
    return json.dumps(client_metadata, ensure_ascii=False, indent=2, sort_keys=True)


def local_file_md5(path: str) -> str:
    import hashlib

    hasher = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def preview_object_name(local_path: str | Path, *, use_primary: bool, gallery_index: int) -> str:
    suffix = Path(local_path).suffix.lower() or ".webp"
    base = "primary" if use_primary else f"gallery-{gallery_index:03d}"
    return f"{base}{suffix}"


def build_processing_entity(
    *,
    client_id: str,
    client_resource_id: str,
    resource_type: str,
    source_files,
    local_source_paths: list[Path],
    client_metadata: Any | None,
):
    ensure_resource_processor_imports()
    from ResourceProcessor.preview_metadata import FileInfo, ResourceProcessingEntity

    files = []
    for index, (ref, path) in enumerate(zip(source_files, local_source_paths)):
        checksum = ref.checksum or local_file_md5(str(path))
        files.append(FileInfo(
            file_path=str(path),
            file_name=_file_name(ref),
            file_size=ref.file_size or path.stat().st_size,
            file_format=_file_format(ref, str(path)),
            content_md5=checksum,
            file_role="main",
            is_primary=ref.is_primary or index == 0,
        ))

    import hashlib

    composite = "|".join(sorted(file.content_md5 for file in files if file.content_md5))
    content_md5 = hashlib.md5(composite.encode("utf-8")).hexdigest() if composite else ""
    return ResourceProcessingEntity(
        resource_type=resource_type,
        source_directory=str(local_source_paths[0].parent) if local_source_paths else "",
        files=files,
        content_md5=content_md5,
        source=client_id,
        source_resource_id=client_resource_id,
        auxiliary_metadata={
            "client_id": client_id,
            "client_resource_id": client_resource_id,
            "client_metadata_json": _metadata_context(client_metadata),
        },
    )


async def generate_previews(entity, previews_dir: Path):
    ensure_resource_processor_imports()
    if entity.resource_type == SPINE_SKELETON_RESOURCE_TYPE:
        from spine_preview.runtime_preview import generate_spine_runtime_previews

        try:
            return await generate_spine_runtime_previews(
                entity,
                previews_dir,
                max_size=settings.preview_max_size,
            )
        except Exception as exc:
            from ResourceProcessor.preview.crawler_thumbnail_policy import CrawlerThumbnailPolicy

            policy = CrawlerThumbnailPolicy(str(previews_dir), max_size=settings.preview_max_size)
            previews = await policy.generate_previews(entity)
            reason = str(exc)[:300]
            for preview in previews:
                preview.renderer = "spine-runtime-fallback"
                preview.mode = f"runtime_fallback_{preview.mode}"
                preview.confidence = "low"
                preview.fail_reason = reason
            return previews

    from ResourceProcessor.preview.crawler_thumbnail_policy import CrawlerThumbnailPolicy

    policy = CrawlerThumbnailPolicy(str(previews_dir), max_size=settings.preview_max_size)
    return await policy.generate_previews(entity)


class PreviewRendererService:
    def __init__(self):
        ensure_resource_processor_imports()

    async def _download_source_object(self, url: str, target: Path) -> Path:
        url = validate_signed_source_url(
            url,
            allowed_hosts=settings.allowed_source_url_hosts,
            allow_private_hosts=settings.allow_private_source_url_hosts,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        downloaded = 0
        async with httpx.AsyncClient(follow_redirects=False, timeout=300.0) as client:
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    raise UrlValidationError("source_object_url redirects are not allowed")
                response.raise_for_status()
                with target.open("wb") as handle:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        downloaded += len(chunk)
                        if downloaded > settings.max_download_bytes:
                            raise RuntimeError("source object download exceeds limit")
                        handle.write(chunk)
        return target

    async def render(self, *, client_id: str, request: PreviewRenderRequest) -> RenderedPreviewResult:
        render_id = f"preview_{uuid.uuid4().hex[:16]}"
        render_root = safe_join_under(settings.work_dir, render_id, fallback="render")
        work_root = safe_join_under(render_root, request.client_resource_id, fallback="resource")
        source_dir = work_root / "source"
        preview_dir = work_root / "previews"
        bundle_dir = preview_dir / "bundle"
        try:
            source_object_name = safe_file_name(
                request.source_object.file_name or Path(request.source_object.object_key).name,
                "source",
            )
            local_source_object = await self._download_source_object(
                request.source_object_url,
                source_dir / source_object_name,
            )
            local_sources = resolve_local_source_files(
                local_source_object,
                request.source_files,
                source_dir,
                max_zip_members=settings.max_zip_members,
                max_zip_member_bytes=settings.max_zip_member_bytes,
                max_zip_extract_bytes=settings.max_zip_extract_bytes,
                max_zip_compression_ratio=settings.max_zip_compression_ratio,
            )
            entity = build_processing_entity(
                client_id=client_id,
                client_resource_id=request.client_resource_id,
                resource_type=request.resource_type,
                source_files=request.source_files,
                local_source_paths=local_sources,
                client_metadata=request.client_metadata,
            )
            generated_infos = await generate_previews(entity, preview_dir / "generated")

            bundle_dir.mkdir(parents=True, exist_ok=True)
            previews: list[PreviewFileOut] = []
            copied_paths: list[Path] = []
            primary_used = False
            gallery_index = 1
            for info in generated_infos:
                source_path_text = getattr(info, "path", "") or ""
                if not source_path_text:
                    continue
                source_path = Path(source_path_text)
                if not source_path.is_file():
                    continue

                role = getattr(info, "role", "") or "primary"
                use_primary = role == "primary" and not primary_used
                name = preview_object_name(source_path, use_primary=use_primary, gallery_index=gallery_index)
                if use_primary:
                    primary_used = True
                else:
                    gallery_index += 1

                target = safe_join_under(bundle_dir, name, fallback="preview")
                shutil.copy2(source_path, target)
                copied_paths.append(target)
                stat = target.stat()
                previews.append(
                    PreviewFileOut(
                        role="primary" if use_primary else "gallery",
                        file_name=name,
                        content_type=_content_type(target),
                        width=getattr(info, "width", None),
                        height=getattr(info, "height", None),
                        size=getattr(info, "size", None) or stat.st_size,
                        checksum=local_file_md5(str(target)),
                        strategy=_strategy_value(info),
                        mode=getattr(info, "mode", "") or "",
                        confidence=getattr(info, "confidence", "") or "",
                        origin="generated",
                        renderer=getattr(info, "renderer", None) or "preview-renderer",
                        used_placeholder=bool(getattr(info, "used_placeholder", False)),
                        fail_reason=getattr(info, "fail_reason", None) or "",
                    )
                )

            if not previews:
                raise RuntimeError("preview generation produced no valid previews")

            manifest = PreviewRenderManifest(
                client_resource_id=request.client_resource_id,
                previews=previews,
                preview_count=len(previews),
            )
            manifest_path = bundle_dir / "manifest.json"
            manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

            zip_path = preview_dir / "rendered_previews.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(manifest_path, "manifest.json")
                for preview, path in zip(previews, copied_paths):
                    archive.write(path, preview.file_name)

            primary_index = next((index for index, preview in enumerate(previews) if preview.role == "primary"), 0)
            return RenderedPreviewResult(
                work_root=render_root,
                zip_path=zip_path,
                manifest=manifest,
                primary_path=copied_paths[primary_index],
                primary_preview=previews[primary_index],
            )
        except Exception:
            self.cleanup_work_root(render_root)
            raise

    def cleanup_work_root(self, work_root: Path) -> None:
        if not settings.keep_work_dir:
            shutil.rmtree(work_root, ignore_errors=True)
