"""Flush queued object-storage delete jobs from pipeline.db.

The crawler sync records delete intent before removing local task rows. This
tool performs the network side effect later, so object-storage outages do not
block local cache synchronization.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

_CLIENT_SCRIPTS = Path(__file__).resolve().parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_CLIENT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CLIENT_SCRIPTS))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ObjectStorageUpload.uploader import ObjectStorageUploader  # noqa: E402
from ResourceProcessor.cache.local_cache import LocalCacheStore  # noqa: E402
from ResourceProcessor.pipeline_common import Report, make_arg_parser, print_progress  # noqa: E402


DELETE_JOB_TABLE = "resource_object_delete_job"
FINAL_STATUSES = {"deleted", "superseded"}


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


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _append_ref(
    refs: list[dict[str, str]],
    value: Any,
    *,
    fallback_storage_profile_id: str = "",
) -> None:
    if not isinstance(value, dict):
        return
    object_key = str(value.get("object_key") or "").strip()
    if not object_key:
        return
    refs.append(
        {
            "storage_profile_id": str(value.get("storage_profile_id") or fallback_storage_profile_id or "").strip(),
            "object_key": object_key,
        }
    )


def _live_object_refs_from_manifest(manifest: dict[str, Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    source_object = manifest.get("source_object")
    source_storage_profile_id = ""
    if isinstance(source_object, dict):
        source_storage_profile_id = str(source_object.get("storage_profile_id") or "").strip()
    _append_ref(refs, source_object)
    for item in manifest.get("source_files") or []:
        _append_ref(refs, item, fallback_storage_profile_id=source_storage_profile_id)
    for item in manifest.get("previews") or []:
        _append_ref(refs, item, fallback_storage_profile_id=source_storage_profile_id)
    _append_ref(refs, manifest.get("package_object"), fallback_storage_profile_id=source_storage_profile_id)
    return refs


def _load_live_object_keys(conn: sqlite3.Connection) -> tuple[set[tuple[str, str]], set[str]]:
    pairs: set[tuple[str, str]] = set()
    keys: set[str] = set()
    if not _table_exists(conn, "resource_object_manifest"):
        return pairs, keys
    rows = conn.execute(
        "SELECT manifest_json FROM resource_object_manifest WHERE upload_state = 'uploaded'"
    ).fetchall()
    for row in rows:
        manifest = _json_loads(row["manifest_json"], {})
        if not isinstance(manifest, dict):
            continue
        for ref in _live_object_refs_from_manifest(manifest):
            object_key = ref["object_key"]
            storage_profile_id = ref.get("storage_profile_id", "")
            pairs.add((storage_profile_id, object_key))
            keys.add(object_key)
    return pairs, keys


def _job_refs(row: sqlite3.Row) -> list[dict[str, str]]:
    refs = _json_loads(row["object_refs_json"], [])
    if isinstance(refs, list) and refs:
        result = []
        fallback_profile = str(row["storage_profile_id"] or "").strip()
        for item in refs:
            if not isinstance(item, dict):
                continue
            object_key = str(item.get("object_key") or "").strip()
            if not object_key:
                continue
            result.append(
                {
                    "storage_profile_id": str(item.get("storage_profile_id") or fallback_profile or "").strip(),
                    "object_key": object_key,
                }
            )
        return result

    keys = _json_loads(row["object_keys_json"], [])
    if not isinstance(keys, list):
        return []
    storage_profile_id = str(row["storage_profile_id"] or "").strip()
    return [
        {"storage_profile_id": storage_profile_id, "object_key": str(key)}
        for key in keys
        if str(key or "").strip()
    ]


def _is_live_ref(ref: dict[str, str], live_pairs: set[tuple[str, str]], live_keys: set[str]) -> bool:
    object_key = ref["object_key"]
    storage_profile_id = ref.get("storage_profile_id", "")
    if storage_profile_id:
        return (storage_profile_id, object_key) in live_pairs or ("", object_key) in live_pairs
    return object_key in live_keys


def _mark_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    status: str,
    last_error: str = "",
    increment_attempt: bool = False,
) -> None:
    now = _now()
    deleted_at = now if status in FINAL_STATUSES else None
    if increment_attempt:
        conn.execute(
            """UPDATE resource_object_delete_job
               SET status = ?, attempt_count = attempt_count + 1,
                   last_error = ?, updated_at = ?, deleted_at = ?
               WHERE id = ?""",
            (status, last_error[:1000], now, deleted_at, job_id),
        )
    else:
        conn.execute(
            """UPDATE resource_object_delete_job
               SET status = ?, last_error = ?, updated_at = ?, deleted_at = ?
               WHERE id = ?""",
            (status, last_error[:1000], now, deleted_at, job_id),
        )


def _pending_jobs(conn: sqlite3.Connection, *, limit: int | None, max_attempts: int) -> list[sqlite3.Row]:
    sql = f"""
        SELECT *
        FROM {DELETE_JOB_TABLE}
        WHERE status IN ('pending', 'failed')
          AND attempt_count < ?
        ORDER BY id
    """
    params: list[Any] = [max_attempts]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def _delete_grouped_refs(
    grouped: dict[str, list[str]],
    *,
    uploaders: dict[str, ObjectStorageUploader] | None = None,
) -> int:
    deleted = 0
    for storage_profile_id, object_keys in grouped.items():
        if uploaders is None:
            uploader = ObjectStorageUploader(storage_profile_id=storage_profile_id or None)
        else:
            cache_key = storage_profile_id or ""
            uploader = uploaders.get(cache_key)
            if uploader is None:
                uploader = ObjectStorageUploader(storage_profile_id=storage_profile_id or None)
                uploaders[cache_key] = uploader
        deleted += uploader.delete_objects(object_keys)
    return deleted


def _unique_refs(refs: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for ref in refs:
        object_key = str(ref.get("object_key") or "").strip()
        storage_profile_id = str(ref.get("storage_profile_id") or "").strip()
        if not object_key:
            continue
        unique = (storage_profile_id, object_key)
        if unique in seen:
            continue
        seen.add(unique)
        result.append({"storage_profile_id": storage_profile_id, "object_key": object_key})
    return result


def flush(args) -> int:
    db_path = os.path.abspath(args.db_path)
    LocalCacheStore(db_path).close()

    report = Report(label="对象存储删除队列")
    conn = sqlite3.connect(db_path, timeout=300)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=300000")
    try:
        if not _table_exists(conn, DELETE_JOB_TABLE):
            report.ok("删除队列", "表不存在，无需处理")
            return 0

        live_pairs, live_keys = _load_live_object_keys(conn)
        jobs = _pending_jobs(conn, limit=args.limit, max_attempts=args.max_attempts)
        report.ok("待处理任务", f"{len(jobs)} 个")

        deleted_jobs = 0
        superseded_jobs = 0
        failed_jobs = 0
        deleted_keys = 0
        protected_keys = 0
        processed_jobs = 0
        batch_size = max(1, int(getattr(args, "batch_size", 1000) or 1000))
        progress_every = int(getattr(args, "progress_every", 1000) or 0)
        uploaders: dict[str, ObjectStorageUploader] = {}
        batch_jobs: list[dict[str, Any]] = []
        batch_grouped: dict[str, list[str]] = defaultdict(list)
        batch_key_count = 0

        def flush_batch() -> None:
            nonlocal batch_jobs, batch_grouped, batch_key_count
            nonlocal deleted_jobs, failed_jobs, deleted_keys
            if not batch_jobs:
                return
            try:
                count = _delete_grouped_refs(batch_grouped, uploaders=uploaders)
                for item in batch_jobs:
                    skipped_detail = (
                        f"; protected_keys={item['protected_keys']}"
                        if item["protected_keys"]
                        else ""
                    )
                    _mark_job(
                        conn,
                        item["job_id"],
                        status="deleted",
                        last_error=f"deleted_keys={item['delete_keys']}{skipped_detail}",
                    )
                conn.commit()
                deleted_jobs += len(batch_jobs)
                deleted_keys += count
            except Exception as exc:
                conn.rollback()
                for item in batch_jobs:
                    _mark_job(
                        conn,
                        item["job_id"],
                        status="failed",
                        last_error=str(exc),
                        increment_attempt=True,
                    )
                conn.commit()
                failed_jobs += len(batch_jobs)
                report.fail(
                    "删除失败",
                    f"batch_jobs={len(batch_jobs)}, batch_keys={batch_key_count}: {str(exc)[:160]}",
                )
            finally:
                batch_jobs = []
                batch_grouped = defaultdict(list)
                batch_key_count = 0

        for row in jobs:
            job_id = int(row["id"])
            refs = _unique_refs(_job_refs(row))
            protected = [ref for ref in refs if _is_live_ref(ref, live_pairs, live_keys)]
            deletable = [ref for ref in refs if not _is_live_ref(ref, live_pairs, live_keys)]
            protected_keys += len(protected)
            processed_jobs += 1

            if args.dry_run:
                report.ok(
                    "dry-run",
                    f"job_id={job_id}, delete_keys={len(deletable)}, protected_keys={len(protected)}",
                )
                continue

            if not refs:
                _mark_job(conn, job_id, status="deleted", last_error="no object refs")
                conn.commit()
                deleted_jobs += 1
                continue
            if not deletable:
                _mark_job(conn, job_id, status="superseded", last_error="all keys still referenced by live manifests")
                conn.commit()
                superseded_jobs += 1
                continue

            batch_jobs.append(
                {
                    "job_id": job_id,
                    "delete_keys": len(deletable),
                    "protected_keys": len(protected),
                }
            )
            for ref in deletable:
                batch_grouped[ref.get("storage_profile_id", "")].append(ref["object_key"])
            batch_key_count += len(deletable)

            if batch_key_count >= batch_size:
                flush_batch()
            if progress_every and processed_jobs % progress_every == 0:
                print_progress(
                    processed_jobs,
                    len(jobs),
                    (
                        f"deleted_jobs={deleted_jobs}, failed_jobs={failed_jobs}, "
                        f"superseded_jobs={superseded_jobs}"
                    ),
                )

        if not args.dry_run:
            flush_batch()
            if progress_every:
                print_progress(
                    processed_jobs,
                    len(jobs),
                    (
                        f"deleted_jobs={deleted_jobs}, failed_jobs={failed_jobs}, "
                        f"superseded_jobs={superseded_jobs}"
                    ),
                )

        report.ok(
            "处理结果",
            (
                f"deleted_jobs={deleted_jobs}, superseded_jobs={superseded_jobs}, "
                f"failed_jobs={failed_jobs}, deleted_keys={deleted_keys}, protected_keys={protected_keys}"
            ),
        )
        ok = report.summary()
        return 0 if ok and failed_jobs == 0 else 1
    finally:
        conn.close()


def main() -> int:
    parser = make_arg_parser(
        "重试对象存储删除队列",
        extra_args=[
            ("--dry-run", {"action": "store_true", "help": "只打印计划，不删除对象、不改任务状态"}),
            ("--max-attempts", {"type": int, "default": 10, "help": "失败任务最大尝试次数"}),
            ("--batch-size", {"type": int, "default": 1000, "help": "每批最多删除多少个对象 key"}),
            ("--progress-every", {"type": int, "default": 1000, "help": "每处理多少个任务打印一次进度，0 表示不打印"}),
        ],
    )
    args = parser.parse_args()
    return flush(args)


if __name__ == "__main__":
    raise SystemExit(main())
