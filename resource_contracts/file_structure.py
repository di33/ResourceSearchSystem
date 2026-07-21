from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Iterable

from resource_contracts.source_files import safe_zip_member_name


DEFAULT_MAX_STRUCTURE_ENTRIES = 100_000


def normalize_structure_entry(raw: Any) -> dict[str, Any]:
    get = raw.get if isinstance(raw, dict) else lambda key, default=None: getattr(raw, key, default)
    path = safe_zip_member_name(get("path") or get("path_in_package") or get("file_name") or "")
    name = str(get("name") or get("file_name") or Path(path).name).strip()
    if not name:
        raise ValueError("file structure entry name must not be blank")
    size = int(get("size") or get("file_size") or 0)
    if size < 0:
        raise ValueError("file structure entry size must not be negative")
    file_format = str(get("format") or get("file_format") or "").lower().lstrip(".")
    if not file_format and "." in name:
        file_format = name.rsplit(".", 1)[-1].lower()
    return {
        "path": path,
        "name": name,
        "type": "file",
        "size": size,
        "format": file_format,
        "checksum": str(get("checksum") or ""),
        "is_primary": bool(get("is_primary") or False),
    }


def build_file_structure(
    entries: Iterable[Any],
    *,
    source: str,
    source_object_checksum: str = "",
    max_entries: int = DEFAULT_MAX_STRUCTURE_ENTRIES,
) -> dict[str, Any]:
    normalized = [normalize_structure_entry(item) for item in entries]
    if max_entries > 0 and len(normalized) > max_entries:
        raise ValueError(f"file structure contains too many entries: {len(normalized)}")
    paths = [item["path"] for item in normalized]
    if len(paths) != len(set(paths)):
        raise ValueError("file structure contains duplicate paths")
    if normalized and not any(item["is_primary"] for item in normalized):
        normalized[0]["is_primary"] = True
    return {
        "source": str(source or "processor"),
        "state": "complete",
        "source_object_checksum": str(source_object_checksum or ""),
        "entry_count": len(normalized),
        "total_size": sum(item["size"] for item in normalized),
        "entries": normalized,
    }


def validate_file_structure(value: Any, *, max_entries: int = DEFAULT_MAX_STRUCTURE_ENTRIES) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, dict):
        raise ValueError("file_structure must be an object")
    structure = build_file_structure(
        value.get("entries") or [],
        source=str(value.get("source") or "client"),
        source_object_checksum=str(value.get("source_object_checksum") or ""),
        max_entries=max_entries,
    )
    declared_count = value.get("entry_count")
    if declared_count is not None and int(declared_count) != structure["entry_count"]:
        raise ValueError("file_structure.entry_count does not match entries")
    declared_size = value.get("total_size")
    if declared_size is not None and int(declared_size) != structure["total_size"]:
        raise ValueError("file_structure.total_size does not match entries")
    return structure


def scan_source_file_structure(
    source_object: str | Path,
    *,
    checksum: str = "",
    max_entries: int = DEFAULT_MAX_STRUCTURE_ENTRIES,
) -> dict[str, Any]:
    path = Path(source_object)
    if path.suffix.lower() != ".zip":
        return build_file_structure(
            [{"path": path.name, "name": path.name, "size": path.stat().st_size, "is_primary": True}],
            source="processor",
            source_object_checksum=checksum,
            max_entries=max_entries,
        )
    entries: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            try:
                member = safe_zip_member_name(info.filename)
            except RuntimeError:
                continue
            entries.append({
                "path": member,
                "name": Path(member).name,
                "size": int(info.file_size or 0),
                "format": Path(member).suffix.lower().lstrip("."),
            })
            if max_entries > 0 and len(entries) > max_entries:
                raise ValueError(f"file structure contains too many entries: more than {max_entries}")
    return build_file_structure(
        entries,
        source="processor",
        source_object_checksum=checksum,
        max_entries=max_entries,
    )
