"""从 crawler_state.db.assets 构建 pipeline.db 中的 asset_index 索引表。

Usage:
    python client/Scripts/build_asset_index.py \
        --db-path data/databases/pipeline.db \
        --crawler-state-db G:/ResourceCrawler/data/crawler_state.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path


DEFAULT_CRAWLER_STATE_DB = r"G:\ResourceCrawler\data\crawler_state.db"


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _text_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value if v is not None)
    return str(value)


def _metadata_value(metadata: dict, index: dict, key: str) -> str:
    value = metadata.get(key)
    if value in (None, ""):
        value = index.get(key)
    return _text_value(value)


def build(db_path: str, crawler_state_db: str) -> None:
    if not os.path.isfile(crawler_state_db):
        print(f"错误：crawler_state.db 不存在: {crawler_state_db}", file=sys.stderr)
        raise SystemExit(1)

    source = sqlite3.connect(crawler_state_db)
    source.row_factory = sqlite3.Row
    conn = sqlite3.connect(db_path)
    try:
        row = source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='assets'"
        ).fetchone()
        if row is None:
            print(f"错误：{crawler_state_db} 中没有 assets 表", file=sys.stderr)
            raise SystemExit(1)

        conn.execute("DROP TABLE IF EXISTS asset_index")
        conn.execute(
            """CREATE TABLE asset_index (
                asset_id   TEXT NOT NULL,
                file_path  TEXT NOT NULL DEFAULT '',
                source     TEXT NOT NULL DEFAULT '',
                pack_name  TEXT NOT NULL DEFAULT '',
                fmt        TEXT NOT NULL DEFAULT '',
                style      TEXT NOT NULL DEFAULT '',
                theme      TEXT NOT NULL DEFAULT ''
            )"""
        )

        t0 = time.time()
        batch: list[tuple] = []
        total = 0
        BATCH_SIZE = 10000

        print(f"读取 {crawler_state_db} assets ...")

        rows = source.execute(
            "SELECT id, file_path, source, source_pack, metadata_json, index_json FROM assets"
        )
        for row in rows:
            metadata = _json_object(row["metadata_json"])
            index = _json_object(row["index_json"])
            file_path = _text_value(row["file_path"])
            fmt = _metadata_value(metadata, index, "format").lower()
            if not fmt:
                fmt = Path(file_path).suffix.lstrip(".").lower()
            batch.append(
                (
                    _text_value(row["id"]),
                    file_path,
                    _text_value(row["source"] or index.get("source")),
                    _text_value(row["source_pack"] or index.get("source_pack") or index.get("pack_name")),
                    fmt,
                    _metadata_value(metadata, index, "style"),
                    _metadata_value(metadata, index, "theme"),
                )
            )
            total += 1
            if len(batch) >= BATCH_SIZE:
                conn.executemany(
                    "INSERT INTO asset_index VALUES (?,?,?,?,?,?,?)", batch
                )
                batch.clear()
                elapsed = time.time() - t0
                print(f"\r  已处理 {total:,} 行 ({elapsed:.1f}s)", end="", flush=True)

        if batch:
            conn.executemany(
                "INSERT INTO asset_index VALUES (?,?,?,?,?,?,?)", batch
            )

        elapsed = time.time() - t0
        print(f"\r  已处理 {total:,} 行 ({elapsed:.1f}s)")

        print("创建索引 ...")
        conn.execute("CREATE INDEX idx_asset_id ON asset_index(asset_id)")
        conn.execute(
            "CREATE INDEX idx_asset_source_pack ON asset_index(source, pack_name, file_path)"
        )
        conn.commit()

        count = conn.execute("SELECT COUNT(*) FROM asset_index").fetchone()[0]
        print(f"完成：asset_index 表共 {count:,} 行")
    finally:
        conn.close()
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 asset_index SQLite 索引表")
    parser.add_argument("--db-path", required=True, help="SQLite 数据库路径")
    parser.add_argument("--crawler-state-db", default=DEFAULT_CRAWLER_STATE_DB, help="crawler_state.db 路径")
    args = parser.parse_args()
    build(os.path.abspath(args.db_path), os.path.abspath(args.crawler_state_db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
