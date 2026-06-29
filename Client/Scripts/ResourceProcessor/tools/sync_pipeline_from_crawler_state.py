"""Incrementally sync pipeline.db from ResourceCrawler crawler_state.db.

Default mode preserves unchanged resource_task rows and their generated
previews/descriptions. Changed resources are invalidated just enough for the
split pipeline to regenerate the affected downstream artifacts.

Full rebuild mode is the same sync after clearing the current cache first:

    python -m ResourceProcessor.tools.sync_pipeline_from_crawler_state \
        --crawler-state-db G:/ResourceCrawler/data/crawler_state.db \
        --crawler-output K:/ResourceCrawler/output \
        --db-path G:/ResourceUpload/data/databases/pipeline.db \
        --clear-first
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

_CLIENT_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_CLIENT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CLIENT_SCRIPTS))

from ResourceProcessor.cache.local_cache import LocalCacheStore  # noqa: E402
from ResourceProcessor.crawler.catalog_loader import (  # noqa: E402
    DEFAULT_CRAWLER_OUTPUT,
    DEFAULT_CRAWLER_STATE_DB,
    CrawlerCatalog,
    CrawlerResourceRecord,
    _json_object,
    _text_value,
    load_crawler_catalog,
)
from ResourceProcessor.crawler.resource_adapter import (  # noqa: E402
    build_processing_entity,
    compute_resource_fingerprint,
)
from ResourceProcessor.pipeline_common import Report, print_progress  # noqa: E402
from ResourceProcessor.preview_metadata import ProcessState  # noqa: E402


RESOURCE_COLUMNS = (
    "id, pack_id, source, pack_name, resource_type, title, resource_path, "
    "group_name, parent_resource_id, member_count, record_json"
)

TASK_COLUMNS = (
    "content_md5, resource_type, source_directory, source_resource_id, title, "
    "pack_id, pack_name, source, resource_path, parent_resource_id, "
    "child_resource_ids, child_resource_count, contains_resource_types, "
    "source_url, download_url, category, tags, license_name, source_description, "
    "member_count, missing_files, auxiliary_metadata, process_state, resource_id, "
    "retry_count, last_error_code, last_error_message, created_at, updated_at"
)

ASSET_INDEX_DDL = """CREATE TABLE IF NOT EXISTS asset_index (
    asset_id   TEXT NOT NULL,
    file_path  TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    pack_name  TEXT NOT NULL DEFAULT '',
    fmt        TEXT NOT NULL DEFAULT '',
    style      TEXT NOT NULL DEFAULT '',
    theme      TEXT NOT NULL DEFAULT ''
)"""

RESOURCE_CHILD_TABLES = (
    "description_lease",
    "resource_description",
    "resource_embedding",
    "resource_upload_job",
    "process_log",
)


@dataclass
class SyncStats:
    assets_added: int = 0
    assets_updated: int = 0
    assets_deleted: int = 0
    resources_added: int = 0
    resources_preview_changed: int = 0
    resources_description_changed: int = 0
    resources_deleted: int = 0
    resources_unchanged: int = 0
    packs_invalidated: int = 0
    files_inserted: int = 0
    preview_rows_deleted: int = 0
    preview_files_planned: int = 0
    preview_files_deleted: int = 0
    preview_files_skipped: int = 0
    failures: int = 0
    failure_examples: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.failures += 1
        if len(self.failure_examples) < 20:
            self.failure_examples.append(message)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_loads(value: Any, fallback):
    if value in (None, ""):
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed


def _json_list(value: Any) -> list[str]:
    parsed = _json_loads(value, [])
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _json_dict(value: Any) -> dict[str, Any]:
    parsed = _json_loads(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _norm_text(value: Any) -> str:
    return _text_value(value).strip()


def _norm_list(values: Iterable[Any]) -> list[str]:
    return [str(value) for value in values]


def _same_list(left: Iterable[Any], right: Iterable[Any]) -> bool:
    return _norm_list(left) == _norm_list(right)


def _merge_tags(record: CrawlerResourceRecord) -> list[str]:
    tags: list[str] = []
    for value in record.tags + record.pack_tags:
        value = str(value).strip()
        if value and value not in tags:
            tags.append(value)
    return tags


def _source_description(record: CrawlerResourceRecord) -> str:
    return record.description or record.pack_description


def _metadata_value(metadata: dict[str, Any], index: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if value in (None, ""):
        value = index.get(key)
    return _text_value(value)


def _asset_index_row(row: sqlite3.Row) -> tuple[str, str, str, str, str, str, str]:
    metadata = _json_object(row["metadata_json"])
    index = _json_object(row["index_json"])
    file_path = _text_value(row["file_path"])
    fmt = _metadata_value(metadata, index, "format").lower()
    if not fmt:
        fmt = Path(file_path).suffix.lstrip(".").lower()
    return (
        _text_value(row["id"]),
        file_path,
        _text_value(row["source"] or index.get("source")),
        _text_value(row["source_pack"] or index.get("source_pack") or index.get("pack_name")),
        fmt,
        _metadata_value(metadata, index, "style"),
        _metadata_value(metadata, index, "theme"),
    )


def _entity_task_values(entity, now: str) -> tuple[Any, ...]:
    return (
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
        _json_dumps(entity.child_resource_ids),
        entity.child_resource_count,
        _json_dumps(entity.contains_resource_types),
        entity.source_url,
        entity.download_url,
        entity.category,
        _json_dumps(entity.tags),
        entity.license_name,
        entity.source_description,
        entity.member_count,
        _json_dumps(entity.missing_files),
        _json_dumps(entity.auxiliary_metadata or {}),
        entity.process_state.value,
        entity.resource_id,
        entity.retry_count,
        entity.last_error_code,
        entity.last_error_message,
        now,
        now,
    )


def _insert_entity(conn: sqlite3.Connection, entity) -> tuple[int, int]:
    now = _now()
    placeholders = ", ".join("?" for _ in TASK_COLUMNS.split(", "))
    cur = conn.execute(
        f"INSERT INTO resource_task ({TASK_COLUMNS}) VALUES ({placeholders})",
        _entity_task_values(entity, now),
    )
    task_id = int(cur.lastrowid)
    file_count = _insert_resource_files(conn, task_id, entity.files, now)
    return task_id, file_count


def _insert_resource_files(conn: sqlite3.Connection, task_id: int, files, now: str) -> int:
    if not files:
        return 0
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
            for f in files
        ],
    )
    return len(files)


def _update_task_with_entity(
    conn: sqlite3.Connection,
    task_id: int,
    entity,
    *,
    state: ProcessState,
) -> int:
    now = _now()
    entity.process_state = state
    entity.resource_id = None
    entity.retry_count = 0
    entity.last_error_code = ""
    entity.last_error_message = ""
    insert_columns = TASK_COLUMNS.split(", ")
    values_by_column = dict(zip(insert_columns, _entity_task_values(entity, now)))
    update_columns = [column for column in insert_columns if column != "created_at"]
    assignments = ", ".join(f"{column} = ?" for column in update_columns)
    values = [values_by_column[column] for column in update_columns]
    conn.execute(
        f"UPDATE resource_task SET {assignments} WHERE id = ?",
        (*values, task_id),
    )
    conn.execute("DELETE FROM resource_file WHERE task_id = ?", (task_id,))
    return _insert_resource_files(conn, task_id, entity.files, now)


def _update_task_description_metadata(
    conn: sqlite3.Connection,
    task_id: int,
    record: CrawlerResourceRecord,
) -> None:
    now = _now()
    conn.execute(
        """UPDATE resource_task
           SET title = ?, pack_id = ?, pack_name = ?, source = ?,
               source_url = ?, download_url = ?, category = ?, tags = ?,
               license_name = ?, source_description = ?,
               process_state = ?, resource_id = NULL, retry_count = 0,
               last_error_code = '', last_error_message = '', updated_at = ?
           WHERE id = ?""",
        (
            record.title,
            record.pack_id,
            record.pack_name,
            record.source,
            record.source_url,
            record.download_url,
            record.category,
            _json_dumps(_merge_tags(record)),
            record.license_name,
            _source_description(record),
            ProcessState.PREVIEW_READY.value,
            now,
            task_id,
        ),
    )


def _source_record_from_entry(
    catalog: CrawlerCatalog,
    entry: dict[str, Any],
    *,
    include_assets: bool,
) -> CrawlerResourceRecord:
    pack_metadata = catalog.get_pack_metadata(
        str(entry.get("source", "")),
        str(entry.get("pack_name", "")),
    )
    file_paths = [str(value) for value in entry.get("file_paths", []) or []]
    resolved_files: list[str] = []
    missing_files: list[str] = []
    for file_path in file_paths:
        abs_path = catalog.resolve_asset_file(
            str(entry.get("source", "")),
            str(entry.get("pack_name", "")),
            file_path,
        )
        if os.path.isfile(abs_path):
            resolved_files.append(abs_path)
        else:
            missing_files.append(file_path)

    return CrawlerResourceRecord(
        raw=entry,
        pack_metadata=pack_metadata,
        assets=catalog._resolve_assets(entry) if include_assets else [],
        resolved_files=resolved_files,
        missing_files=missing_files,
    )


def _iter_source_entries(
    source_conn: sqlite3.Connection,
    catalog: CrawlerCatalog,
) -> Iterable[dict[str, Any]]:
    rows = source_conn.execute(
        f"SELECT {RESOURCE_COLUMNS} FROM resource_index ORDER BY row_id"
    )
    seen: set[str] = set()
    for row in rows:
        entry = catalog._entry_from_resource_row(row)
        rid = str(entry.get("id", ""))
        if not rid or rid in seen:
            continue
        seen.add(rid)
        yield entry


def _load_source_entry_by_id(
    source_conn: sqlite3.Connection,
    catalog: CrawlerCatalog,
    source_resource_id: str,
) -> dict[str, Any] | None:
    row = source_conn.execute(
        f"SELECT {RESOURCE_COLUMNS} FROM resource_index WHERE id = ? ORDER BY row_id LIMIT 1",
        (source_resource_id,),
    ).fetchone()
    return catalog._entry_from_resource_row(row) if row else None


def _load_target_tasks(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute("SELECT * FROM resource_task WHERE source_resource_id <> ''").fetchall()
    by_source: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        source_id = item["source_resource_id"]
        if source_id not in by_source:
            by_source[source_id] = item
    return by_source


def _preview_change_reasons(task: dict[str, Any], record: CrawlerResourceRecord) -> list[str]:
    reasons: list[str] = []
    current_fingerprint = compute_resource_fingerprint(record)
    if _norm_text(task.get("content_md5")) != current_fingerprint:
        reasons.append("file_paths_or_resource_shape")
    if _norm_text(task.get("resource_type")) != record.resource_type:
        reasons.append("resource_type")
    if _norm_text(task.get("resource_path")) != record.resource_path:
        reasons.append("resource_path")
    if _norm_text(task.get("parent_resource_id")) != (record.parent_resource_id or ""):
        reasons.append("parent_resource_id")
    if not _same_list(_json_list(task.get("child_resource_ids")), record.child_resource_ids):
        reasons.append("child_resource_ids")
    if int(task.get("child_resource_count") or 0) != record.child_resource_count:
        reasons.append("child_resource_count")
    if not _same_list(_json_list(task.get("contains_resource_types")), record.contains_resource_types):
        reasons.append("contains_resource_types")
    if not _same_list(_json_list(task.get("missing_files")), record.missing_files):
        reasons.append("missing_files")
    return reasons


def _description_change_reasons(task: dict[str, Any], record: CrawlerResourceRecord) -> list[str]:
    reasons: list[str] = []
    text_fields = {
        "title": record.title,
        "pack_id": record.pack_id,
        "pack_name": record.pack_name,
        "source": record.source,
        "source_url": record.source_url,
        "download_url": record.download_url,
        "category": record.category,
        "license_name": record.license_name,
        "source_description": _source_description(record),
    }
    for field_name, current_value in text_fields.items():
        if _norm_text(task.get(field_name)) != _norm_text(current_value):
            reasons.append(field_name)
    if not _same_list(_json_list(task.get("tags")), _merge_tags(record)):
        reasons.append("tags")
    return reasons


def _ensure_asset_index(conn: sqlite3.Connection) -> None:
    conn.execute(ASSET_INDEX_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_asset_id ON asset_index(asset_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_asset_source_pack "
        "ON asset_index(source, pack_name, file_path)"
    )


def _sync_asset_index(
    conn: sqlite3.Connection,
    source_conn: sqlite3.Connection,
    stats: SyncStats,
    *,
    dry_run: bool,
    batch_size: int,
) -> None:
    _ensure_asset_index(conn)
    target = {
        row["asset_id"]: (
            row["file_path"],
            row["source"],
            row["pack_name"],
            row["fmt"],
            row["style"],
            row["theme"],
        )
        for row in conn.execute("SELECT * FROM asset_index").fetchall()
    }

    inserts: list[tuple[str, str, str, str, str, str, str]] = []
    updates: list[tuple[str, str, str, str, str, str, str]] = []

    def flush() -> None:
        nonlocal inserts, updates
        if dry_run:
            inserts.clear()
            updates.clear()
            return
        if inserts:
            conn.executemany(
                "INSERT INTO asset_index "
                "(asset_id, file_path, source, pack_name, fmt, style, theme) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                inserts,
            )
            inserts.clear()
        if updates:
            conn.executemany(
                """UPDATE asset_index
                   SET file_path = ?, source = ?, pack_name = ?, fmt = ?, style = ?, theme = ?
                   WHERE asset_id = ?""",
                [
                    (file_path, source, pack_name, fmt, style, theme, asset_id)
                    for asset_id, file_path, source, pack_name, fmt, style, theme in updates
                ],
            )
            updates.clear()

    for row in source_conn.execute(
        "SELECT id, file_path, source, source_pack, metadata_json, index_json FROM assets"
    ):
        asset_id, file_path, source, pack_name, fmt, style, theme = _asset_index_row(row)
        current = (file_path, source, pack_name, fmt, style, theme)
        previous = target.pop(asset_id, None)
        if previous is None:
            stats.assets_added += 1
            inserts.append((asset_id, file_path, source, pack_name, fmt, style, theme))
        elif previous != current:
            stats.assets_updated += 1
            updates.append((asset_id, file_path, source, pack_name, fmt, style, theme))
        if len(inserts) + len(updates) >= batch_size:
            flush()

    flush()
    stats.assets_deleted = len(target)
    if target and not dry_run:
        for chunk in _chunks(list(target), batch_size):
            conn.executemany("DELETE FROM asset_index WHERE asset_id = ?", [(asset_id,) for asset_id in chunk])


def _chunks(values: list[Any], size: int = 800) -> Iterable[list[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _select_preview_paths(conn: sqlite3.Connection, task_ids: list[int]) -> list[str]:
    paths: list[str] = []
    if not task_ids:
        return paths
    if not _table_exists(conn, "resource_preview"):
        return paths
    for chunk in _chunks(task_ids):
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""SELECT path FROM resource_preview
                WHERE task_id IN ({placeholders})
                  AND path IS NOT NULL AND path <> ''""",
            chunk,
        ).fetchall()
        paths.extend(str(row["path"]) for row in rows if row["path"])
    return paths


def _delete_by_task_id(
    conn: sqlite3.Connection,
    table_name: str,
    task_ids: list[int],
    *,
    dry_run: bool,
) -> int:
    if not task_ids:
        return 0
    if not _table_exists(conn, table_name):
        return 0
    count = 0
    for chunk in _chunks(task_ids):
        placeholders = ",".join("?" for _ in chunk)
        count += conn.execute(
            f"SELECT COUNT(*) FROM {table_name} WHERE task_id IN ({placeholders})",
            chunk,
        ).fetchone()[0]
        if not dry_run:
            conn.execute(f"DELETE FROM {table_name} WHERE task_id IN ({placeholders})", chunk)
    return count


def _clear_downstream(
    conn: sqlite3.Connection,
    task_ids: list[int],
    *,
    include_previews: bool,
    include_files: bool,
    dry_run: bool,
) -> tuple[int, list[str]]:
    preview_paths = _select_preview_paths(conn, task_ids) if include_previews else []
    for table_name in RESOURCE_CHILD_TABLES:
        _delete_by_task_id(conn, table_name, task_ids, dry_run=dry_run)
    preview_rows = 0
    if include_previews:
        preview_rows = _delete_by_task_id(conn, "resource_preview", task_ids, dry_run=dry_run)
    if include_files:
        _delete_by_task_id(conn, "resource_file", task_ids, dry_run=dry_run)
    return preview_rows, preview_paths


def _delete_resource_tasks(
    conn: sqlite3.Connection,
    task_ids: list[int],
    *,
    dry_run: bool,
) -> None:
    if not task_ids:
        return
    for chunk in _chunks(task_ids):
        placeholders = ",".join("?" for _ in chunk)
        if not dry_run:
            conn.execute(f"DELETE FROM resource_task WHERE id IN ({placeholders})", chunk)


def _default_preview_roots(db_path: str) -> list[Path]:
    project_root = Path(__file__).resolve().parents[4]
    db_stem = Path(db_path).stem
    suffix = db_stem.removeprefix("pipeline_")
    workdirs = project_root / "data" / "workdirs"
    roots = [
        workdirs / "test_workdir_crawler" / "previews",
        workdirs / "test_workdir_rebuilt" / "previews",
        workdirs / f"test_workdir_{suffix}" / "previews",
    ]
    return roots


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_delete_preview_files(
    paths: Iterable[str],
    *,
    preview_roots: list[Path],
    dry_run: bool,
    stats: SyncStats,
) -> None:
    seen: set[Path] = set()
    resolved_roots = [root.resolve() for root in preview_roots]
    for raw_path in paths:
        if not raw_path:
            continue
        path = Path(raw_path).resolve()
        if path in seen:
            continue
        seen.add(path)
        stats.preview_files_planned += 1
        if resolved_roots:
            allowed = any(_is_relative_to(path, root) for root in resolved_roots)
        else:
            allowed = "previews" in {part.lower() for part in path.parts}
        if not allowed or not path.is_file():
            stats.preview_files_skipped += 1
            continue
        if not dry_run:
            try:
                path.unlink()
                stats.preview_files_deleted += 1
            except OSError as exc:
                stats.preview_files_skipped += 1
                stats.fail(f"删除预览文件失败: {path} ({exc})")


def _backup_db(db_path: str, report: Report) -> str | None:
    if not os.path.exists(db_path):
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak_sync_{stamp}"
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=300)
    dest = sqlite3.connect(backup_path, timeout=300)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    report.ok("备份目标数据库", backup_path)
    return backup_path


def _remove_sqlite_files(db_path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            os.remove(path)


def _clear_current_cache(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
) -> tuple[int, list[str]]:
    task_ids = [int(row["id"]) for row in conn.execute("SELECT id FROM resource_task").fetchall()]
    preview_rows, preview_paths = _clear_downstream(
        conn,
        task_ids,
        include_previews=True,
        include_files=True,
        dry_run=dry_run,
    )
    _delete_resource_tasks(conn, task_ids, dry_run=dry_run)
    if not dry_run:
        conn.execute("DELETE FROM asset_index")
    return preview_rows, preview_paths


def _process_preview_changed_resource(
    conn: sqlite3.Connection,
    task: dict[str, Any],
    record: CrawlerResourceRecord,
    stats: SyncStats,
    md5_cache: dict[str, str],
    *,
    dry_run: bool,
) -> list[str]:
    task_id = int(task["id"])
    preview_rows, preview_paths = _clear_downstream(
        conn,
        [task_id],
        include_previews=True,
        include_files=True,
        dry_run=dry_run,
    )
    stats.preview_rows_deleted += preview_rows
    if not dry_run:
        record.assets = record.assets or []
        entity = build_processing_entity(record, file_md5_cache=md5_cache)
        stats.files_inserted += _update_task_with_entity(
            conn,
            task_id,
            entity,
            state=ProcessState.DISCOVERED,
        )
    return preview_paths


def _process_description_changed_resource(
    conn: sqlite3.Connection,
    task: dict[str, Any],
    record: CrawlerResourceRecord,
    *,
    dry_run: bool,
) -> None:
    task_id = int(task["id"])
    _clear_downstream(
        conn,
        [task_id],
        include_previews=False,
        include_files=False,
        dry_run=dry_run,
    )
    if not dry_run:
        _update_task_description_metadata(conn, task_id, record)


def _sync_resources(
    conn: sqlite3.Connection,
    source_conn: sqlite3.Connection,
    catalog: CrawlerCatalog,
    stats: SyncStats,
    *,
    dry_run: bool,
    commit_every: int,
) -> list[str]:
    target_by_source = _load_target_tasks(conn)
    all_targets = dict(target_by_source)
    preview_paths_to_delete: list[str] = []
    md5_cache: dict[str, str] = {}
    processed_source_ids: set[str] = set()
    new_source_ids: set[str] = set()
    preview_changed_source_ids: set[str] = set()
    description_changed_source_ids: set[str] = set()
    impacted_parent_ids: set[str] = set()
    processed = 0
    t0 = time.time()

    for entry in _iter_source_entries(source_conn, catalog):
        source_resource_id = str(entry.get("id", ""))
        processed_source_ids.add(source_resource_id)
        task = target_by_source.pop(source_resource_id, None)
        record = _source_record_from_entry(catalog, entry, include_assets=False)

        if task is None:
            stats.resources_added += 1
            new_source_ids.add(source_resource_id)
            if record.parent_resource_id:
                impacted_parent_ids.add(record.parent_resource_id)
            if not dry_run:
                record.assets = catalog._resolve_assets(entry)
                try:
                    entity = build_processing_entity(record, file_md5_cache=md5_cache)
                    _, file_count = _insert_entity(conn, entity)
                    stats.files_inserted += file_count
                except Exception as exc:
                    stats.fail(f"新增资源失败 {source_resource_id}: {exc}")
            processed += 1
        else:
            preview_reasons = _preview_change_reasons(task, record)
            if preview_reasons:
                stats.resources_preview_changed += 1
                preview_changed_source_ids.add(source_resource_id)
                if record.parent_resource_id:
                    impacted_parent_ids.add(record.parent_resource_id)
                if not dry_run:
                    record.assets = catalog._resolve_assets(entry)
                try:
                    preview_paths_to_delete.extend(
                        _process_preview_changed_resource(
                            conn,
                            task,
                            record,
                            stats,
                            md5_cache,
                            dry_run=dry_run,
                        )
                    )
                except Exception as exc:
                    stats.fail(f"更新资源失败 {source_resource_id}: {exc}")
                processed += 1
            else:
                description_reasons = _description_change_reasons(task, record)
                if description_reasons:
                    stats.resources_description_changed += 1
                    description_changed_source_ids.add(source_resource_id)
                    try:
                        _process_description_changed_resource(conn, task, record, dry_run=dry_run)
                    except Exception as exc:
                        stats.fail(f"更新描述元数据失败 {source_resource_id}: {exc}")
                else:
                    stats.resources_unchanged += 1

        if not dry_run and commit_every and processed and processed % commit_every == 0:
            conn.commit()
            elapsed = time.time() - t0
            print_progress(
                processed,
                processed + stats.resources_unchanged,
                f"新增 {stats.resources_added:,}, 变更 {stats.resources_preview_changed + stats.resources_description_changed:,}, {elapsed:.1f}s",
            )

    removed_tasks = list(target_by_source.values())
    for task in removed_tasks:
        parent_id = _norm_text(task.get("parent_resource_id"))
        if parent_id:
            impacted_parent_ids.add(parent_id)
    if removed_tasks:
        removed_task_ids = [int(task["id"]) for task in removed_tasks]
        preview_rows, preview_paths = _clear_downstream(
            conn,
            removed_task_ids,
            include_previews=True,
            include_files=True,
            dry_run=dry_run,
        )
        stats.preview_rows_deleted += preview_rows
        preview_paths_to_delete.extend(preview_paths)
        stats.resources_deleted += len(removed_task_ids)
        _delete_resource_tasks(conn, removed_task_ids, dry_run=dry_run)

    invalidated_pack_ids: set[str] = set()
    already_invalidated = new_source_ids | preview_changed_source_ids | set(target_by_source)
    for parent_id in sorted(impacted_parent_ids):
        if (
            not parent_id
            or parent_id in already_invalidated
            or parent_id in invalidated_pack_ids
        ):
            continue
        task = all_targets.get(parent_id)
        if task is None or parent_id not in processed_source_ids:
            continue
        entry = _load_source_entry_by_id(source_conn, catalog, parent_id)
        if entry is None:
            continue
        record = _source_record_from_entry(catalog, entry, include_assets=not dry_run)
        try:
            preview_paths_to_delete.extend(
                _process_preview_changed_resource(
                    conn,
                    task,
                    record,
                    stats,
                    md5_cache,
                    dry_run=dry_run,
                )
            )
            invalidated_pack_ids.add(parent_id)
            stats.packs_invalidated += 1
        except Exception as exc:
            stats.fail(f"失效父级 pack 失败 {parent_id}: {exc}")

    return preview_paths_to_delete


def _open_source(crawler_state_db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{crawler_state_db}?mode=ro", uri=True, timeout=300)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=300000")
    return conn


def sync(args: argparse.Namespace) -> int:
    db_path = os.path.abspath(args.db_path)
    crawler_state_db = os.path.abspath(args.crawler_state_db)
    crawler_output = os.path.abspath(args.crawler_output)

    report = Report(label="增量同步 pipeline 缓存")
    print("=" * 60)
    print("  从 crawler_state.db 增量同步 pipeline 本地缓存")
    print(f"  Crawler DB:     {crawler_state_db}")
    print(f"  Crawler Output: {crawler_output}")
    print(f"  目标数据库:     {db_path}")
    print(f"  模式:           {'dry-run' if args.dry_run else 'apply'}")
    if args.clear_first:
        print("  初始化:         clear-first")
    print("=" * 60)

    if not os.path.isfile(crawler_state_db):
        report.fail("检查 crawler_state.db", f"不存在: {crawler_state_db}")
        report.summary()
        return 1
    if not os.path.isdir(os.path.join(crawler_output, "assets")):
        report.fail("检查 assets 根目录", f"不存在: {os.path.join(crawler_output, 'assets')}")
        report.summary()
        return 1

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    backup_done = False
    if args.clear_first and not args.dry_run and os.path.exists(db_path) and args.replace_db_file:
        if not args.no_backup:
            _backup_db(db_path, report)
            backup_done = True
        _remove_sqlite_files(db_path)

    cache = LocalCacheStore(db_path)
    cache.close()

    preview_roots = [Path(path) for path in args.preview_dir]
    if not preview_roots:
        preview_roots = _default_preview_roots(db_path)

    if not args.dry_run and not args.no_backup and not backup_done:
        _backup_db(db_path, report)

    catalog = load_crawler_catalog(crawler_output, crawler_state_db=crawler_state_db)
    source_conn = _open_source(crawler_state_db)
    conn = sqlite3.connect(db_path, timeout=300)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=300000")

    stats = SyncStats()
    preview_paths_to_delete: list[str] = []
    t0 = time.time()
    try:
        if args.dry_run:
            conn.execute("BEGIN")
        else:
            conn.execute("BEGIN IMMEDIATE")

        _ensure_asset_index(conn)

        if args.clear_first:
            preview_rows, preview_paths = _clear_current_cache(conn, dry_run=args.dry_run)
            stats.preview_rows_deleted += preview_rows
            preview_paths_to_delete.extend(preview_paths)
            report.ok("清空当前缓存", "resource_task/resource_file/resource_preview/resource_description/asset_index")

        _sync_asset_index(
            conn,
            source_conn,
            stats,
            dry_run=args.dry_run,
            batch_size=args.asset_batch_size,
        )
        report.ok(
            "同步 asset_index",
            f"新增 {stats.assets_added:,}, 更新 {stats.assets_updated:,}, 删除 {stats.assets_deleted:,}",
        )

        preview_paths_to_delete.extend(
            _sync_resources(
                conn,
                source_conn,
                catalog,
                stats,
                dry_run=args.dry_run,
                commit_every=args.commit_every,
            )
        )

        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception as exc:
        conn.rollback()
        report.fail("同步失败", str(exc)[:500])
        return 1
    finally:
        conn.close()
        source_conn.close()

    _safe_delete_preview_files(
        preview_paths_to_delete,
        preview_roots=preview_roots,
        dry_run=args.dry_run or args.keep_preview_files,
        stats=stats,
    )

    elapsed = time.time() - t0
    report.ok(
        "同步 resource_task/resource_file",
        f"新增 {stats.resources_added:,}, 预览级变更 {stats.resources_preview_changed:,}, "
        f"描述级变更 {stats.resources_description_changed:,}, 删除 {stats.resources_deleted:,}, "
        f"未变 {stats.resources_unchanged:,}, 父级 pack 失效 {stats.packs_invalidated:,}, "
        f"插入文件 {stats.files_inserted:,}",
    )
    report.ok(
        "清理预览",
        f"DB 预览行 {stats.preview_rows_deleted:,}, 文件计划 {stats.preview_files_planned:,}, "
        f"删除 {stats.preview_files_deleted:,}, 跳过 {stats.preview_files_skipped:,}",
    )
    if not args.dry_run:
        check_conn = sqlite3.connect(db_path, timeout=300)
        try:
            check = check_conn.execute("PRAGMA quick_check").fetchone()[0]
            fk_count = check_conn.execute("SELECT COUNT(*) FROM pragma_foreign_key_check").fetchone()[0]
        finally:
            check_conn.close()
        report.ok("数据库检查", f"quick_check={check}, foreign_key_check={fk_count}")
    report.ok("后续流程", "generate_previews -> generate_descriptions -> upload_resources")
    report.ok("耗时", f"{elapsed:.1f}s")

    for failure in stats.failure_examples:
        report.fail("同步局部失败", failure)
    ok = report.summary()
    return 0 if ok and stats.failures == 0 else 1


def main() -> int:
    default_db = Path(__file__).resolve().parents[4] / "data" / "databases" / "pipeline.db"
    parser = argparse.ArgumentParser(description="从 crawler_state.db 增量同步 pipeline 本地缓存库")
    parser.add_argument("--crawler-state-db", default=DEFAULT_CRAWLER_STATE_DB, help="crawler_state.db 路径")
    parser.add_argument("--crawler-output", default=DEFAULT_CRAWLER_OUTPUT, help="ResourceCrawler output 根目录")
    parser.add_argument("--db-path", default=str(default_db), help="目标 pipeline SQLite 路径")
    parser.add_argument("--dry-run", action="store_true", help="只统计差异，不写库、不删文件")
    parser.add_argument("--clear-first", action="store_true", help="同步前清空当前 pipeline 表，相当于全量重建")
    parser.add_argument("--replace-db-file", action="store_true", help="clear-first 时直接删除旧 SQLite 文件后重建")
    parser.add_argument("--no-backup", action="store_true", help="apply 前不备份目标数据库")
    parser.add_argument("--keep-preview-files", action="store_true", help="只删 resource_preview 记录，不删除预览文件")
    parser.add_argument(
        "--preview-dir",
        action="append",
        default=[],
        help="允许删除预览文件的目录，可重复传；默认推断常见 previews 目录",
    )
    parser.add_argument("--commit-every", type=int, default=1000, help="每 N 条新增/变更资源提交一次")
    parser.add_argument("--asset-batch-size", type=int, default=10000, help="asset_index 批处理大小")
    return sync(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
