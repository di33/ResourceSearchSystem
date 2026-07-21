"""Restore missing manifest ``source_files`` from the client pipeline cache.

This tool never uploads objects.  It keeps the existing source object and
rebuilds only the logical member list stored in ``resource_object_manifest``.
Dry-run is the default; pass ``--apply`` to write changes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

_CLIENT_SCRIPTS_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _CLIENT_SCRIPTS_ROOT.parents[1]
for _path in (_CLIENT_SCRIPTS_ROOT, _REPO_ROOT, _REPO_ROOT / "Tools"):
    if str(_path) not in sys.path:
        sys.path.append(str(_path))
if os.name == "nt":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

from ObjectStorageUpload.resource_manifest import object_fingerprint_for_entity
from resource_contracts.file_structure import build_file_structure
from ObjectStorageUpload.storage_profiles import load_storage_profiles
from ResourceProcessor.cache.local_cache import (
    LocalCacheStore,
    refresh_resource_fingerprint_for_connection,
)
from ResourceProcessor.pipeline_common import Report, make_arg_parser


ARCHIVE_FORMATS = {"zip", "7z", "rar", "tar", "gz"}


@dataclass(frozen=True)
class BackfillResult:
    scanned: int = 0
    eligible: int = 0
    updated: int = 0
    skipped_limit: int = 0
    skipped_invalid: int = 0


def _split_values(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for part in str(value or "").replace(";", ",").split(","):
            text = part.strip()
            if text:
                result.append(text)
    return list(dict.fromkeys(result))


def _manifest_source_files(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    structure = manifest.get("file_structure") or {}
    value = structure.get("entries") or manifest.get("source_files")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _relative_member_path(source_directory: str, file_path: str, file_name: str) -> str:
    path = Path(file_path)
    member = Path(file_name or path.name)
    if source_directory:
        try:
            candidate = path.resolve().relative_to(Path(source_directory).resolve())
            if candidate.parts and candidate.parts[0] != "..":
                member = candidate
        except (OSError, ValueError):
            pass
    text = str(member).replace("\\", "/").strip("/")
    return text or str(file_name or path.name).replace("\\", "/").strip("/")


def build_source_files(source_directory: str, rows: Iterable[sqlite3.Row | dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the same logical member list as the current object uploader."""
    output: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    primary_seen = False
    for index, raw in enumerate(rows):
        row = dict(raw)
        file_name = str(row.get("file_name") or Path(str(row.get("file_path") or "")).name).strip()
        if not file_name:
            raise ValueError("resource_file has neither file_name nor file_path")
        member = _relative_member_path(source_directory, str(row.get("file_path") or ""), file_name)
        if not member or member.startswith("../"):
            raise ValueError(f"unsafe package member path: {member!r}")
        if member in seen:
            seen[member] += 1
            base, ext = os.path.splitext(member)
            member = f"{base}_{seen[member]}{ext}"
        else:
            seen[member] = 0
        is_primary = bool(row.get("is_primary")) or (not primary_seen and index == 0)
        if is_primary:
            primary_seen = True
        output.append({
            "file_name": file_name,
            "file_format": str(row.get("file_format") or "").lower().lstrip("."),
            "file_size": int(row.get("file_size") or 0),
            "checksum": str(row.get("content_md5") or ""),
            "path_in_package": member,
            "is_primary": is_primary,
        })
    return output


def _candidate_rows(
    conn: sqlite3.Connection,
    *,
    resource_types: list[str],
    task_ids: list[int],
    limit: int | None,
) -> list[sqlite3.Row]:
    where = [
        "fc.file_count > 1",
        "json_array_length(coalesce(json_extract(rom.manifest_json, '$.source_files'), '[]')) = 0",
        "json_array_length(coalesce(json_extract(rom.manifest_json, '$.file_structure.entries'), '[]')) = 0",
        "rom.upload_state = 'uploaded'",
    ]
    params: list[Any] = []
    if resource_types:
        where.append(f"rt.resource_type IN ({','.join('?' for _ in resource_types)})")
        params.extend(resource_types)
    if task_ids:
        where.append(f"rt.id IN ({','.join('?' for _ in task_ids)})")
        params.extend(task_ids)
    sql = f"""
        WITH fc AS (
            SELECT task_id, count(*) AS file_count
            FROM resource_file
            GROUP BY task_id
        )
        SELECT rt.id AS task_id, rt.resource_type, rt.source_directory,
               rt.source_resource_id, rt.title, fc.file_count,
               rom.manifest_json, rom.submit_state, rom.upload_options_json
        FROM resource_task rt
        JOIN fc ON fc.task_id = rt.id
        JOIN resource_object_manifest rom ON rom.task_id = rt.id
        WHERE {' AND '.join(where)}
        ORDER BY rt.id
    """
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def backfill(
    *,
    db_path: str,
    resource_types: list[str],
    task_ids: list[int],
    limit: int | None,
    max_members: int,
    apply: bool,
    report: Report | None = None,
) -> BackfillResult:
    uri = f"{Path(db_path).resolve().as_uri()}?mode={'rw' if apply else 'ro'}"
    conn = sqlite3.connect(uri, uri=True, timeout=300)
    conn.row_factory = sqlite3.Row
    if not apply:
        conn.execute("PRAGMA query_only=ON")
    rows = _candidate_rows(conn, resource_types=resource_types, task_ids=task_ids, limit=limit)
    conn.close()

    store = LocalCacheStore(db_path) if apply else None
    write_conn = store._conn if store is not None else None
    updated = skipped_limit = skipped_invalid = 0
    try:
        for row in rows:
            task_id = int(row["task_id"])
            if max_members > 0 and int(row["file_count"]) > max_members:
                skipped_limit += 1
                if report:
                    report.ok("跳过成员上限", f"task_id={task_id}, files={row['file_count']}")
                continue
            read_conn = write_conn
            close_read_conn = False
            if read_conn is None:
                read_uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
                read_conn = sqlite3.connect(read_uri, uri=True, timeout=300)
                read_conn.row_factory = sqlite3.Row
                close_read_conn = True
            file_rows = read_conn.execute(
                """SELECT file_path, file_name, file_size, file_format, content_md5, is_primary
                   FROM resource_file WHERE task_id = ? ORDER BY is_primary DESC, id""",
                (task_id,),
            ).fetchall()
            if close_read_conn:
                read_conn.close()
            try:
                source_files = build_source_files(str(row["source_directory"] or ""), file_rows)
                if len(source_files) != int(row["file_count"]):
                    raise ValueError("rebuilt source_files count mismatch")
                manifest = json.loads(row["manifest_json"] or "{}")
                source_object = manifest.get("source_object") or {}
                if str(source_object.get("file_format") or "").lower().lstrip(".") not in ARCHIVE_FORMATS:
                    raise ValueError("existing multi-file source object is not an archive")
                if not source_object.get("object_key"):
                    raise ValueError("existing source object has no object_key")
                manifest.pop("source_files", None)
                manifest["file_structure"] = build_file_structure(
                    source_files,
                    source="client",
                    source_object_checksum=str(source_object.get("checksum") or ""),
                )
                if not apply:
                    if report:
                        report.ok("计划回填", f"task_id={task_id}, files={len(source_files)}")
                    continue

                assert store is not None
                entity = store.rebuild_entity_from_cache(task_id)
                if entity is None:
                    raise ValueError("could not rebuild entity")
                options = json.loads(row["upload_options_json"] or "{}")
                profile_id = str(options.get("storage_profile_id") or source_object.get("storage_profile_id") or "")
                resolved_profile = load_storage_profiles().get(profile_id or None).profile_id
                object_fingerprint, _ = object_fingerprint_for_entity(
                    entity,
                    client_id=str(options.get("client_id") or manifest.get("request_id", "client").split(":", 1)[0]),
                    storage_profile_id=resolved_profile,
                    key_prefix=str(options.get("key_prefix") or ""),
                    include_previews=bool(options.get("include_previews", True)),
                )
                now = store._now()
                write_conn.execute(
                    """UPDATE resource_object_manifest
                       SET manifest_json = ?, submit_state = ?, object_fingerprint = ?,
                           processing_job_id = '', processing_result_json = '{}',
                           error_message = '', updated_at = ?
                       WHERE task_id = ? AND upload_state = 'uploaded'""",
                    (
                        json.dumps(manifest, ensure_ascii=False),
                        "package_only" if row["resource_type"] == "pack" else "pending",
                        object_fingerprint,
                        now,
                        task_id,
                    ),
                )
                refreshed = refresh_resource_fingerprint_for_connection(write_conn, task_id, now=now)
                write_conn.execute(
                    "UPDATE resource_object_manifest SET resource_fingerprint = ? WHERE task_id = ?",
                    (refreshed, task_id),
                )
                write_conn.execute(
                    "INSERT INTO process_log (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, "manifest_source_files_backfilled", f"files={len(source_files)}", now),
                )
                updated += 1
                if updated % 250 == 0:
                    write_conn.commit()
                if report:
                    report.ok("已回填", f"task_id={task_id}, files={len(source_files)}")
            except Exception as exc:
                skipped_invalid += 1
                if report:
                    report.fail("回填失败", f"task_id={task_id}: {str(exc)[:200]}")
    finally:
        if store is not None:
            store._conn.commit()
            store.close()
    return BackfillResult(
        scanned=len(rows), eligible=len(rows) - skipped_limit - skipped_invalid,
        updated=updated, skipped_limit=skipped_limit, skipped_invalid=skipped_invalid,
    )


def main() -> int:
    parser = make_arg_parser(
        "从客户端 resource_file 恢复 uploaded manifest 的 source_files（默认 dry-run）",
        extra_args=[
            ("--resource-types", {"action": "append", "default": [], "help": "资源类型，可逗号分隔或重复传入"}),
            ("--task-id", {"action": "append", "type": int, "default": [], "help": "指定 task id，可重复传入"}),
            ("--max-members", {"type": int, "default": 512, "help": "默认跳过超过加工服务器成员上限 512 的资源；0 表示不限"}),
            ("--apply", {"action": "store_true", "help": "实际写入；不传则只做 dry-run"}),
            ("--verbose", {"action": "store_true", "help": "逐条输出；默认只输出汇总"}),
        ],
    )
    args = parser.parse_args()
    resource_types = _split_values([args.resource_type, *args.resource_types])
    if args.apply and not resource_types and not args.task_id:
        parser.error("--apply 必须配合资源类型或 --task-id，避免无边界批量写入")
    report = Report(label="manifest source_files 回填")
    result = backfill(
        db_path=args.db_path,
        resource_types=resource_types,
        task_ids=list(args.task_id or []),
        limit=args.limit,
        max_members=max(0, int(args.max_members)),
        apply=bool(args.apply),
        report=report if args.verbose else None,
    )
    report.ok(
        "汇总",
        f"scanned={result.scanned}, eligible={result.eligible}, updated={result.updated}, "
        f"skipped_limit={result.skipped_limit}, skipped_invalid={result.skipped_invalid}",
    )
    return 0 if report.summary() else 1


if __name__ == "__main__":
    raise SystemExit(main())
