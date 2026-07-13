from __future__ import annotations

import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable


DEFAULT_MAX_ZIP_MEMBERS = 512
DEFAULT_MAX_ZIP_MEMBER_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_ZIP_EXTRACT_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_ZIP_COMPRESSION_RATIO = 100.0


def _attr(ref, name: str, default: str = ""):
    if isinstance(ref, dict):
        return ref.get(name, default)
    return getattr(ref, name, default)


def safe_zip_member_name(raw: str) -> str:
    name = str(raw or "").replace("\\", "/").strip()
    path = PurePosixPath(name)
    if not name or path.is_absolute():
        raise RuntimeError(f"unsafe package path: {raw}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimeError(f"unsafe package path: {raw}")
    if path.parts and ":" in path.parts[0]:
        raise RuntimeError(f"unsafe package path: {raw}")
    return path.as_posix()


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == 0o120000


def _validate_target_under_root(target: Path, root: Path, member_name: str) -> None:
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"unsafe zip member: {member_name}") from exc


def resolve_local_source_files(
    source_object: Path,
    source_files: Iterable,
    source_dir: Path,
    *,
    max_zip_members: int = DEFAULT_MAX_ZIP_MEMBERS,
    max_zip_member_bytes: int = DEFAULT_MAX_ZIP_MEMBER_BYTES,
    max_zip_extract_bytes: int = DEFAULT_MAX_ZIP_EXTRACT_BYTES,
    max_zip_compression_ratio: float = DEFAULT_MAX_ZIP_COMPRESSION_RATIO,
) -> list[Path]:
    refs = list(source_files)
    if not refs:
        return []
    if len(refs) == 1:
        if source_object.suffix.lower() != ".zip":
            return [source_object]
        path_in_package = str(_attr(refs[0], "path_in_package") or "").strip()
        file_name = str(_attr(refs[0], "file_name") or "").strip()
        if not path_in_package and file_name.casefold() == source_object.name.casefold():
            return [source_object]
    if len(refs) > max_zip_members:
        raise RuntimeError(f"too many package members requested: {len(refs)}")

    extract_dir = source_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)
    resolved_extract_dir = extract_dir.resolve()

    local_paths: list[Path] = []
    total_size = 0
    requested_names = [
        safe_zip_member_name(_attr(ref, "path_in_package") or _attr(ref, "file_name"))
        for ref in refs
    ]
    requested_set = set(requested_names)

    with zipfile.ZipFile(source_object) as zf:
        infos = zf.infolist()
        if len(infos) > max_zip_members:
            raise RuntimeError(f"too many package members in archive: {len(infos)}")
        info_by_name: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            if info.is_dir():
                continue
            try:
                normalized = safe_zip_member_name(info.filename)
            except RuntimeError:
                continue
            if normalized not in requested_set:
                continue
            if normalized in info_by_name:
                raise RuntimeError(f"duplicate package member: {normalized}")
            info_by_name[normalized] = info
        for ref in refs:
            member_name = requested_names.pop(0)
            info = info_by_name.get(member_name)
            if info is None:
                raise RuntimeError(f"package member not found: {member_name}")
            if info.is_dir() or _is_zip_symlink(info):
                raise RuntimeError(f"unsupported zip member: {member_name}")
            if info.file_size > max_zip_member_bytes:
                raise RuntimeError(f"zip member too large: {member_name}")
            total_size += int(info.file_size or 0)
            if total_size > max_zip_extract_bytes:
                raise RuntimeError("zip extracted size exceeds limit")
            if info.compress_size > 0:
                ratio = float(info.file_size or 0) / float(info.compress_size)
                if ratio > max_zip_compression_ratio:
                    raise RuntimeError(f"zip compression ratio exceeds limit: {member_name}")

            target = (extract_dir / member_name).resolve()
            _validate_target_under_root(target, resolved_extract_dir, member_name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            local_paths.append(target)

    return local_paths
