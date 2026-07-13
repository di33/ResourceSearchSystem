from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ResourceProcessor.core.object_storage_upload import safe_object_part
from ResourceProcessor.preview_metadata import ResourceProcessingEntity


def resource_identity(entity: ResourceProcessingEntity) -> str:
    return entity.source_resource_id or entity.content_md5 or safe_object_part(entity.title or entity.resource_path)


_GENERIC_DISPLAY_TITLES = {
    "image",
    "images",
    "png",
    "resource",
    "source",
    "transparent png",
}
_FRAME_SUFFIX_RE = re.compile(r"(?:[_\-\s]*\d{1,5})+$")
_SEPARATOR_RE = re.compile(r"[_\-]+")


def _path_stem(value: str) -> str:
    text = str(value or "").replace("\\", "/").strip().strip("/")
    if not text:
        return ""
    name = text.rsplit("/", 1)[-1]
    if "." in name and not name.startswith("."):
        name = name.rsplit(".", 1)[0]
    return name


def _clean_display_title(value: str) -> str:
    text = _SEPARATOR_RE.sub(" ", str(value or "").strip())
    return " ".join(text.split())


def _is_generic_display_title(value: str) -> bool:
    text = _clean_display_title(value).lower()
    return not text or text in _GENERIC_DISPLAY_TITLES


def _frame_prefix_title(value: str) -> str:
    stem = _path_stem(value)
    if "__" in stem:
        stem = stem.split("__", 1)[0]
    stem = _FRAME_SUFFIX_RE.sub("", stem).strip("_- ")
    return _clean_display_title(stem)


def display_title_from_entity(entity: ResourceProcessingEntity) -> str:
    primary = entity.primary_file
    candidates = [
        entity.title,
        _path_stem(entity.resource_path),
        str(entity.auxiliary_metadata.get("group_name") or ""),
        _frame_prefix_title(primary.file_name if primary is not None else ""),
    ]
    for candidate in candidates:
        title = _clean_display_title(candidate)
        if not _is_generic_display_title(title):
            return title
    return _clean_display_title(entity.title or entity.resource_path or resource_identity(entity))


def client_metadata_from_entity(entity: ResourceProcessingEntity) -> dict[str, Any]:
    primary = entity.primary_file
    display_title = display_title_from_entity(entity)
    metadata = {
        "display_title": display_title,
        "action_name": display_title if entity.resource_type == "animation_sequence" else "",
        "group_name": entity.auxiliary_metadata.get("group_name") or "",
        "source_title": entity.title,
        "resource_path": entity.resource_path,
        "pack_name": entity.pack_name,
        "source": entity.source,
        "source_resource_id": entity.source_resource_id,
        "parent_resource_id": entity.parent_resource_id or "",
        "member_count": entity.member_count,
        "primary_file_name": primary.file_name if primary is not None else "",
        "auxiliary_metadata": entity.auxiliary_metadata,
    }
    return {key: value for key, value in metadata.items() if value not in ("", [], {}, None)}


def description_context(entity: ResourceProcessingEntity) -> dict[str, Any]:
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


def description_from_entity(entity: ResourceProcessingEntity) -> dict[str, Any] | None:
    if not (entity.description_main or entity.description_detail or entity.description_full):
        return None
    main, detail = _main_detail_from_entity(entity)
    return {
        "summary": main,
        "detail": detail,
        "prompt_version": entity.prompt_version or "resource-processor-local",
        "description_quality_score": entity.description_quality_score,
        "source": "resource_processor",
    }


def classification_from_entity(entity: ResourceProcessingEntity) -> dict[str, Any] | None:
    payload = {
        "usage_space": entity.usage_space,
        "usage_category": entity.usage_category,
        "usage_subcategories": entity.usage_subcategories,
        "usage_classification_reason": entity.usage_classification_reason,
        "usage_classification_suggestion": entity.usage_classification_suggestion or {},
        "usage_classification_version": entity.usage_classification_version,
    }
    return payload if any(value not in ("", [], {}, None) for value in payload.values()) else None


def object_key(prefix: str, client_id: str, client_resource_id: str, file_name: str) -> str:
    parts = [
        prefix.strip("/"),
        safe_object_part(client_id),
        safe_object_part(client_resource_id),
        safe_object_part(file_name),
    ]
    return "/".join(part for part in parts if part)


def _clean_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def _public_object_ref(ref: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(ref, dict):
        return {}
    keys = ("storage_profile_id", "object_key", "file_name", "file_format", "size", "checksum")
    return {key: ref[key] for key in keys if key in ref and _clean_value(ref[key])}


def _public_preview_ref(ref: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "storage_profile_id",
        "object_key",
        "role",
        "file_name",
        "file_format",
        "size",
        "checksum",
        "width",
        "height",
    )
    return {key: ref[key] for key in keys if key in ref and _clean_value(ref[key])}


def _public_source_file_ref(ref: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(ref, dict):
        return {}
    file_name = str(ref.get("file_name") or "").strip()
    if not file_name:
        return {}
    keys = (
        "storage_profile_id",
        "object_key",
        "file_name",
        "file_format",
        "size",
        "file_size",
        "checksum",
        "path_in_package",
        "is_primary",
    )
    public_ref: dict[str, Any] = {}
    for key in keys:
        if key not in ref:
            continue
        value = ref[key]
        if key == "path_in_package" or _clean_value(value):
            public_ref[key] = value
    return public_ref


def build_processing_manifest(
    entity: ResourceProcessingEntity,
    *,
    client_id: str,
    source_object: dict[str, Any],
    source_files: list[dict[str, Any]],
    previews: list[dict[str, Any]] | None = None,
    description: dict[str, Any] | None = None,
    classification: dict[str, Any] | None = None,
    package_object: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client_resource_id = resource_identity(entity)
    public_package_object = _public_object_ref(package_object)
    public_source_files = [
        item
        for item in (_public_source_file_ref(item) for item in (source_files or []) if isinstance(item, dict))
        if item
    ]
    public_previews = [_public_preview_ref(item) for item in (previews or []) if isinstance(item, dict)]
    manifest = {
        "request_id": f"{client_id}:{client_resource_id}",
        "client_resource_id": client_resource_id,
        "resource_type": entity.resource_type,
        "source_object": _public_object_ref(source_object),
        "client_metadata": client_metadata_from_entity(entity),
    }
    if public_source_files:
        manifest["source_files"] = public_source_files
    if public_previews:
        manifest["previews"] = public_previews
    if description is not None:
        manifest["description"] = description
    if classification is not None:
        manifest["classification"] = classification
    if public_package_object:
        manifest["package_object"] = public_package_object
    if description is None:
        manifest["description_context"] = description_context(entity)
    return manifest
