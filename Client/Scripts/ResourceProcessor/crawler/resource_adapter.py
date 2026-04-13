from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from ResourceProcessor.crawler.catalog_loader import CrawlerAssetRecord, CrawlerResourceRecord
from ResourceProcessor.description.description_generator import DescriptionInput
from ResourceProcessor.preview_metadata import FileInfo, ResourceProcessingEntity


def _md5_file(path: str) -> str:
    hasher = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_resource_fingerprint(record: CrawlerResourceRecord) -> str:
    payload = {
        "id": record.id,
        "source": record.source,
        "pack_name": record.pack_name,
        "resource_type": record.resource_type,
        "resource_path": record.resource_path,
        "member_count": record.member_count,
        "file_paths": record.file_paths,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.md5(blob).hexdigest()


def _merge_tags(record: CrawlerResourceRecord) -> list[str]:
    tags: list[str] = []
    for value in record.tags + record.pack_tags:
        value = str(value).strip()
        if value and value not in tags:
            tags.append(value)
    return tags


def _asset_counter(assets: list[CrawlerAssetRecord], key: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for asset in assets:
        value = asset.metadata.get(key)
        if isinstance(value, str) and value.strip():
            counter[value.strip()] += 1
    return dict(counter)


def _asset_formats(assets: list[CrawlerAssetRecord], resolved_files: list[str]) -> list[str]:
    values: list[str] = []
    for asset in assets:
        fmt = asset.format
        if fmt and fmt not in values:
            values.append(fmt)
    for path in resolved_files:
        fmt = Path(path).suffix.lstrip(".").lower()
        if fmt and fmt not in values:
            values.append(fmt)
    return values


def _prefer_primary_relative_path(record: CrawlerResourceRecord) -> str:
    resource_path = record.resource_path
    if resource_path and Path(resource_path).suffix:
        return resource_path
    if record.file_paths:
        return record.file_paths[0]
    return ""


def _build_file_role(record: CrawlerResourceRecord, abs_path: str) -> tuple[str, bool]:
    ext = Path(abs_path).suffix.lower()
    is_first = bool(record.resolved_files) and abs_path == record.resolved_files[0]
    if record.resource_type == "audio_file":
        return "audio", is_first
    if record.resource_type == "font_file":
        return "font", is_first
    if record.resource_type in {"tileset", "animation_sequence"}:
        return ("tile" if record.resource_type == "tileset" else "frame"), is_first
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}:
        preferred_name = Path(_prefer_primary_relative_path(record)).name.lower()
        is_primary = Path(abs_path).name.lower() == preferred_name if preferred_name else is_first
        if preferred_name and not any(Path(p).name.lower() == preferred_name for p in record.resolved_files):
            is_primary = is_first
        return "main", is_primary
    return "attachment", is_first


def build_processing_entity(record: CrawlerResourceRecord) -> ResourceProcessingEntity:
    files: list[FileInfo] = []
    preferred_rel = _prefer_primary_relative_path(record)
    preferred_name = Path(preferred_rel).name.lower()

    for abs_path in record.resolved_files:
        file_name = Path(abs_path).name
        ext = Path(abs_path).suffix.lstrip(".").lower()
        file_role, is_primary = _build_file_role(record, abs_path)
        if preferred_name and file_name.lower() == preferred_name:
            is_primary = True
        files.append(
            FileInfo(
                file_path=abs_path,
                file_name=file_name,
                file_size=os.path.getsize(abs_path),
                file_format=ext,
                content_md5=_md5_file(abs_path),
                file_role=file_role,
                is_primary=is_primary,
            )
        )

    if files and not any(file.is_primary for file in files):
        files[0].is_primary = True

    source_directory = record.resource_path or record.pack_name
    if record.resolved_files:
        root_dir = os.path.commonpath(record.resolved_files)
        source_directory = root_dir if os.path.isdir(root_dir) else os.path.dirname(root_dir)

    source_description = record.description or record.pack_description
    tags = _merge_tags(record)
    asset_formats = _asset_formats(record.assets, record.resolved_files)
    missing_file_ratio = 0.0
    if record.member_count:
        missing_file_ratio = len(record.missing_files) / float(record.member_count)

    auxiliary_metadata: dict[str, Any] = {
        "source_resource_id": record.id,
        "resolved_file_count": len(record.resolved_files),
        "missing_file_count": len(record.missing_files),
        "asset_formats": asset_formats,
        "styles": _asset_counter(record.assets, "style"),
        "themes": _asset_counter(record.assets, "theme"),
    }

    return ResourceProcessingEntity(
        resource_type=record.resource_type,
        source_directory=source_directory,
        files=files,
        content_md5=compute_resource_fingerprint(record),
        source=record.source,
        pack_id=record.pack_id,
        pack_name=record.pack_name,
        title=record.title,
        resource_path=record.resource_path,
        source_resource_id=record.id,
        parent_resource_id=record.parent_resource_id or None,
        child_resource_ids=record.child_resource_ids,
        child_resource_count=record.child_resource_count,
        contains_resource_types=record.contains_resource_types,
        source_url=record.source_url,
        download_url=record.download_url,
        category=record.category,
        tags=tags,
        license_name=record.license_name,
        source_description=source_description,
        member_count=record.member_count,
        missing_files=record.missing_files,
        auxiliary_metadata={
            **auxiliary_metadata,
            "missing_file_ratio": round(missing_file_ratio, 4),
        },
    )


def build_description_input(entity: ResourceProcessingEntity) -> DescriptionInput:
    preview = entity.previews[0] if entity.previews else None
    asset_formats = entity.auxiliary_metadata.get("asset_formats", [])
    missing_count = len(entity.missing_files)
    denominator = entity.member_count or max(len(entity.files) + missing_count, 1)
    return DescriptionInput(
        preview_path=preview.path if preview and preview.path else "",
        resource_type=entity.resource_type,
        preview_strategy=preview.strategy.value if preview else "none",
        auxiliary_metadata={
            "format": ", ".join(asset_formats) if asset_formats else (entity.files[0].file_format if entity.files else "unknown"),
            "file_count": len(entity.files),
            "member_count": entity.member_count,
            "missing_file_count": missing_count,
            "styles": entity.auxiliary_metadata.get("styles", {}),
            "themes": entity.auxiliary_metadata.get("themes", {}),
        },
        title=entity.title,
        pack_name=entity.pack_name,
        resource_path=entity.resource_path,
        source=entity.source,
        source_tags=entity.tags,
        source_description=entity.source_description,
        category=entity.category,
        member_count=entity.member_count,
        asset_formats=asset_formats,
        preview_mode=preview.mode if preview else "none",
        preview_confidence=preview.confidence if preview else "low",
        missing_file_ratio=missing_count / float(denominator),
    )
