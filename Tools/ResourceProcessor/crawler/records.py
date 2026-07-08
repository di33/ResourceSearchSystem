"""Crawler resource record shapes used by pure adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
