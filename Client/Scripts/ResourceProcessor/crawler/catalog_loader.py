from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

# Windows replaces these characters in directory/file names at extraction time
_WIN_UNSAFE_CHARS = '<>:"/\\|?*'


def _sanitize_windows_name(name: str) -> str:
    """Replace characters that Windows converts to '_' in directory/file names."""
    table = str.maketrans({ch: "_" for ch in _WIN_UNSAFE_CHARS})
    return name.translate(table)


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _iter_jsonl(path: Path, *, skip_bad: bool = False) -> Iterator[dict[str, Any]]:
    """逐行解析 JSONL。``skip_bad=True`` 时跳过损坏行并打日志，避免整批流水线被单行脏数据拖死。"""
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                if skip_bad:
                    logger.warning(
                        "跳过损坏的 JSONL 行 %s:%d: %s | 片段=%r",
                        path.name,
                        lineno,
                        exc,
                        line[:240],
                    )
                    continue
                raise


@dataclass
class CrawlerAssetRecord:
    asset_id: str
    file_path: str
    source: str
    pack_name: str
    fmt: str = ""
    style: str = ""
    theme: str = ""

    @property
    def format(self) -> str:
        if self.fmt:
            return self.fmt
        return Path(self.file_path).suffix.lstrip(".").lower()


@dataclass
class CrawlerResourceRecord:
    raw: dict[str, Any]
    pack_metadata: dict[str, Any]
    assets: list[CrawlerAssetRecord]
    resolved_files: list[str]
    missing_files: list[str]

    @property
    def id(self) -> str:
        return str(self.raw.get("id", ""))

    @property
    def source(self) -> str:
        return str(self.raw.get("source", ""))

    @property
    def pack_id(self) -> str:
        return str(self.raw.get("pack_id", ""))

    @property
    def pack_name(self) -> str:
        return str(self.raw.get("pack_name", ""))

    @property
    def resource_type(self) -> str:
        return str(self.raw.get("resource_type", ""))

    @property
    def title(self) -> str:
        return str(self.raw.get("title", ""))

    @property
    def resource_path(self) -> str:
        return str(self.raw.get("resource_path", ""))

    @property
    def parent_resource_id(self) -> str:
        return str(self.raw.get("parent_resource_id", ""))

    @property
    def child_resource_ids(self) -> list[str]:
        value = self.raw.get("child_resource_ids", [])
        return [str(v) for v in value] if isinstance(value, list) else []

    @property
    def child_resource_count(self) -> int:
        value = self.raw.get("child_resource_count", len(self.child_resource_ids))
        try:
            return int(value)
        except (TypeError, ValueError):
            return len(self.child_resource_ids)

    @property
    def contains_resource_types(self) -> list[str]:
        value = self.raw.get("contains_resource_types", [])
        return [str(v) for v in value] if isinstance(value, list) else []

    @property
    def file_paths(self) -> list[str]:
        value = self.raw.get("file_paths", [])
        return [str(v) for v in value] if isinstance(value, list) else []

    @property
    def asset_ids(self) -> list[str]:
        value = self.raw.get("asset_ids", [])
        return [str(v) for v in value] if isinstance(value, list) else []

    @property
    def tags(self) -> list[str]:
        value = self.raw.get("tags", [])
        return [str(v) for v in value] if isinstance(value, list) else []

    @property
    def description(self) -> str:
        return str(self.raw.get("description", ""))

    @property
    def category(self) -> str:
        return str(self.raw.get("category", ""))

    @property
    def license_name(self) -> str:
        return str(self.raw.get("license", ""))

    @property
    def member_count(self) -> int:
        value = self.raw.get("member_count", len(self.file_paths))
        try:
            return int(value)
        except (TypeError, ValueError):
            return len(self.file_paths)

    @property
    def source_url(self) -> str:
        return str(self.raw.get("source_url", ""))

    @property
    def download_url(self) -> str:
        return str(self.raw.get("download_url", ""))

    @property
    def pack_description(self) -> str:
        pack = self.pack_metadata.get("pack", {})
        return str(pack.get("description", ""))

    @property
    def pack_tags(self) -> list[str]:
        pack = self.pack_metadata.get("pack", {})
        value = pack.get("tags", [])
        return [str(v) for v in value] if isinstance(value, list) else []


class CrawlerCatalog:
    def __init__(self, output_root: str, db_path: str | None = None):
        self.output_root = Path(output_root).resolve()
        self.assets_root = self.output_root / "assets"
        self.metadata_root = self.output_root / "metadata"
        self.resource_index_path = self.metadata_root / "resource_index.jsonl"

        if db_path is None:
            project_root = Path(__file__).resolve().parents[4]
            db_path = str(project_root / "pipeline.db")
        self._db_path = os.path.abspath(db_path)

        if not os.path.isfile(self._db_path):
            raise FileNotFoundError(
                f"数据库不存在: {self._db_path}\n"
                f"请先执行: python Client/Scripts/build_asset_index.py "
                f"--db-path {self._db_path} --index-jsonl <index.jsonl路径>"
            )
        # Verify asset_index table exists
        conn = self._open_conn()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='asset_index'"
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    f"数据库 {self._db_path} 中没有 asset_index 表\n"
                    f"请先执行: python Client/Scripts/build_asset_index.py "
                    f"--db-path {self._db_path} --index-jsonl <index.jsonl路径>"
                )
        finally:
            conn.close()

        self._pack_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def _open_conn(self):
        conn = sqlite3.connect(self._db_path, timeout=300)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=300000")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        return conn

    def _require_file(self, path: Path, label: str) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"缺少 {label}: {path}")

    def _query_asset(self, asset_id: str) -> CrawlerAssetRecord | None:
        conn = self._open_conn()
        try:
            row = conn.execute(
                "SELECT asset_id, file_path, source, pack_name, fmt, style, theme "
                "FROM asset_index WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            if row is None:
                return None
            return CrawlerAssetRecord(
                asset_id=row[0],
                file_path=row[1],
                source=row[2],
                pack_name=row[3],
                fmt=row[4],
                style=row[5],
                theme=row[6],
            )
        finally:
            conn.close()

    def _query_assets_by_path(
        self, source: str, pack_name: str, file_path: str
    ) -> list[CrawlerAssetRecord]:
        conn = self._open_conn()
        try:
            rows = conn.execute(
                "SELECT asset_id, file_path, source, pack_name, fmt, style, theme "
                "FROM asset_index WHERE source = ? AND pack_name = ? AND file_path = ?",
                (source, pack_name, file_path),
            ).fetchall()
            return [
                CrawlerAssetRecord(
                    asset_id=r[0], file_path=r[1], source=r[2],
                    pack_name=r[3], fmt=r[4], style=r[5], theme=r[6],
                )
                for r in rows
            ]
        finally:
            conn.close()

    def get_pack_metadata(self, source: str, pack_name: str) -> dict[str, Any]:
        key = (source, pack_name)
        if key in self._pack_cache:
            return self._pack_cache[key]
        pack_name_safe = _sanitize_windows_name(pack_name)
        pack_path = self.metadata_root / source / f"{pack_name_safe}.json"
        if pack_path.is_file():
            self._pack_cache[key] = _read_json(pack_path)
        else:
            self._pack_cache[key] = {}
        return self._pack_cache[key]

    def resolve_asset_file(self, source: str, pack_name: str, file_path: str) -> str:
        pack_name = _sanitize_windows_name(pack_name)
        return str((self.assets_root / source / pack_name / Path(file_path)).resolve())

    def _resolve_assets(self, resource_entry: dict[str, Any]) -> list[CrawlerAssetRecord]:
        file_paths = resource_entry.get("file_paths", []) or []
        asset_ids = resource_entry.get("asset_ids", []) or []
        assets: list[CrawlerAssetRecord] = []
        seen_ids: set[str] = set()

        for asset_id in asset_ids:
            asset = self._query_asset(str(asset_id))
            if asset is not None:
                assets.append(asset)
                seen_ids.add(asset.asset_id)

        if assets:
            return assets

        source = str(resource_entry.get("source", ""))
        pack_name = str(resource_entry.get("pack_name", ""))
        for file_path in file_paths:
            for asset in self._query_assets_by_path(source, pack_name, file_path):
                if asset.asset_id in seen_ids:
                    continue
                assets.append(asset)
                seen_ids.add(asset.asset_id)
                break
        return assets

    def iter_resources(
        self,
        limit: Optional[int] = None,
        resource_type: str = "",
        source_filter: str = "",
    ) -> Iterator[CrawlerResourceRecord]:
        yielded = 0
        wanted_type = resource_type.strip().lower()
        wanted_source = source_filter.strip().lower()

        self._require_file(self.resource_index_path, "crawler 资源索引 resource_index.jsonl")
        for entry in _iter_jsonl(self.resource_index_path, skip_bad=True):
            source = str(entry.get("source", "")).lower()
            current_type = str(entry.get("resource_type", "")).lower()
            if wanted_type and current_type != wanted_type:
                continue
            if wanted_source and source != wanted_source:
                continue

            pack_metadata = self.get_pack_metadata(
                str(entry.get("source", "")),
                str(entry.get("pack_name", "")),
            )
            file_paths = [str(v) for v in entry.get("file_paths", []) or []]
            resolved_files: list[str] = []
            missing_files: list[str] = []
            for file_path in file_paths:
                abs_path = self.resolve_asset_file(
                    str(entry.get("source", "")),
                    str(entry.get("pack_name", "")),
                    file_path,
                )
                if os.path.isfile(abs_path):
                    resolved_files.append(abs_path)
                else:
                    missing_files.append(file_path)

            yield CrawlerResourceRecord(
                raw=entry,
                pack_metadata=pack_metadata,
                assets=self._resolve_assets(entry),
                resolved_files=resolved_files,
                missing_files=missing_files,
            )
            yielded += 1
            if limit is not None and yielded >= limit:
                return


def load_crawler_catalog(output_root: str, db_path: str | None = None) -> CrawlerCatalog:
    return CrawlerCatalog(output_root, db_path=db_path)


def load_crawler_resources(
    output_root: str,
    limit: Optional[int] = None,
    resource_type: str = "",
    source_filter: str = "",
) -> list[CrawlerResourceRecord]:
    catalog = load_crawler_catalog(output_root)
    return list(
        catalog.iter_resources(
            limit=limit,
            resource_type=resource_type,
            source_filter=source_filter,
        )
    )
