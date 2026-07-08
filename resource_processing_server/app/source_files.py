from __future__ import annotations

from pathlib import Path

from resource_contracts.source_files import resolve_local_source_files as _resolve_local_source_files
from resource_processing_server.app.config import settings


def resolve_local_source_files(source_object: Path, source_files, source_dir: Path) -> list[Path]:
    return _resolve_local_source_files(
        source_object,
        source_files,
        source_dir,
        max_zip_members=settings.max_zip_members,
        max_zip_member_bytes=settings.max_zip_member_bytes,
        max_zip_extract_bytes=settings.max_zip_extract_bytes,
        max_zip_compression_ratio=settings.max_zip_compression_ratio,
    )
