from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

# Windows replaces these characters in directory/file names at extraction time
_WIN_UNSAFE_CHARS = '<>:"/\\|?*'
DEFAULT_CRAWLER_OUTPUT = r"K:\ResourceCrawler\output"
DEFAULT_CRAWLER_STATE_DB = r"G:\ResourceCrawler\data\crawler_state.db"


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


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value if v is not None)
    return str(value)


def _metadata_value(metadata: dict[str, Any], index: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if value in (None, ""):
        value = index.get(key)
    return _text_value(value)


def _chunks(values: list[str], size: int = 500) -> Iterator[list[str]]:
    for i in range(0, len(values), size):
        yield values[i:i + size]


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
    """Read ResourceCrawler state directly from crawler_state.db.

    The old JSONL resource index flow has been retired. ``output_root`` is still
    used to resolve files under ``assets/`` and optional pack metadata under
    ``metadata/``.
    """

    def __init__(
        self,
        output_root: str | None = None,
        db_path: str | None = None,
        *,
        crawler_state_db: str | None = None,
    ):
        output_root = output_root or DEFAULT_CRAWLER_OUTPUT
        self.output_root = Path(output_root).resolve()
        self.assets_root = self.output_root / "assets"
        self.metadata_root = self.output_root / "metadata"

        # ``db_path`` is kept only for older call sites; new code should pass
        # crawler_state_db explicitly.
        crawler_state_db = crawler_state_db or db_path or DEFAULT_CRAWLER_STATE_DB
        self._crawler_state_db = os.path.abspath(crawler_state_db)

        if not os.path.isfile(self._crawler_state_db):
            raise FileNotFoundError(f"crawler_state.db 不存在: {self._crawler_state_db}")

        conn = self._open_conn()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('assets', 'resource_index')"
            ).fetchall()
            names = {row["name"] for row in rows}
            missing = {"assets", "resource_index"} - names
            if missing:
                raise RuntimeError(
                    f"数据库 {self._crawler_state_db} 缺少表: {', '.join(sorted(missing))}"
                )
        finally:
            conn.close()

        self._pack_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def _open_conn(self):
        uri = f"{Path(self._crawler_state_db).resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=300)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=300000")
        return conn

    def _require_file(self, path: Path, label: str) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"缺少 {label}: {path}")

    def _asset_from_row(self, row: sqlite3.Row) -> CrawlerAssetRecord:
        metadata = _json_object(row["metadata_json"])
        index = _json_object(row["index_json"])
        file_path = _text_value(row["file_path"])
        fmt = _metadata_value(metadata, index, "format").lower()
        if not fmt:
            fmt = Path(file_path).suffix.lstrip(".").lower()
        return CrawlerAssetRecord(
            asset_id=_text_value(row["id"]),
            file_path=file_path,
            source=_text_value(row["source"] or index.get("source")),
            pack_name=_text_value(row["source_pack"] or index.get("source_pack") or index.get("pack_name")),
            fmt=fmt,
            style=_metadata_value(metadata, index, "style"),
            theme=_metadata_value(metadata, index, "theme"),
        )

    def _query_assets_by_ids(self, asset_ids: list[str]) -> list[CrawlerAssetRecord]:
        if not asset_ids:
            return []
        rows_by_id: dict[str, CrawlerAssetRecord] = {}
        conn = self._open_conn()
        try:
            for chunk in _chunks(asset_ids):
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT id, file_path, source, source_pack, metadata_json, index_json "
                    f"FROM assets WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    asset = self._asset_from_row(row)
                    rows_by_id[asset.asset_id] = asset
        finally:
            conn.close()
        return [rows_by_id[asset_id] for asset_id in asset_ids if asset_id in rows_by_id]

    def _query_assets_by_path(
        self, source: str, pack_name: str, file_path: str
    ) -> list[CrawlerAssetRecord]:
        return self._query_assets_by_paths(source, pack_name, [file_path])

    def _query_assets_by_paths(
        self, source: str, pack_name: str, file_paths: list[str]
    ) -> list[CrawlerAssetRecord]:
        if not file_paths:
            return []
        seen_paths: set[str] = set()
        ordered_paths: list[str] = []
        for file_path in file_paths:
            if file_path in seen_paths:
                continue
            seen_paths.add(file_path)
            ordered_paths.append(file_path)

        rows_by_path: dict[str, CrawlerAssetRecord] = {}
        conn = self._open_conn()
        try:
            for chunk in _chunks(ordered_paths):
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT id, file_path, source, source_pack, metadata_json, index_json "
                    "FROM assets WHERE source = ? AND source_pack = ? "
                    f"AND file_path IN ({placeholders})",
                    [source, pack_name, *chunk],
                ).fetchall()
                for row in rows:
                    file_path = _text_value(row["file_path"])
                    if file_path not in rows_by_path:
                        rows_by_path[file_path] = self._asset_from_row(row)
        finally:
            conn.close()
        return [rows_by_path[file_path] for file_path in ordered_paths if file_path in rows_by_path]

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

        for asset in self._query_assets_by_ids([str(asset_id) for asset_id in asset_ids]):
            if asset.asset_id in seen_ids:
                continue
            assets.append(asset)
            seen_ids.add(asset.asset_id)

        if assets:
            return assets

        source = str(resource_entry.get("source", ""))
        pack_name = str(resource_entry.get("pack_name", ""))
        for asset in self._query_assets_by_paths(source, pack_name, [str(v) for v in file_paths]):
            if asset.asset_id in seen_ids:
                continue
            assets.append(asset)
            seen_ids.add(asset.asset_id)
        return assets

    def _entry_from_resource_row(self, row: sqlite3.Row) -> dict[str, Any]:
        entry = _json_object(row["record_json"])
        for key in (
            "id",
            "pack_id",
            "source",
            "pack_name",
            "resource_type",
            "title",
            "resource_path",
            "parent_resource_id",
        ):
            if entry.get(key) in (None, ""):
                entry[key] = _text_value(row[key])
        if entry.get("member_count") in (None, ""):
            entry["member_count"] = row["member_count"] or 0
        if row["group_name"] and entry.get("group_name") in (None, ""):
            entry["group_name"] = _text_value(row["group_name"])
        entry.setdefault("file_paths", [])
        entry.setdefault("asset_ids", [])
        return entry

    def iter_resources(
        self,
        limit: Optional[int] = None,
        resource_type: str = "",
        source_filter: str = "",
    ) -> Iterator[CrawlerResourceRecord]:
        yielded = 0
        wanted_type = resource_type.strip().lower()
        wanted_source = source_filter.strip().lower()
        seen_ids: set[str] = set()

        sql = [
            "SELECT id, pack_id, source, pack_name, resource_type, title, resource_path,",
            "group_name, parent_resource_id, member_count, record_json",
            "FROM resource_index WHERE 1 = 1",
        ]
        params: list[Any] = []
        if wanted_type:
            sql.append("AND lower(resource_type) = ?")
            params.append(wanted_type)
        if wanted_source:
            sql.append("AND lower(source) = ?")
            params.append(wanted_source)
        sql.append("ORDER BY row_id")

        conn = self._open_conn()
        try:
            rows = conn.execute(" ".join(sql), params)
            for row in rows:
                entry = self._entry_from_resource_row(row)
                # 去重：跳过重复 ID
                rid = str(entry.get("id", ""))
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)

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
        finally:
            conn.close()


def load_crawler_catalog(
    output_root: str | None = None,
    db_path: str | None = None,
    *,
    crawler_state_db: str | None = None,
) -> CrawlerCatalog:
    return CrawlerCatalog(output_root, db_path=db_path, crawler_state_db=crawler_state_db)


def load_crawler_resources(
    output_root: str | None = None,
    limit: Optional[int] = None,
    resource_type: str = "",
    source_filter: str = "",
    crawler_state_db: str | None = None,
) -> list[CrawlerResourceRecord]:
    catalog = load_crawler_catalog(output_root, crawler_state_db=crawler_state_db)
    return list(
        catalog.iter_resources(
            limit=limit,
            resource_type=resource_type,
            source_filter=source_filter,
        )
    )
