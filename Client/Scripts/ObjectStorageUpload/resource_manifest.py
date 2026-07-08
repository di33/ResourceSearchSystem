"""Upload local resource files and produce resource-processing manifests."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import zipfile
import datetime
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Iterable

from ObjectStorageUpload.uploader import (
    ObjectStorageUploader,
    file_md5,
    safe_object_part,
    safe_object_path_part,
)
from ObjectStorageUpload.storage_profiles import load_storage_profiles
from ResourceProcessor.core.processing_manifest import (
    build_processing_manifest,
    client_metadata,
    resource_identity,
    provided_description_from_entity,
)
from ResourceProcessor.pipeline_common import Report
from ResourceProcessor.preview_metadata import FileInfo, ResourceProcessingEntity
from resource_contracts.resource_types import PACK_RESOURCE_TYPE, is_search_indexable_resource_type


_THREAD_LOCAL = threading.local()
_WINDOWS_FILENAME_FORBIDDEN = set('<>:"/\\|?*')
OBJECT_FINGERPRINT_VERSION = "client-object-fingerprint-v1"
MANIFEST_FINGERPRINT_VERSION = "processing-manifest-fingerprint-v1"
UPLOAD_KEY_SCHEME_VERSION = "client-object-key-v1"
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(value: Any) -> str:
    import hashlib

    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _object_key_prefix_parts(prefix: str) -> list[str]:
    normalized = str(prefix or "").replace("\\", "/").strip("/")
    return [safe_object_path_part(part) for part in normalized.split("/") if part not in {"", ".", ".."}]


def _object_key(prefix: str, client_id: str, kind: str, client_resource_id: str, relative_path: str | Path) -> str:
    parts = [
        *_object_key_prefix_parts(prefix),
        safe_object_part(client_id),
        safe_object_part(kind),
        safe_object_part(client_resource_id),
    ]
    raw_parts = Path(relative_path).parts
    for part in raw_parts:
        if part in {"", os.sep, os.altsep, ".", ".."}:
            continue
        parts.append(safe_object_path_part(part))
    return "/".join(part for part in parts if part)


def _source_object_key(prefix: str, client_id: str, client_resource_id: str, relative_path: str | Path) -> str:
    return _object_key(prefix, client_id, "files", client_resource_id, relative_path)


def _preview_object_key(prefix: str, client_id: str, client_resource_id: str, preview_name: str) -> str:
    return _object_key(prefix, client_id, "previews", client_resource_id, preview_name)


def _relative_file_path(entity: ResourceProcessingEntity, file_info: FileInfo) -> Path:
    file_path = Path(file_info.file_path)
    if entity.source_directory:
        try:
            rel = file_path.resolve().relative_to(Path(entity.source_directory).resolve())
            if rel.parts and rel.parts[0] != "..":
                return rel
        except (OSError, ValueError):
            pass
    return Path(file_info.file_name or file_path.name)


def _package_file_name() -> str:
    return "source.zip"


def _local_package_file_name(package_name: str) -> str:
    text = "".join("_" if ch in _WINDOWS_FILENAME_FORBIDDEN or ord(ch) < 32 else ch for ch in package_name)
    text = text.strip(" .") or "package.zip"
    if not text.lower().endswith(".zip"):
        text = f"{text}.zip"
    if Path(text).stem.upper() in _WINDOWS_RESERVED_NAMES:
        text = f"_{text}"
    return text


def _preview_extension(preview, path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix:
        return suffix
    fmt = safe_object_path_part(getattr(preview, "format", "") or "webp").strip(". ")
    return f".{fmt or 'webp'}"


def _preview_name(preview, path: Path, *, use_primary: bool, gallery_index: int) -> str:
    suffix = _preview_extension(preview, path)
    if use_primary:
        return f"primary{suffix}"
    return f"gallery-{gallery_index:03d}{suffix}"


def _package_members(entity: ResourceProcessingEntity) -> list[tuple[FileInfo, Path, str]]:
    root = Path(entity.source_directory).resolve() if entity.source_directory else None
    members: list[tuple[FileInfo, Path, str]] = []
    seen: dict[str, int] = {}
    for file_info in entity.files:
        if not file_info.file_path or not Path(file_info.file_path).is_file():
            continue
        file_path = Path(file_info.file_path)
        arcname = Path(file_info.file_name or file_path.name)
        if root is not None:
            try:
                arcname = file_path.resolve().relative_to(root)
            except (OSError, ValueError):
                arcname = Path(file_info.file_name or file_path.name)
        arcname_text = str(arcname).replace("\\", "/").strip("/")
        if not arcname_text or arcname_text.startswith("../"):
            arcname_text = file_info.file_name or file_path.name
        if arcname_text in seen:
            seen[arcname_text] += 1
            base, ext = os.path.splitext(arcname_text)
            arcname_text = f"{base}_{seen[arcname_text]}{ext}"
        else:
            seen[arcname_text] = 0
        members.append((file_info, file_path, arcname_text))
    return members


def _build_source_zip(entity: ResourceProcessingEntity, output_path: Path) -> list[tuple[FileInfo, Path, str]]:
    members = _package_members(entity)
    if not members:
        return []
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for _file_info, file_path, arcname_text in members:
            zf.write(file_path, arcname=arcname_text)
    return members


def _planned_ref(
    file_path: str | Path,
    *,
    storage_profile_id: str,
    object_key_value: str,
    is_primary: bool,
) -> dict[str, Any]:
    path = Path(file_path)
    return {
        "storage_profile_id": storage_profile_id,
        "object_key": object_key_value,
        "file_name": path.name,
        "file_format": path.suffix.lower().lstrip("."),
        "size": path.stat().st_size,
        "checksum": file_md5(path),
        "etag": "",
        "is_primary": is_primary,
    }


def _logical_file_ref(file_info: FileInfo, path: Path, arcname: str, *, is_primary: bool) -> dict[str, Any]:
    return {
        "file_name": file_info.file_name or path.name,
        "file_format": (file_info.file_format or path.suffix.lower().lstrip(".")).lower().lstrip("."),
        "file_size": int(file_info.file_size or path.stat().st_size),
        "checksum": file_info.content_md5 or file_md5(path),
        "path_in_package": arcname,
        "is_primary": is_primary,
    }


def _source_files_from_members(members: list[tuple[FileInfo, Path, str]]) -> list[dict[str, Any]]:
    source_files: list[dict[str, Any]] = []
    primary_seen = False
    for index, (file_info, path, arcname) in enumerate(members):
        is_primary = bool(file_info.is_primary) or (not primary_seen and index == 0)
        if is_primary:
            primary_seen = True
        source_files.append(_logical_file_ref(file_info, path, arcname, is_primary=is_primary))
    return source_files


def _file_size_hint(file_info: FileInfo, path: Path) -> int:
    if file_info.file_size:
        return int(file_info.file_size)
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _logical_file_ref_without_reads(file_info: FileInfo, path: Path, arcname: str, *, is_primary: bool) -> dict[str, Any]:
    return {
        "file_name": file_info.file_name or path.name,
        "file_format": (file_info.file_format or path.suffix.lower().lstrip(".")).lower().lstrip("."),
        "file_size": _file_size_hint(file_info, path),
        "checksum": file_info.content_md5 or "",
        "path_in_package": arcname,
        "is_primary": is_primary,
    }


def _source_object_plan_parts(
    entity: ResourceProcessingEntity,
    *,
    client_id: str,
    storage_profile_id: str,
    key_prefix: str,
) -> dict[str, Any]:
    client_resource_id = resource_identity(entity)
    members = _package_members(entity)
    source_object: dict[str, Any] = {}
    source_files: list[dict[str, Any]] = []
    if len(members) == 1:
        file_info, path, arcname = members[0]
        key = _source_object_key(key_prefix, client_id, client_resource_id, _relative_file_path(entity, file_info))
        source_object = {
            "storage_profile_id": storage_profile_id,
            "object_key": key,
            "file_name": path.name,
            "file_format": path.suffix.lower().lstrip("."),
            "size": _file_size_hint(file_info, path),
            "checksum": file_info.content_md5 or "",
            "is_primary": True,
        }
        source_files = [_logical_file_ref_without_reads(file_info, path, "", is_primary=True)]
    elif len(members) > 1:
        package_name = _package_file_name()
        key = _source_object_key(key_prefix, client_id, client_resource_id, package_name)
        source_object = {
            "storage_profile_id": storage_profile_id,
            "object_key": key,
            "file_name": package_name,
            "file_format": "zip",
            "package_strategy": "zip",
        }
        primary_seen = False
        for index, (file_info, path, arcname) in enumerate(members):
            is_primary = bool(file_info.is_primary) or (not primary_seen and index == 0)
            if is_primary:
                primary_seen = True
            source_files.append(_logical_file_ref_without_reads(file_info, path, arcname, is_primary=is_primary))
    return {
        "source_object": source_object,
        "source_files": source_files,
    }


def _preview_plan_parts(
    entity: ResourceProcessingEntity,
    *,
    client_id: str,
    storage_profile_id: str,
    key_prefix: str,
    include_previews: bool,
) -> list[dict[str, Any]]:
    if not include_previews:
        return []
    client_resource_id = resource_identity(entity)
    previews: list[dict[str, Any]] = []
    primary_used = False
    gallery_index = 1
    for preview in entity.previews:
        if not preview.path or not Path(preview.path).is_file():
            continue
        path = Path(preview.path)
        input_role = preview.role or "primary"
        use_primary = input_role == "primary" and not primary_used
        preview_name = _preview_name(preview, path, use_primary=use_primary, gallery_index=gallery_index)
        if use_primary:
            primary_used = True
        else:
            gallery_index += 1
        role = "primary" if use_primary else "gallery"
        previews.append(
            {
                "storage_profile_id": storage_profile_id,
                "object_key": _preview_object_key(key_prefix, client_id, client_resource_id, preview_name),
                "role": role,
                "file_name": path.name,
                "file_format": path.suffix.lower().lstrip("."),
                "size": int(preview.size or 0),
                "content_hash": getattr(preview, "content_hash", "") or "",
                "width": preview.width,
                "height": preview.height,
                "strategy": preview.strategy.value if hasattr(preview.strategy, "value") else str(preview.strategy),
                "origin": "provided",
                "renderer": preview.renderer or "client",
                "is_primary": use_primary,
            }
        )
    return previews


def upload_options_payload(
    *,
    client_id: str,
    storage_profile_id: str,
    key_prefix: str,
    include_previews: bool,
    include_descriptions: bool,
) -> dict[str, Any]:
    return {
        "client_id": client_id,
        "storage_profile_id": storage_profile_id,
        "key_prefix": key_prefix or "",
        "include_previews": bool(include_previews),
        "include_descriptions": bool(include_descriptions),
        "key_scheme_version": UPLOAD_KEY_SCHEME_VERSION,
    }


def object_fingerprint_for_entity(
    entity: ResourceProcessingEntity,
    *,
    client_id: str,
    storage_profile_id: str,
    key_prefix: str,
    include_previews: bool,
) -> tuple[str, dict[str, Any]]:
    source_parts = _source_object_plan_parts(
        entity,
        client_id=client_id,
        storage_profile_id=storage_profile_id,
        key_prefix=key_prefix,
    )
    preview_parts = _preview_plan_parts(
        entity,
        client_id=client_id,
        storage_profile_id=storage_profile_id,
        key_prefix=key_prefix,
        include_previews=include_previews,
    )
    parts = {
        "version": OBJECT_FINGERPRINT_VERSION,
        "key_scheme_version": UPLOAD_KEY_SCHEME_VERSION,
        "client_resource_id": resource_identity(entity),
        "source_object": source_parts["source_object"],
        "source_files": source_parts["source_files"],
        "provided_previews": preview_parts,
    }
    return _stable_hash(parts), parts


def manifest_resource_fingerprint_for_entity(
    entity: ResourceProcessingEntity,
    *,
    client_id: str,
    storage_profile_id: str,
    key_prefix: str,
    include_previews: bool,
    include_descriptions: bool,
    object_fingerprint: str,
    package_object: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    source_parts = _source_object_plan_parts(
        entity,
        client_id=client_id,
        storage_profile_id=storage_profile_id,
        key_prefix=key_prefix,
    )
    payload = {
        "version": MANIFEST_FINGERPRINT_VERSION,
        "upload_options": upload_options_payload(
            client_id=client_id,
            storage_profile_id=storage_profile_id,
            key_prefix=key_prefix,
            include_previews=include_previews,
            include_descriptions=include_descriptions,
        ),
        "request_id": f"{client_id}:{resource_identity(entity)}",
        "client_resource_id": resource_identity(entity),
        "resource_type": entity.resource_type,
        "object_fingerprint": object_fingerprint,
        "source_files": source_parts["source_files"],
        "provided_description": provided_description_from_entity(entity) if include_descriptions else None,
        "package_object": package_object or None,
        "client_metadata": client_metadata(entity),
    }
    return _stable_hash(payload), payload


def _manifest_reusing_objects(
    entity: ResourceProcessingEntity,
    old_manifest: dict[str, Any],
    *,
    client_id: str,
    include_descriptions: bool,
    package_object: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_processing_manifest(
        entity,
        client_id=client_id,
        source_object=old_manifest.get("source_object") or {},
        source_files=old_manifest.get("source_files") or [],
        provided_previews=old_manifest.get("provided_previews") or [],
        provided_description=provided_description_from_entity(entity) if include_descriptions else None,
        package_object=package_object or None,
    )


def upload_entity_objects(
    entity: ResourceProcessingEntity,
    *,
    uploader: ObjectStorageUploader | None,
    client_id: str,
    include_previews: bool,
    key_prefix: str = "",
    include_descriptions: bool = False,
    storage_profile_id: str = "",
    dry_run: bool = False,
    package_object: dict[str, Any] | None = None,
    reuse_source_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    client_resource_id = resource_identity(entity)
    resolved_profile_id = storage_profile_id or (uploader.profile.profile_id if uploader else "")
    if not resolved_profile_id:
        resolved_profile_id = load_storage_profiles().default_profile_id

    source_object: dict[str, Any] = {}
    source_files: list[dict[str, Any]] = []
    if reuse_source_manifest:
        source_object = dict(reuse_source_manifest.get("source_object") or {})
        source_files = list(reuse_source_manifest.get("source_files") or [])
    else:
        members = _package_members(entity)
        if len(members) == 1:
            file_info, path, arcname = members[0]
            source_files = _source_files_from_members(members)
            key = _source_object_key(key_prefix, client_id, client_resource_id, _relative_file_path(entity, file_info))
            if dry_run:
                source_object = _planned_ref(
                    path,
                    storage_profile_id=resolved_profile_id,
                    object_key_value=key,
                    is_primary=True,
                )
            else:
                if uploader is None:
                    raise ValueError("uploader is required when dry_run is false")
                source_object = uploader.upload_file(
                    path,
                    object_key=key,
                    is_primary=True,
                ).to_manifest_dict()
            source_files[0]["path_in_package"] = ""
        elif len(members) > 1:
            package_name = _package_file_name()
            key = _source_object_key(key_prefix, client_id, client_resource_id, package_name)
            with tempfile.TemporaryDirectory(prefix="resource_source_") as temp_dir:
                package_path = Path(temp_dir) / _local_package_file_name(package_name)
                members = _build_source_zip(entity, package_path)
                source_files = _source_files_from_members(members)
                if dry_run:
                    source_object = _planned_ref(
                        package_path,
                        storage_profile_id=resolved_profile_id,
                        object_key_value=key,
                        is_primary=True,
                    )
                else:
                    if uploader is None:
                        raise ValueError("uploader is required when dry_run is false")
                    source_object = uploader.upload_file(
                        package_path,
                        object_key=key,
                        is_primary=True,
                        content_type="application/zip",
                    ).to_manifest_dict()
                source_object["file_name"] = package_name
                source_object["file_format"] = "zip"

    provided_previews = []
    if include_previews:
        primary_used = False
        gallery_index = 1
        for preview in entity.previews:
            if not preview.path or not Path(preview.path).is_file():
                continue
            path = Path(preview.path)
            input_role = preview.role or "primary"
            use_primary = input_role == "primary" and not primary_used
            preview_name = _preview_name(preview, path, use_primary=use_primary, gallery_index=gallery_index)
            if use_primary:
                primary_used = True
            else:
                gallery_index += 1
            role = "primary" if use_primary else "gallery"
            key = _preview_object_key(key_prefix, client_id, client_resource_id, preview_name)
            is_primary = use_primary
            if dry_run:
                ref = _planned_ref(
                    path,
                    storage_profile_id=resolved_profile_id,
                    object_key_value=key,
                    is_primary=is_primary,
                )
            else:
                if uploader is None:
                    raise ValueError("uploader is required when dry_run is false")
                ref = uploader.upload_file(
                    path,
                    object_key=key,
                    is_primary=is_primary,
                ).to_manifest_dict()
            ref.update({
                "role": role,
                "width": preview.width,
                "height": preview.height,
                "strategy": preview.strategy.value if hasattr(preview.strategy, "value") else str(preview.strategy),
                "origin": "provided",
                "renderer": preview.renderer or "client",
            })
            provided_previews.append(ref)

    return build_processing_manifest(
        entity,
        client_id=client_id,
        source_object=source_object,
        source_files=source_files,
        provided_previews=provided_previews,
        provided_description=provided_description_from_entity(entity) if include_descriptions else None,
        package_object=package_object or None,
    )


def write_manifest_records(records: Iterable[dict[str, Any]], output_path: str | Path | None) -> int:
    count = 0
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
        return count

    for record in records:
        print(json.dumps(record, ensure_ascii=False))
        count += 1
    return count


def source_object_keys_from_manifest(manifest: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    source_object = manifest.get("source_object")
    if isinstance(source_object, dict) and source_object.get("object_key"):
        keys.append(str(source_object["object_key"]))
    for item in manifest.get("source_files") or []:
        if isinstance(item, dict) and item.get("object_key"):
            keys.append(str(item["object_key"]))
    for item in manifest.get("provided_previews") or []:
        if isinstance(item, dict) and item.get("object_key"):
            keys.append(str(item["object_key"]))
    return list(dict.fromkeys(keys))


def source_object_refs_from_manifest(manifest: dict[str, Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    source_object = manifest.get("source_object")
    source_storage_profile_id = ""
    if isinstance(source_object, dict):
        source_storage_profile_id = str(source_object.get("storage_profile_id") or "").strip()
        object_key = str(source_object.get("object_key") or "").strip()
        if object_key:
            refs.append({"storage_profile_id": source_storage_profile_id, "object_key": object_key})
    for section in ("source_files", "provided_previews"):
        for item in manifest.get(section) or []:
            if not isinstance(item, dict):
                continue
            object_key = str(item.get("object_key") or "").strip()
            if not object_key:
                continue
            refs.append(
                {
                    "storage_profile_id": str(item.get("storage_profile_id") or source_storage_profile_id).strip(),
                    "object_key": object_key,
                }
            )
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for ref in refs:
        unique = (ref["storage_profile_id"], ref["object_key"])
        if unique in seen:
            continue
        seen.add(unique)
        result.append(ref)
    return result


def package_object_ref_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    source_object = manifest.get("source_object") if isinstance(manifest, dict) else None
    if not isinstance(source_object, dict):
        return {}
    object_key_value = str(source_object.get("object_key") or "").strip()
    if not object_key_value:
        return {}
    return {
        "storage_profile_id": str(source_object.get("storage_profile_id") or ""),
        "object_key": object_key_value,
    }


def _thread_uploader(storage_profile_id: str) -> ObjectStorageUploader:
    uploaders = getattr(_THREAD_LOCAL, "uploaders", None)
    if uploaders is None:
        uploaders = {}
        _THREAD_LOCAL.uploaders = uploaders
    key = storage_profile_id or "__default__"
    uploader = uploaders.get(key)
    if uploader is None:
        uploader = ObjectStorageUploader(storage_profile_id=storage_profile_id or None)
        uploaders[key] = uploader
    return uploader


def _upload_entity_worker(
    entity: ResourceProcessingEntity,
    *,
    client_id: str,
    storage_profile_id: str,
    include_previews: bool,
    key_prefix: str,
    include_descriptions: bool,
    dry_run: bool,
    package_object: dict[str, Any] | None = None,
    reuse_source_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return upload_entity_objects(
        entity,
        uploader=None if dry_run else _thread_uploader(storage_profile_id),
        client_id=client_id,
        key_prefix=key_prefix,
        include_previews=include_previews,
        include_descriptions=include_descriptions,
        storage_profile_id=storage_profile_id,
        dry_run=dry_run,
        package_object=package_object,
        reuse_source_manifest=reuse_source_manifest,
    )


def _normalize_resource_types(resource_type: str = "", resource_types: Iterable[str] | None = None) -> list[str]:
    raw_values: list[str] = []
    if resource_type:
        raw_values.append(resource_type)
    if isinstance(resource_types, str):
        raw_values.append(resource_types)
    else:
        raw_values.extend(resource_types or [])
    values: list[str] = []
    for raw in raw_values:
        for part in str(raw or "").replace(";", ",").split(","):
            text = part.strip()
            if text:
                values.append(text)
    return list(dict.fromkeys(values))


def build_manifests_from_cache(
    *,
    db_path: str,
    client_id: str,
    storage_profile_id: str = "",
    include_previews: bool,
    key_prefix: str = "",
    include_descriptions: bool = False,
    dry_run: bool,
    resume: bool = False,
    force: bool = False,
    workers: int = 1,
    limit: int | None = None,
    resource_type: str = "",
    resource_types: Iterable[str] | None = None,
    process_states: Iterable[str] | None = None,
    min_task_id: int | None = None,
    max_task_id: int | None = None,
    preview_created_after: str = "",
    source_filter: str = "",
    defer_replaced_object_cleanup: bool = False,
    missing_manifest_only: bool = False,
    report: Report | None = None,
):
    from ResourceProcessor.cache.local_cache import LocalCacheStore

    cache = LocalCacheStore(os.path.abspath(db_path))
    worker_count = max(1, int(workers or 1))
    resolved_storage_profile_id = load_storage_profiles().get(storage_profile_id or None).profile_id
    uploader = None if dry_run or worker_count > 1 else ObjectStorageUploader(
        storage_profile_id=resolved_storage_profile_id or None,
    )
    resource_type_values = _normalize_resource_types(resource_type, resource_types)
    if report and resume and not force:
        report.ok("断点续传", "按资源指纹跳过未变化且已提交的资源；待提交 manifest 会继续返回")
    if report and worker_count > 1 and not dry_run:
        report.ok("并发上传", f"workers={worker_count}")
    skipped = 0
    skipped_clean = 0
    reused_objects = 0
    emitted_manifests = 0
    manifest_limit = int(limit or 0)
    cleanup_uploader = None

    def now() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    upload_options = upload_options_payload(
        client_id=client_id,
        storage_profile_id=resolved_storage_profile_id,
        key_prefix=key_prefix,
        include_previews=include_previews,
        include_descriptions=include_descriptions,
    )
    package_upload_options = upload_options_payload(
        client_id=client_id,
        storage_profile_id=resolved_storage_profile_id,
        key_prefix=key_prefix,
        include_previews=False,
        include_descriptions=False,
    )
    package_refs_by_task_id: dict[int, dict[str, Any]] = {}

    def output_limit_reached() -> bool:
        return manifest_limit > 0 and emitted_manifests >= manifest_limit

    def note_manifest_emitted() -> None:
        nonlocal emitted_manifests
        emitted_manifests += 1

    def cleanup_replaced_objects(task_id: int, old_manifest: dict[str, Any], new_manifest: dict[str, Any]) -> None:
        nonlocal cleanup_uploader
        old_keys = set(source_object_keys_from_manifest(old_manifest))
        new_keys = set(source_object_keys_from_manifest(new_manifest))
        keys = sorted(old_keys - new_keys)
        if not keys:
            return
        if dry_run:
            if report:
                report.ok("计划清理旧对象", f"task_id={task_id}, old_objects={len(keys)}")
            return
        if defer_replaced_object_cleanup:
            refs = [
                ref
                for ref in source_object_refs_from_manifest(old_manifest)
                if ref["object_key"] in keys
            ]
            timestamp = now()
            client_resource_id = str(
                new_manifest.get("client_resource_id")
                or old_manifest.get("client_resource_id")
                or task_id
            )
            cache._conn.execute(
                """INSERT INTO resource_object_delete_job
                   (client_resource_id, source_resource_id, task_id_snapshot,
                    storage_profile_id, object_keys_json, object_refs_json,
                    manifest_json_snapshot, status, attempt_count, last_error,
                    reason, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, '', ?, ?, ?)""",
                (
                    client_resource_id,
                    client_resource_id,
                    task_id,
                    resolved_storage_profile_id,
                    json.dumps(keys, ensure_ascii=False),
                    json.dumps(refs, ensure_ascii=False),
                    json.dumps(old_manifest, ensure_ascii=False),
                    "replaced_object_cleanup",
                    timestamp,
                    timestamp,
                ),
            )
            cache._conn.commit()
            if report:
                report.ok("旧对象清理入队", f"task_id={task_id}, old_objects={len(keys)}")
            return
        if cleanup_uploader is None:
            cleanup_uploader = ObjectStorageUploader(storage_profile_id=resolved_storage_profile_id or None)
        try:
            deleted = cleanup_uploader.delete_objects(keys)
            if report:
                report.ok("清理旧对象", f"task_id={task_id}, old_objects={deleted}")
        except Exception as exc:
            if report:
                report.fail("清理旧对象失败", f"task_id={task_id}: {str(exc)[:160]}")

    def handle_manifest(
        task_id: int,
        manifest: dict[str, Any],
        *,
        resource_fingerprint: str,
        object_fingerprint: str,
        submit_state: str = "pending",
        upload_options_value: dict[str, Any] | None = None,
    ):
        nonlocal skipped
        if not manifest["source_files"]:
            skipped += 1
            if report:
                report.fail("跳过", f"task_id={task_id} 没有可上传原始文件")
            return None
        if not dry_run:
            cache.upsert_object_manifest(
                task_id,
                manifest,
                submit_state=submit_state,
                resource_fingerprint=resource_fingerprint,
                object_fingerprint=object_fingerprint,
                upload_options=upload_options_value or upload_options,
            )
            cache.add_log(task_id, "object_manifest_saved", json.dumps(manifest, ensure_ascii=False))
        return task_id, manifest

    def plan_manifest(
        task_id: int,
        entity: ResourceProcessingEntity,
        *,
        include_previews_value: bool,
        include_descriptions_value: bool,
        package_object: dict[str, Any] | None = None,
    ):
        nonlocal skipped_clean, reused_objects
        object_fingerprint, _object_parts = object_fingerprint_for_entity(
            entity,
            client_id=client_id,
            storage_profile_id=resolved_storage_profile_id,
            key_prefix=key_prefix,
            include_previews=include_previews_value,
        )
        source_only_object_fingerprint, _source_only_parts = object_fingerprint_for_entity(
            entity,
            client_id=client_id,
            storage_profile_id=resolved_storage_profile_id,
            key_prefix=key_prefix,
            include_previews=False,
        )
        resource_fingerprint, _resource_parts = manifest_resource_fingerprint_for_entity(
            entity,
            client_id=client_id,
            storage_profile_id=resolved_storage_profile_id,
            key_prefix=key_prefix,
            include_previews=include_previews_value,
            include_descriptions=include_descriptions_value,
            object_fingerprint=object_fingerprint,
            package_object=package_object,
        )
        old = cache.get_object_manifest(task_id)
        if old and not force and old.get("resource_fingerprint") == resource_fingerprint:
            if (
                old.get("submit_state") == "submitted"
                and old.get("committed_fingerprint") == resource_fingerprint
            ):
                skipped_clean += 1
                return {
                    "action": "skip",
                    "resource_fingerprint": resource_fingerprint,
                    "object_fingerprint": object_fingerprint,
                    "old_manifest": old.get("manifest") or {},
                }
            reused_objects += 1
            return {
                "action": "reuse_pending",
                "manifest": old.get("manifest") or {},
                "resource_fingerprint": resource_fingerprint,
                "object_fingerprint": object_fingerprint,
                "old_manifest": old.get("manifest") or {},
            }

        object_dirty = force or not old or old.get("object_fingerprint") != object_fingerprint
        if old and not object_dirty:
            reused_objects += 1
            return {
                "action": "reuse_objects",
                "manifest": _manifest_reusing_objects(
                    entity,
                    old.get("manifest") or {},
                    client_id=client_id,
                    include_descriptions=include_descriptions_value,
                    package_object=package_object,
                ),
                "resource_fingerprint": resource_fingerprint,
                "object_fingerprint": object_fingerprint,
                "old_manifest": old.get("manifest") or {},
            }
        if (
            old
            and not force
            and include_previews_value
            and entity.previews
            and old.get("object_fingerprint") == source_only_object_fingerprint
        ):
            return {
                "action": "upload_previews",
                "resource_fingerprint": resource_fingerprint,
                "object_fingerprint": object_fingerprint,
                "old_manifest": old.get("manifest") or {},
            }
        return {
            "action": "upload",
            "resource_fingerprint": resource_fingerprint,
            "object_fingerprint": object_fingerprint,
            "source_only_object_fingerprint": source_only_object_fingerprint,
            "old_manifest": (old.get("manifest") or {}) if old else {},
        }

    def handle_failure(task_id: int, exc: Exception) -> None:
        cache.record_task_error(task_id, "object_storage_upload_error", str(exc)[:1000])
        if report:
            report.fail("上传失败", f"task_id={task_id}: {str(exc)[:160]}")

    def find_package_task_id(entity: ResourceProcessingEntity) -> int | None:
        if entity.resource_type == PACK_RESOURCE_TYPE:
            return None
        parent = str(entity.parent_resource_id or "").strip()
        if parent:
            params: list[Any] = [PACK_RESOURCE_TYPE, parent]
            where = "resource_type = ? AND source_resource_id = ?"
            try:
                params.append(int(parent))
                where = f"({where} OR (resource_type = ? AND id = ?))"
                params = [PACK_RESOURCE_TYPE, parent, PACK_RESOURCE_TYPE, int(parent)]
            except ValueError:
                pass
            row = cache._conn.execute(
                f"SELECT id FROM resource_task WHERE {where} ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
            if row:
                return int(row["id"])
        if entity.source and entity.pack_name:
            row = cache._conn.execute(
                """SELECT id FROM resource_task
                   WHERE resource_type = ? AND source = ? AND pack_name = ?
                   ORDER BY id DESC LIMIT 1""",
                (PACK_RESOURCE_TYPE, entity.source, entity.pack_name),
            ).fetchone()
            if row:
                return int(row["id"])
        return None

    def ensure_package_object(pack_task_id: int) -> dict[str, Any]:
        if pack_task_id in package_refs_by_task_id:
            return package_refs_by_task_id[pack_task_id]
        pack_entity = cache.rebuild_entity_from_cache(pack_task_id)
        if pack_entity is None or not pack_entity.files:
            package_refs_by_task_id[pack_task_id] = {}
            return {}
        decision = plan_manifest(
            pack_task_id,
            pack_entity,
            include_previews_value=False,
            include_descriptions_value=False,
        )
        manifest = decision.get("manifest") or decision.get("old_manifest") or {}
        if decision["action"] == "upload":
            package_uploader = None if dry_run else (uploader or _thread_uploader(resolved_storage_profile_id))
            manifest = upload_entity_objects(
                pack_entity,
                uploader=package_uploader,
                client_id=client_id,
                key_prefix=key_prefix,
                include_previews=False,
                include_descriptions=False,
                storage_profile_id=resolved_storage_profile_id,
                dry_run=dry_run,
            )
            result = handle_manifest(
                pack_task_id,
                manifest,
                resource_fingerprint=decision["resource_fingerprint"],
                object_fingerprint=decision["object_fingerprint"],
                submit_state="package_only",
                upload_options_value=package_upload_options,
            )
            if result is not None:
                cleanup_replaced_objects(pack_task_id, decision["old_manifest"], manifest)
        elif decision["action"] in {"reuse_pending", "reuse_objects"}:
            handle_manifest(
                pack_task_id,
                manifest,
                resource_fingerprint=decision["resource_fingerprint"],
                object_fingerprint=decision["object_fingerprint"],
                submit_state="package_only",
                upload_options_value=package_upload_options,
            )
        ref = package_object_ref_from_manifest(manifest)
        package_refs_by_task_id[pack_task_id] = ref
        return ref

    def package_object_for_entity(entity: ResourceProcessingEntity) -> dict[str, Any]:
        pack_task_id = find_package_task_id(entity)
        if pack_task_id is None:
            return {}
        return ensure_package_object(pack_task_id)

    try:
        task_ids = cache.iter_tasks(
            resource_types=resource_type_values,
            process_states=process_states,
            min_task_id=min_task_id,
            max_task_id=max_task_id,
            preview_created_after=preview_created_after,
            source=source_filter,
            exclude_uploaded_object_manifest=missing_manifest_only,
        )
        if worker_count <= 1 or dry_run:
            for task_id in task_ids:
                if output_limit_reached():
                    break
                entity = cache.rebuild_entity_from_cache(task_id)
                if entity is None or not entity.files:
                    skipped += 1
                    continue
                try:
                    if entity.resource_type == PACK_RESOURCE_TYPE:
                        ensure_package_object(task_id)
                        continue
                    if not is_search_indexable_resource_type(entity.resource_type):
                        skipped += 1
                        continue
                    package_object = package_object_for_entity(entity)
                    decision = plan_manifest(
                        task_id,
                        entity,
                        include_previews_value=include_previews,
                        include_descriptions_value=include_descriptions,
                        package_object=package_object,
                    )
                    if decision["action"] == "skip":
                        continue
                    if decision["action"] in {"reuse_pending", "reuse_objects"}:
                        result = handle_manifest(
                            task_id,
                            decision["manifest"],
                            resource_fingerprint=decision["resource_fingerprint"],
                            object_fingerprint=decision["object_fingerprint"],
                        )
                        if result is not None:
                            note_manifest_emitted()
                            yield result
                            if output_limit_reached():
                                break
                        continue
                    reuse_source_manifest = (
                        decision["old_manifest"]
                        if decision["action"] == "upload_previews"
                        else None
                    )
                    manifest = upload_entity_objects(
                        entity,
                        uploader=uploader,
                        client_id=client_id,
                        key_prefix=key_prefix,
                        include_previews=include_previews,
                        include_descriptions=include_descriptions,
                        storage_profile_id=resolved_storage_profile_id,
                        dry_run=dry_run,
                        package_object=package_object,
                        reuse_source_manifest=reuse_source_manifest,
                    )
                    result = handle_manifest(
                        task_id,
                        manifest,
                        resource_fingerprint=decision["resource_fingerprint"],
                        object_fingerprint=decision["object_fingerprint"],
                    )
                    if result is not None:
                        cleanup_replaced_objects(task_id, decision["old_manifest"], manifest)
                        note_manifest_emitted()
                        yield result
                        if output_limit_reached():
                            break
                except Exception as exc:
                    handle_failure(task_id, exc)
            return

        pending: dict[Future, tuple[int, dict[str, Any]]] = {}

        def drain(done):
            for future in done:
                task_id, decision = pending.pop(future)
                try:
                    manifest = future.result()
                    result = handle_manifest(
                        task_id,
                        manifest,
                        resource_fingerprint=decision["resource_fingerprint"],
                        object_fingerprint=decision["object_fingerprint"],
                    )
                    if result is not None:
                        cleanup_replaced_objects(task_id, decision["old_manifest"], manifest)
                        note_manifest_emitted()
                        yield result
                except Exception as exc:
                    handle_failure(task_id, exc)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            max_pending = worker_count * 2
            if manifest_limit > 0:
                max_pending = min(max_pending, manifest_limit)
            for task_id in task_ids:
                while manifest_limit > 0 and emitted_manifests + len(pending) >= manifest_limit and pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    yield from drain(done)
                if output_limit_reached():
                    break
                entity = cache.rebuild_entity_from_cache(task_id)
                if entity is None or not entity.files:
                    skipped += 1
                    continue
                if entity.resource_type == PACK_RESOURCE_TYPE:
                    ensure_package_object(task_id)
                    continue
                if not is_search_indexable_resource_type(entity.resource_type):
                    skipped += 1
                    continue
                package_object = package_object_for_entity(entity)
                decision = plan_manifest(
                    task_id,
                    entity,
                    include_previews_value=include_previews,
                    include_descriptions_value=include_descriptions,
                    package_object=package_object,
                )
                if decision["action"] == "skip":
                    continue
                if decision["action"] in {"reuse_pending", "reuse_objects"}:
                    result = handle_manifest(
                        task_id,
                        decision["manifest"],
                        resource_fingerprint=decision["resource_fingerprint"],
                        object_fingerprint=decision["object_fingerprint"],
                    )
                    if result is not None:
                        note_manifest_emitted()
                        yield result
                        if output_limit_reached():
                            break
                    continue
                reuse_source_manifest = (
                    decision["old_manifest"]
                    if decision["action"] == "upload_previews"
                    else None
                )
                future = executor.submit(
                    _upload_entity_worker,
                    entity,
                    client_id=client_id,
                    storage_profile_id=resolved_storage_profile_id,
                    include_previews=include_previews,
                    key_prefix=key_prefix,
                    include_descriptions=include_descriptions,
                    dry_run=False,
                    package_object=package_object,
                    reuse_source_manifest=reuse_source_manifest,
                )
                pending[future] = (task_id, decision)
                if len(pending) >= max_pending:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    yield from drain(done)

            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                yield from drain(done)
    finally:
        if report and skipped:
            report.ok("跳过", f"{skipped} 个资源未生成 manifest")
        if report and skipped_clean:
            report.ok("指纹未变化", f"{skipped_clean} 个资源已提交，跳过")
        if report and reused_objects:
            report.ok("复用对象", f"{reused_objects} 个资源无需重传桶对象")
        cache.close()
