"""Rebuild pipeline.db from ResourceCrawler crawler_state.db.

This replaces the old JSONL migration path. The rebuilt cache contains fresh
resource_task/resource_file rows plus asset_index. Preview, description, and
upload tables are intentionally empty so the split pipeline can regenerate them.

Usage:
    python -m ResourceProcessor.tools.rebuild_pipeline_from_crawler_state \
        --crawler-state-db G:/ResourceCrawler/data/crawler_state.db \
        --crawler-output K:/ResourceCrawler/output \
        --db-path G:/ResourceUpload/data/databases/pipeline.db \
        --replace
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

_CLIENT_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_CLIENT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CLIENT_SCRIPTS))

from build_asset_index import build as build_asset_index  # noqa: E402
from ResourceProcessor.cache.local_cache import LocalCacheStore  # noqa: E402
from ResourceProcessor.crawler.catalog_loader import (  # noqa: E402
    DEFAULT_CRAWLER_OUTPUT,
    DEFAULT_CRAWLER_STATE_DB,
    load_crawler_catalog,
)
from ResourceProcessor.crawler.resource_adapter import build_processing_entity  # noqa: E402
from ResourceProcessor.pipeline_common import Report, print_progress  # noqa: E402


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _remove_sqlite_files(db_path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            os.remove(path)


def _prepare_db(db_path: str, *, replace: bool, backup: bool, report: Report) -> None:
    if not os.path.exists(db_path):
        return
    if not replace:
        raise RuntimeError(f"目标数据库已存在，请加 --replace 或换 --db-path: {db_path}")

    if backup:
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{db_path}.bak_{stamp}"
        shutil.copy2(db_path, backup_path)
        report.ok("备份旧数据库", backup_path)

    _remove_sqlite_files(db_path)
    report.ok("清理旧数据库", db_path)


def _insert_entity(conn: sqlite3.Connection, entity) -> int:
    now = _now()
    cur = conn.execute(
        """INSERT INTO resource_task
           (content_md5, resource_type, source_directory,
            source_resource_id, title, pack_id, pack_name, source, resource_path,
            parent_resource_id, child_resource_ids, child_resource_count,
            contains_resource_types, source_url, download_url, category,
            tags, license_name, source_description, member_count,
            missing_files, auxiliary_metadata,
            process_state, resource_id,
            retry_count, last_error_code, last_error_message,
            created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entity.content_md5,
            entity.resource_type,
            entity.source_directory,
            entity.source_resource_id,
            entity.title,
            entity.pack_id,
            entity.pack_name,
            entity.source,
            entity.resource_path,
            entity.parent_resource_id or "",
            json.dumps(entity.child_resource_ids, ensure_ascii=False),
            entity.child_resource_count,
            json.dumps(entity.contains_resource_types, ensure_ascii=False),
            entity.source_url,
            entity.download_url,
            entity.category,
            json.dumps(entity.tags, ensure_ascii=False),
            entity.license_name,
            entity.source_description,
            entity.member_count,
            json.dumps(entity.missing_files, ensure_ascii=False),
            json.dumps(entity.auxiliary_metadata or {}, ensure_ascii=False),
            entity.process_state.value,
            entity.resource_id,
            entity.retry_count,
            entity.last_error_code,
            entity.last_error_message,
            now,
            now,
        ),
    )
    task_id = int(cur.lastrowid)

    if entity.files:
        conn.executemany(
            """INSERT INTO resource_file
               (task_id, file_path, file_name, file_size, file_format,
                content_md5, file_role, ks3_key, is_primary, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    task_id,
                    f.file_path,
                    f.file_name,
                    f.file_size,
                    f.file_format,
                    f.content_md5,
                    f.file_role,
                    None,
                    1 if f.is_primary else 0,
                    now,
                )
                for f in entity.files
            ],
        )
    return task_id


def rebuild(args: argparse.Namespace) -> int:
    db_path = os.path.abspath(args.db_path)
    crawler_state_db = os.path.abspath(args.crawler_state_db)
    crawler_output = os.path.abspath(args.crawler_output)

    report = Report(label="重建 pipeline 缓存")
    print("=" * 60)
    print("  从 crawler_state.db 重建 pipeline 本地缓存")
    print(f"  Crawler DB:     {crawler_state_db}")
    print(f"  Crawler Output: {crawler_output}")
    print(f"  目标数据库:     {db_path}")
    print("=" * 60)

    if not os.path.isfile(crawler_state_db):
        report.fail("检查 crawler_state.db", f"不存在: {crawler_state_db}")
        report.summary()
        return 1
    if not os.path.isdir(os.path.join(crawler_output, "assets")):
        report.fail("检查 assets 根目录", f"不存在: {os.path.join(crawler_output, 'assets')}")
        report.summary()
        return 1

    try:
        _prepare_db(db_path, replace=args.replace, backup=not args.no_backup, report=report)
    except Exception as exc:
        report.fail("准备目标数据库", str(exc))
        report.summary()
        return 1

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    cache = LocalCacheStore(db_path)
    cache.close()
    report.ok("初始化缓存表", "resource_task/resource_file/resource_preview/resource_description/...")

    try:
        build_asset_index(db_path, crawler_state_db)
        report.ok("重建 asset_index", "来自 crawler_state.db.assets")
    except Exception as exc:
        report.fail("重建 asset_index", str(exc)[:200])
        report.summary()
        return 1

    catalog = load_crawler_catalog(crawler_output, crawler_state_db=crawler_state_db)
    conn = sqlite3.connect(db_path, timeout=300)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=300000")

    processed = 0
    files = 0
    resources_without_files = 0
    missing_files = 0
    failed = 0
    md5_cache: dict[str, str] = {}
    t0 = time.time()

    try:
        for record in catalog.iter_resources(
            limit=args.limit,
            resource_type=args.resource_type,
            source_filter=args.source_filter,
        ):
            try:
                entity = build_processing_entity(record, file_md5_cache=md5_cache)
                _insert_entity(conn, entity)
                processed += 1
                files += len(entity.files)
                missing_files += len(record.missing_files)
                if not entity.files:
                    resources_without_files += 1
            except Exception as exc:
                failed += 1
                if failed <= 20:
                    report.fail(f"资源 [{record.title or record.id}]", str(exc)[:160])

            if processed and processed % args.commit_every == 0:
                conn.commit()
                elapsed = time.time() - t0
                print_progress(
                    processed,
                    processed,
                    f"文件 {files:,}, 无文件资源 {resources_without_files:,}, 缺失文件 {missing_files:,}, {elapsed:.1f}s",
                )

        conn.commit()
    finally:
        conn.close()

    report.ok(
        "重建 resource_task/resource_file",
        f"资源 {processed:,}, 文件 {files:,}, 无文件资源 {resources_without_files:,}, "
        f"缺失文件 {missing_files:,}, 失败 {failed:,}",
    )
    report.ok("后续流程", "generate_previews -> generate_descriptions -> upload_resources")
    ok = report.summary()
    return 0 if ok and failed == 0 else 1


def main() -> int:
    default_db = Path(__file__).resolve().parents[4] / "data" / "databases" / "pipeline.db"
    parser = argparse.ArgumentParser(description="从 crawler_state.db 全量重建 pipeline 本地缓存库")
    parser.add_argument("--crawler-state-db", default=DEFAULT_CRAWLER_STATE_DB, help="crawler_state.db 路径")
    parser.add_argument("--crawler-output", default=DEFAULT_CRAWLER_OUTPUT, help="ResourceCrawler output 根目录")
    parser.add_argument("--db-path", default=str(default_db), help="目标 pipeline SQLite 路径")
    parser.add_argument("--replace", action="store_true", help="允许覆盖目标数据库")
    parser.add_argument("--no-backup", action="store_true", help="覆盖前不备份旧数据库")
    parser.add_argument("--limit", type=int, default=None, help="最多重建多少个资源")
    parser.add_argument("--resource-type", default="", help="只重建指定资源类型")
    parser.add_argument("--source-filter", default="", help="只重建指定来源站点")
    parser.add_argument("--commit-every", type=int, default=1000, help="每 N 条资源提交一次")
    return rebuild(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
