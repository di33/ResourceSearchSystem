from __future__ import annotations

from typing import Any

from ResourceProcessor.core.object_storage_upload import safe_object_part
from ResourceProcessor.preview_metadata import ResourceProcessingEntity


def resource_identity(entity: ResourceProcessingEntity) -> str:
    return entity.source_resource_id or entity.content_md5 or safe_object_part(entity.title or entity.resource_path)


def client_metadata(entity: ResourceProcessingEntity) -> dict[str, Any]:
    return {
        "title": entity.title,
        "category": entity.category,
        "tags": entity.tags,
        "source_description": entity.source_description,
        "source": entity.source,
        "pack_name": entity.pack_name,
        "resource_path": entity.resource_path,
        "source_url": entity.source_url,
        "download_url": entity.download_url,
        "license_name": entity.license_name,
        "member_count": entity.member_count,
        "missing_files": entity.missing_files,
        "auxiliary_metadata": entity.auxiliary_metadata,
    }


def _strip_description_label(text: str, label: str) -> str:
    text = text.strip()
    return text[len(label):].strip() if text.startswith(label) else text


def _main_detail_from_entity(entity: ResourceProcessingEntity) -> tuple[str, str]:
    main = (entity.description_main or "").strip()
    detail = (entity.description_detail or "").strip()
    full = (entity.description_full or "").strip()
    if (main and detail) or not full:
        return main, detail

    lines = [line.strip() for line in full.splitlines() if line.strip()]
    if not main:
        for line in lines:
            if line.startswith("主体：") or line.startswith("Main:"):
                main = _strip_description_label(_strip_description_label(line, "主体："), "Main:")
                break
    if not detail:
        for line in lines:
            if line.startswith("细节：") or line.startswith("Detail:"):
                detail = _strip_description_label(_strip_description_label(line, "细节："), "Detail:")
                break
    if not main:
        main = full
    if not detail:
        detail = full
    return main, detail


def provided_description_from_entity(entity: ResourceProcessingEntity) -> dict[str, Any] | None:
    if not (entity.description_main or entity.description_detail or entity.description_full):
        return None
    main, detail = _main_detail_from_entity(entity)
    return {
        "main_content": main,
        "detail_content": detail,
        "prompt_version": entity.prompt_version or "resource-processor-local",
        "description_quality_score": entity.description_quality_score,
        "usage_space": entity.usage_space,
        "usage_category": entity.usage_category,
        "usage_subcategories": entity.usage_subcategories,
        "usage_classification_reason": entity.usage_classification_reason,
        "usage_classification_suggestion": entity.usage_classification_suggestion or {},
        "usage_classification_version": entity.usage_classification_version,
        "source": "resource_processor",
    }


def object_key(prefix: str, client_id: str, client_resource_id: str, file_name: str) -> str:
    parts = [
        prefix.strip("/"),
        safe_object_part(client_id),
        safe_object_part(client_resource_id),
        safe_object_part(file_name),
    ]
    return "/".join(part for part in parts if part)


def build_processing_manifest(
    entity: ResourceProcessingEntity,
    *,
    client_id: str,
    source_object: dict[str, Any],
    source_files: list[dict[str, Any]],
    provided_previews: list[dict[str, Any]] | None = None,
    provided_description: dict[str, Any] | None = None,
    package_object: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client_resource_id = resource_identity(entity)
    return {
        "request_id": f"{client_id}:{client_resource_id}",
        "client_resource_id": client_resource_id,
        "resource_type": entity.resource_type,
        "source_object": source_object,
        "source_files": source_files,
        "provided_previews": provided_previews or [],
        "provided_description": provided_description,
        "package_object": package_object,
        "client_metadata": client_metadata(entity),
    }
