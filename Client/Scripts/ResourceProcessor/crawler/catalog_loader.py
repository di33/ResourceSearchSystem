from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


@dataclass
class CrawlerAssetRecord:
    asset_id: str
    file_path: str
    source: str
    pack_name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def format(self) -> str:
        metadata_format = self.metadata.get("format")
        if metadata_format:
            return str(metadata_format).lower()
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
    def __init__(self, output_root: str):
        self.output_root = Path(output_root).resolve()
        self.assets_root = self.output_root / "assets"
        self.metadata_root = self.output_root / "metadata"
        self.resource_index_path = self.metadata_root / "resource_index.jsonl"
        self.asset_index_path = self.metadata_root / "index.jsonl"
        self._asset_index_by_id = self._load_asset_index()
        self._pack_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def _require_file(self, path: Path, label: str) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"缺少 {label}: {path}")

    def _load_asset_index(self) -> dict[str, CrawlerAssetRecord]:
        assets: dict[str, CrawlerAssetRecord] = {}
        self._require_file(self.asset_index_path, "crawler 资产索引 index.jsonl")
        for entry in _iter_jsonl(self.asset_index_path):
            asset = CrawlerAssetRecord(
                asset_id=str(entry.get("id", "")),
                file_path=str(entry.get("file_path", "")),
                source=str(entry.get("source", "")),
                pack_name=str(entry.get("source_pack", "")),
                metadata=entry.get("metadata", {}) or {},
                raw=entry,
            )
            if asset.asset_id:
                assets[asset.asset_id] = asset
        return assets

    def get_pack_metadata(self, source: str, pack_name: str) -> dict[str, Any]:
        key = (source, pack_name)
        if key in self._pack_cache:
            return self._pack_cache[key]
        pack_path = self.metadata_root / source / f"{pack_name}.json"
        if pack_path.is_file():
            self._pack_cache[key] = _read_json(pack_path)
        else:
            self._pack_cache[key] = {}
        return self._pack_cache[key]

    def resolve_asset_file(self, source: str, pack_name: str, file_path: str) -> str:
        return str((self.assets_root / source / pack_name / Path(file_path)).resolve())

    def _resolve_assets(self, resource_entry: dict[str, Any]) -> list[CrawlerAssetRecord]:
        file_paths = resource_entry.get("file_paths", []) or []
        asset_ids = resource_entry.get("asset_ids", []) or []
        assets: list[CrawlerAssetRecord] = []
        seen_ids: set[str] = set()

        for asset_id in asset_ids:
            asset = self._asset_index_by_id.get(str(asset_id))
            if asset is not None:
                assets.append(asset)
                seen_ids.add(asset.asset_id)

        if assets:
            return assets

        source = str(resource_entry.get("source", ""))
        pack_name = str(resource_entry.get("pack_name", ""))
        for file_path in file_paths:
            for asset in self._asset_index_by_id.values():
                if asset.asset_id in seen_ids:
                    continue
                if asset.source == source and asset.pack_name == pack_name and asset.file_path == file_path:
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
        for entry in _iter_jsonl(self.resource_index_path):
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


def load_crawler_catalog(output_root: str) -> CrawlerCatalog:
    return CrawlerCatalog(output_root)


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
