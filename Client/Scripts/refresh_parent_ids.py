"""刷新 pipeline.db 中 resource_task 的 parent_resource_id 字段。

从 crawler_state.db.resource_index 读取正确的 parent_resource_id，更新本地 DB 中缺失的记录。

Usage:
    python client/Scripts/refresh_parent_ids.py \
        --db-path data/databases/pipeline.db \
        --crawler-state-db G:\\ResourceCrawler\\data\\crawler_state.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys


DEFAULT_CRAWLER_STATE_DB = r"G:\ResourceCrawler\data\crawler_state.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新 parent_resource_id")
    parser.add_argument("--db-path", required=True, help="SQLite 数据库路径")
    parser.add_argument("--crawler-state-db", default=DEFAULT_CRAWLER_STATE_DB, help="crawler_state.db 路径")
    args = parser.parse_args()

    db_path = os.path.abspath(args.db_path)
    crawler_state_db = os.path.abspath(args.crawler_state_db)

    if not os.path.isfile(db_path):
        print(f"错误：数据库不存在: {db_path}", file=sys.stderr)
        return 1
    if not os.path.isfile(crawler_state_db):
        print(f"错误：crawler_state.db 不存在: {crawler_state_db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    source = sqlite3.connect(f"file:{crawler_state_db}?mode=ro", uri=True, timeout=300)
    source.row_factory = sqlite3.Row

    # 确保列存在
    cols = {row[1] for row in conn.execute("PRAGMA table_info(resource_task)").fetchall()}
    if "parent_resource_id" not in cols:
        conn.execute("ALTER TABLE resource_task ADD COLUMN parent_resource_id TEXT NOT NULL DEFAULT ''")
        conn.commit()
        print("已添加 parent_resource_id 列")

    updated = 0
    checked = 0
    batch: list[tuple[str, str]] = []
    BATCH_SIZE = 5000

    print(f"读取 {crawler_state_db} resource_index ...")

    rows = source.execute(
        "SELECT id, parent_resource_id FROM resource_index "
        "WHERE parent_resource_id IS NOT NULL AND parent_resource_id <> ''"
    )
    for row in rows:
        rid = row["id"]
        parent = row["parent_resource_id"]
        if not rid or not parent:
            continue

        checked += 1
        batch.append((parent, rid))

        if len(batch) >= BATCH_SIZE:
            result = conn.executemany(
                "UPDATE resource_task SET parent_resource_id = ? WHERE source_resource_id = ? AND (parent_resource_id = '' OR parent_resource_id IS NULL)",
                batch,
            )
            updated += result.rowcount
            batch.clear()
            print(f"\r  已处理 {checked:,} 条，更新 {updated:,} 条", end="", flush=True)

    if batch:
        result = conn.executemany(
            "UPDATE resource_task SET parent_resource_id = ? WHERE source_resource_id = ? AND (parent_resource_id = '' OR parent_resource_id IS NULL)",
            batch,
        )
        updated += result.rowcount

    conn.commit()
    conn.close()
    source.close()

    print(f"\r  已处理 {checked:,} 条，更新 {updated:,} 条")
    print(f"完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
