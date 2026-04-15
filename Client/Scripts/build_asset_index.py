"""将 index.jsonl 转为 pipeline.db 中的 asset_index 索引表。

Usage:
    python Client/Scripts/build_asset_index.py \
        --db-path pipeline.db \
        --index-jsonl K:/ResourceCrawler/output/metadata/index.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time


def build(db_path: str, index_jsonl: str) -> None:
    if not os.path.isfile(index_jsonl):
        print(f"错误：index.jsonl 不存在: {index_jsonl}", file=sys.stderr)
        raise SystemExit(1)

    conn = sqlite3.connect(db_path)
    try:
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

        print(f"读取 {index_jsonl} ...")

        with open(index_jsonl, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                md = entry.get("metadata", {}) or {}
                batch.append(
                    (
                        str(entry.get("id", "")),
                        str(entry.get("file_path", "")),
                        str(entry.get("source", "")),
                        str(entry.get("source_pack", "")),
                        str(md.get("format", "")).lower(),
                        str(md.get("style", "")),
                        str(md.get("theme", "")),
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


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 asset_index SQLite 索引表")
    parser.add_argument("--db-path", required=True, help="SQLite 数据库路径")
    parser.add_argument("--index-jsonl", required=True, help="crawler index.jsonl 路径")
    args = parser.parse_args()
    build(os.path.abspath(args.db_path), os.path.abspath(args.index_jsonl))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
