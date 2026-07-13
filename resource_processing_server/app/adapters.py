from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from resource_processing_server.app.config import settings
from resource_processing_server.app.legacy import ensure_resource_processor_imports
from resource_processing_server.app.models import Description, PreviewRef, SourceFileRef
from resource_processing_server.app.storage import local_file_md5
from resource_contracts.resource_types import SPINE_SKELETON_RESOURCE_TYPE


def _file_name(ref: SourceFileRef) -> str:
    return ref.file_name or Path(ref.path_in_package).name or "resource"


def _file_format(ref: SourceFileRef, local_path: str = "") -> str:
    if ref.file_format:
        return ref.file_format.lower().lstrip(".")
    name = _file_name(ref)
    if "." in name:
        return name.rsplit(".", 1)[-1].lower()
    suffix = Path(local_path).suffix.lower().lstrip(".")
    return suffix


def _description_context_json(description_context: Any | None) -> str:
    if description_context is None:
        return "null"
    return json.dumps(description_context, ensure_ascii=False, indent=2, sort_keys=True)


def build_processing_entity(
    *,
    client_id: str,
    client_resource_id: str,
    resource_type: str,
    source_files: list[SourceFileRef],
    local_source_paths: list[Path],
    description_context: Any | None,
):
    ensure_resource_processor_imports()
    from ResourceProcessor.preview_metadata import FileInfo, ResourceProcessingEntity

    files = []
    for index, ref in enumerate(source_files):
        path = local_source_paths[index] if index < len(local_source_paths) else None
        checksum = ref.checksum or (local_file_md5(str(path)) if path is not None else "")
        files.append(FileInfo(
            file_path=str(path) if path is not None else "",
            file_name=_file_name(ref),
            file_size=ref.file_size or (path.stat().st_size if path is not None else 0),
            file_format=_file_format(ref, str(path) if path is not None else ""),
            content_md5=checksum,
            file_role="main",
            is_primary=ref.is_primary or index == 0,
        ))

    composite = "|".join(sorted(file.content_md5 for file in files if file.content_md5))
    import hashlib

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
            "description_context_json": _description_context_json(description_context),
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


def preview_ref_from_info(info, uploaded: PreviewRef) -> PreviewRef:
    return PreviewRef(
        role=uploaded.role or getattr(info, "role", None) or "primary",
        storage_profile_id=uploaded.storage_profile_id,
        object_key=uploaded.object_key,
        width=getattr(info, "width", None),
        height=getattr(info, "height", None),
        size=getattr(info, "size", None) or uploaded.size,
        strategy=getattr(getattr(info, "strategy", ""), "value", None) or str(getattr(info, "strategy", "static")),
        origin="generated",
        renderer=getattr(info, "renderer", None) or uploaded.renderer,
    )


async def generate_description(
    *,
    entity,
    preview_paths: list[str],
    description_context: Any | None,
):
    ensure_resource_processor_imports()
    from ResourceProcessor.description.description_generator import (
        DescriptionInput,
        generate_resource_description_text,
    )

    formats = sorted({file.file_format for file in entity.files if file.file_format})
    context = {
        "resource_type": entity.resource_type,
        "client_id": entity.source,
        "client_resource_id": entity.source_resource_id,
        "source_files": [
            {
                "file_name": file.file_name,
                "file_format": file.file_format,
                "file_role": file.file_role,
                "file_size": file.file_size,
            }
            for file in entity.files
        ],
        "description_context": description_context,
    }
    prompt_context = (
        "资源加工服务器收到的客户端资源清单上下文如下。"
        "description_context 仅用于辅助生成资源描述，不会作为资源字段入库。\n"
        + json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)
    )
    input_data = DescriptionInput(
        preview_path=preview_paths[0] if preview_paths else "",
        preview_paths=preview_paths,
        llm_input_path=preview_paths[0] if preview_paths else "",
        llm_input_paths=preview_paths,
        resource_type=entity.resource_type,
        preview_strategy="provided" if preview_paths else "metadata",
        auxiliary_metadata={
            "description_context_json": _description_context_json(description_context),
            "source_file_count": len(entity.files),
        },
        asset_formats=formats,
        source_file_path=entity.primary_file.file_path if entity.primary_file else "",
        prompt_context_override=prompt_context,
        prompt_version_tag="resource_processing_server",
    )
    return await generate_resource_description_text(
        input_data,
        provider_name=settings.llm_provider,
        model=settings.llm_model,
    )


async def generate_descriptions_batch(requests: list[dict[str, Any]]) -> list[Any]:
    """Generate descriptions for a coalesced server-side batch.

    The scheduling boundary is implemented here so a provider-native batch
    implementation can replace this fallback without changing processor code.
    """
    import asyncio

    return await asyncio.gather(*[
        generate_description(
            entity=request["entity"],
            preview_paths=request["preview_paths"],
            description_context=request["description_context"],
        )
        for request in requests
    ])


def description_result_from_provided(provided: Description):
    ensure_resource_processor_imports()
    from ResourceProcessor.description.description_generator import DescriptionResult

    main = provided.summary.strip()
    detail = provided.detail.strip()
    full = "\n".join(part for part in (main, detail) if part)
    if not main:
        main = full[:120]
    if not detail:
        detail = full
    return DescriptionResult(
        main_content=main,
        detail_content=detail,
        full_description=full,
        prompt_version=provided.prompt_version or "client-provided",
        description_quality_score=provided.description_quality_score,
        usage_space="",
        usage_category="",
        usage_subcategories=[],
        usage_classification_reason="",
        usage_classification_suggestion=None,
        usage_classification_version="",
    )
