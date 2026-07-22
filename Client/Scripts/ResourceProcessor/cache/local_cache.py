"""SQLite-backed local cache for resource processing state.

Provides persistent storage for breakpoint recovery, result reuse,
and error tracking across processing sessions.
"""

import datetime
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

from ResourceProcessor.preview_metadata import (
    PreviewInfo,
    PreviewStrategy,
    ProcessState,
    ResourceProcessingEntity,
)
from resource_contracts.resource_types import PACK_RESOURCE_TYPE


RESOURCE_FINGERPRINT_VERSION = "client-resource-fingerprint-v2"
CONTENT_HASH_ALGORITHM = "sha256"


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def file_content_hash(path: str | Path | None) -> str:
    """Return a sha256 content hash for a generated artifact."""
    if not path:
        return ""
    try:
        file_path = Path(path)
        if not file_path.is_file():
            return ""
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def description_content_hash(
    *,
    main_content: str = "",
    detail_content: str = "",
    full_description: str = "",
    prompt_version: str = "",
    quality_score: Optional[float] = None,
    usage_space: str = "",
    usage_category: str = "",
    usage_subcategories: Optional[list[str]] = None,
    usage_classification_reason: str = "",
    usage_classification_suggestion: Optional[dict] = None,
    usage_classification_version: str = "",
) -> str:
    return _stable_hash(
        {
            "algorithm": CONTENT_HASH_ALGORITHM,
            "main_content": main_content or "",
            "detail_content": detail_content or "",
            "full_description": full_description or "",
            "prompt_version": prompt_version or "",
            "quality_score": quality_score,
            "usage_space": usage_space or "",
            "usage_category": usage_category or "",
            "usage_subcategories": usage_subcategories or [],
            "usage_classification_reason": usage_classification_reason or "",
            "usage_classification_suggestion": usage_classification_suggestion or {},
            "usage_classification_version": usage_classification_version or "",
        }
    )


def _json_loads_value(value: Any, fallback):
    if value in (None, ""):
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback
    return parsed


def _row_to_dict(row: Any, description: Any = None) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if description is None:
        return dict(row)
    return {description[index][0]: row[index] for index in range(len(description))}


def _fetchone_dict(conn: sqlite3.Connection, sql: str, params=()) -> Optional[dict[str, Any]]:
    cur = conn.execute(sql, params)
    return _row_to_dict(cur.fetchone(), cur.description)


def _fetchall_dicts(conn: sqlite3.Connection, sql: str, params=()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    return [_row_to_dict(row, cur.description) or {} for row in cur.fetchall()]


def _latest_active_preview_rows(conn: sqlite3.Connection, task_id: int) -> list[dict[str, Any]]:
    rows = _fetchall_dicts(
        conn,
        "SELECT * FROM resource_preview WHERE task_id = ? ORDER BY id",
        (task_id,),
    )
    latest_primary_id = max((int(row["id"]) for row in rows if row.get("role") == "primary"), default=None)
    if latest_primary_id is None:
        return rows
    return [
        row
        for row in rows
        if int(row.get("id") or 0) >= latest_primary_id and row.get("role") in {"primary", "gallery"}
    ]


def _stable_manifest_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): value[key]
        for key in sorted(value)
        if key not in {"etag"} and value[key] is not None
    }


def _stable_manifest_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_stable_manifest_dict(item) for item in value if isinstance(item, dict)]


def _object_manifest_fingerprint_parts(conn: sqlite3.Connection, task_id: int) -> dict[str, Any]:
    row = _fetchone_dict(
        conn,
        """SELECT manifest_json, upload_state
           FROM resource_object_manifest
           WHERE task_id = ?""",
        (task_id,),
    )
    if not row or row.get("upload_state") != "uploaded":
        return {}
    manifest = _json_loads_value(row.get("manifest_json"), {})
    if not isinstance(manifest, dict):
        return {}
    return {
        "source_object": _stable_manifest_dict(manifest.get("source_object")),
        "file_structure": _stable_manifest_dict(manifest.get("file_structure")),
        "legacy_source_files": _stable_manifest_list(manifest.get("source_files")),
        "previews": _stable_manifest_list(manifest.get("previews")),
        "package_object": _stable_manifest_dict(manifest.get("package_object")),
    }


def _resource_fingerprint_parts(conn: sqlite3.Connection, task_id: int) -> dict[str, Any]:
    task = _fetchone_dict(conn, "SELECT * FROM resource_task WHERE id = ?", (task_id,))
    if not task:
        return {"version": RESOURCE_FINGERPRINT_VERSION, "missing_task": task_id}

    files = _fetchall_dicts(
        conn,
        """SELECT file_path, file_name, file_size, file_format, content_md5,
                  file_role, ks3_key, is_primary
           FROM resource_file
           WHERE task_id = ?
           ORDER BY is_primary DESC, id""",
        (task_id,),
    )
    previews = _latest_active_preview_rows(conn, task_id)
    description = _fetchone_dict(
        conn,
        "SELECT * FROM resource_description WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    )

    desc_parts: dict[str, Any] = {}
    if description:
        desc_parts = {
            "content_hash": description.get("content_hash") or "",
            "prompt_version": description.get("prompt_version") or "",
            "quality_score": description.get("quality_score"),
            "usage_space": description.get("usage_space") or "",
            "usage_category": description.get("usage_category") or "",
            "usage_subcategories": _json_loads_value(description.get("usage_subcategories"), []),
            "usage_classification_version": description.get("usage_classification_version") or "",
        }

    return {
        "version": RESOURCE_FINGERPRINT_VERSION,
        "task": {
            "content_md5": task.get("content_md5") or "",
            "resource_type": task.get("resource_type") or "",
            "source_directory": task.get("source_directory") or "",
            "source_resource_id": task.get("source_resource_id") or "",
            "title": task.get("title") or "",
            "pack_id": task.get("pack_id") or "",
            "pack_name": task.get("pack_name") or "",
            "source": task.get("source") or "",
            "resource_path": task.get("resource_path") or "",
            "parent_resource_id": task.get("parent_resource_id") or "",
            "child_resource_ids": _json_loads_value(task.get("child_resource_ids"), []),
            "child_resource_count": int(task.get("child_resource_count") or 0),
            "contains_resource_types": _json_loads_value(task.get("contains_resource_types"), []),
            "source_url": task.get("source_url") or "",
            "download_url": task.get("download_url") or "",
            "category": task.get("category") or "",
            "tags": _json_loads_value(task.get("tags"), []),
            "license_name": task.get("license_name") or "",
            "source_description": task.get("source_description") or "",
            "member_count": int(task.get("member_count") or 0),
            "missing_files": _json_loads_value(task.get("missing_files"), []),
            "auxiliary_metadata": _json_loads_value(task.get("auxiliary_metadata"), {}),
        },
        "files": [
            {
                "file_path": row.get("file_path") or "",
                "file_name": row.get("file_name") or "",
                "file_size": int(row.get("file_size") or 0),
                "file_format": row.get("file_format") or "",
                "content_md5": row.get("content_md5") or "",
                "file_role": row.get("file_role") or "",
                "ks3_key": row.get("ks3_key") or "",
                "is_primary": bool(row.get("is_primary")),
            }
            for row in files
        ],
        "previews": [
            {
                "role": row.get("role") or "primary",
                "strategy": row.get("strategy") or "",
                "path": row.get("path") or "",
                "format": row.get("format") or "",
                "width": row.get("width"),
                "height": row.get("height"),
                "size": row.get("size"),
                "renderer": row.get("renderer") or "",
                "used_placeholder": bool(row.get("used_placeholder")),
                "fail_reason": row.get("fail_reason") or "",
                "content_hash": row.get("content_hash") or "",
            }
            for row in previews
        ],
        "uploaded_objects": _object_manifest_fingerprint_parts(conn, task_id),
        "description": desc_parts,
    }


def compute_resource_fingerprint_for_connection(
    conn: sqlite3.Connection,
    task_id: int,
) -> tuple[str, dict[str, Any]]:
    parts = _resource_fingerprint_parts(conn, task_id)
    return _stable_hash(parts), parts


def refresh_resource_fingerprint_for_connection(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    now: str | None = None,
) -> str:
    fingerprint, parts = compute_resource_fingerprint_for_connection(conn, task_id)
    conn.execute(
        """UPDATE resource_task
           SET resource_fingerprint = ?,
               fingerprint_parts_json = ?,
               fingerprint_version = ?,
               updated_at = COALESCE(?, updated_at)
           WHERE id = ?""",
        (
            fingerprint,
            json.dumps(parts, ensure_ascii=False, sort_keys=True),
            RESOURCE_FINGERPRINT_VERSION,
            now,
            task_id,
        ),
    )
    # A completed submission represents committed_fingerprint. If a later
    # description, classification, preview, or task metadata change produces a
    # different resource fingerprint, make the existing uploaded object
    # manifest eligible for submission again. Never disturb an active job;
    # reconciliation will finish it before a subsequent change is submitted.
    conn.execute(
        """UPDATE resource_object_manifest
           SET submit_state = 'pending',
               error_message = '',
               updated_at = COALESCE(?, updated_at)
           WHERE task_id = ?
             AND upload_state = 'uploaded'
             AND submit_state = 'submitted'
             AND committed_fingerprint <> ?""",
        (now, task_id, fingerprint),
    )
    return fingerprint


class LocalCacheStore:
    """SQLite-backed local cache for resource processing state."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, timeout=300, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=300000")
        self._conn.execute("PRAGMA wal_autocheckpoint=0")
        self._create_tables()

    def _write(self, sql: str, params=()):
        """Execute a write SQL, auto-commit."""
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=300000")
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _create_tables(self):
        cur = self._conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS resource_task (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_md5 TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                source_directory TEXT NOT NULL DEFAULT '',
                process_state TEXT NOT NULL DEFAULT 'discovered',
                resource_id TEXT,
                retry_count INTEGER DEFAULT 0,
                last_error_code TEXT DEFAULT '',
                last_error_message TEXT DEFAULT '',
                resource_fingerprint TEXT NOT NULL DEFAULT '',
                fingerprint_parts_json TEXT NOT NULL DEFAULT '{}',
                fingerprint_version TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS resource_file (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES resource_task(id),
                file_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                file_format TEXT NOT NULL,
                content_md5 TEXT NOT NULL,
                file_role TEXT NOT NULL DEFAULT 'main',
                ks3_key TEXT,
                is_primary INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS resource_preview (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES resource_task(id),
                strategy TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'primary',
                path TEXT,
                format TEXT,
                width INTEGER,
                height INTEGER,
                size INTEGER,
                renderer TEXT,
                used_placeholder INTEGER DEFAULT 0,
                fail_reason TEXT,
                content_hash TEXT NOT NULL DEFAULT '',
                content_hash_algorithm TEXT NOT NULL DEFAULT 'sha256',
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS resource_description (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES resource_task(id),
                main_content TEXT NOT NULL DEFAULT '',
                detail_content TEXT NOT NULL DEFAULT '',
                full_description TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                quality_score REAL,
                usage_space TEXT NOT NULL DEFAULT '',
                usage_category TEXT NOT NULL DEFAULT '',
                usage_subcategories TEXT NOT NULL DEFAULT '[]',
                usage_classification_reason TEXT NOT NULL DEFAULT '',
                usage_classification_suggestion TEXT NOT NULL DEFAULT '{}',
                usage_classification_version TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                content_hash_algorithm TEXT NOT NULL DEFAULT 'sha256',
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS resource_upload_job (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES resource_task(id),
                upload_state TEXT NOT NULL DEFAULT 'pending',
                idempotency_key TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS process_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES resource_task(id),
                event TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS resource_object_manifest (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES resource_task(id),
                manifest_json TEXT NOT NULL,
                upload_state TEXT NOT NULL DEFAULT 'uploaded',
                submit_state TEXT NOT NULL DEFAULT 'pending',
                resource_fingerprint TEXT NOT NULL DEFAULT '',
                object_fingerprint TEXT NOT NULL DEFAULT '',
                committed_fingerprint TEXT NOT NULL DEFAULT '',
                upload_options_json TEXT NOT NULL DEFAULT '{}',
                processing_job_id TEXT NOT NULL DEFAULT '',
                processing_result_json TEXT NOT NULL DEFAULT '{}',
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS resource_object_delete_job (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_resource_id TEXT NOT NULL DEFAULT '',
                source_resource_id TEXT NOT NULL DEFAULT '',
                task_id_snapshot INTEGER,
                storage_profile_id TEXT NOT NULL DEFAULT '',
                object_keys_json TEXT NOT NULL DEFAULT '[]',
                object_refs_json TEXT NOT NULL DEFAULT '[]',
                manifest_json_snapshot TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS resource_server_delete_job (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_resource_id TEXT NOT NULL,
                source_resource_id TEXT NOT NULL DEFAULT '',
                task_id_snapshot INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT
            )
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_task_md5
            ON resource_task(content_md5)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_file_md5
            ON resource_file(content_md5)
        """)

        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_object_manifest_task
            ON resource_object_manifest(task_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_object_manifest_submit
            ON resource_object_manifest(upload_state, submit_state, id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_file_task_id
            ON resource_file(task_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_preview_task_id
            ON resource_preview(task_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_description_task_id
            ON resource_description(task_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_upload_job_task_id
            ON resource_upload_job(task_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_process_log_task_id
            ON process_log(task_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_task_state_id
            ON resource_task(process_state, id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_object_delete_job_status
            ON resource_object_delete_job(status, updated_at)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_object_delete_job_client_resource
            ON resource_object_delete_job(client_resource_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_server_delete_job_status
            ON resource_server_delete_job(status, updated_at)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_server_delete_job_client_resource
            ON resource_server_delete_job(client_resource_id)
        """)

        try:
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_description_lease_task_id
                ON description_lease(task_id)
            """)
        except sqlite3.OperationalError:
            pass

        # Migrate old schema: add missing columns if they don't exist
        cur.execute("PRAGMA table_info(resource_task)")
        task_cols = {row["name"] for row in cur.fetchall()}
        if "source_directory" not in task_cols:
            cur.execute("ALTER TABLE resource_task ADD COLUMN source_directory TEXT NOT NULL DEFAULT ''")
        if "source_path" in task_cols:
            pass  # keep old column for backward compat, it will be ignored going forward
        for col_name, col_def in (
            ("resource_fingerprint", "TEXT NOT NULL DEFAULT ''"),
            ("fingerprint_parts_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("fingerprint_version", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col_name not in task_cols:
                cur.execute(f"ALTER TABLE resource_task ADD COLUMN {col_name} {col_def}")

        cur.execute("PRAGMA table_info(resource_preview)")
        preview_cols = {row["name"] for row in cur.fetchall()}
        if "role" not in preview_cols:
            cur.execute("ALTER TABLE resource_preview ADD COLUMN role TEXT NOT NULL DEFAULT 'primary'")
        for col_name, col_def in (
            ("content_hash", "TEXT NOT NULL DEFAULT ''"),
            ("content_hash_algorithm", "TEXT NOT NULL DEFAULT 'sha256'"),
        ):
            if col_name not in preview_cols:
                cur.execute(f"ALTER TABLE resource_preview ADD COLUMN {col_name} {col_def}")

        cur.execute("PRAGMA table_info(resource_description)")
        desc_cols = {row["name"] for row in cur.fetchall()}
        for col_name, col_def in (
            ("usage_space", "TEXT NOT NULL DEFAULT ''"),
            ("usage_category", "TEXT NOT NULL DEFAULT ''"),
            ("usage_subcategories", "TEXT NOT NULL DEFAULT '[]'"),
            ("usage_classification_reason", "TEXT NOT NULL DEFAULT ''"),
            ("usage_classification_suggestion", "TEXT NOT NULL DEFAULT '{}'"),
            ("usage_classification_version", "TEXT NOT NULL DEFAULT ''"),
            ("content_hash", "TEXT NOT NULL DEFAULT ''"),
            ("content_hash_algorithm", "TEXT NOT NULL DEFAULT 'sha256'"),
        ):
            if col_name not in desc_cols:
                cur.execute(f"ALTER TABLE resource_description ADD COLUMN {col_name} {col_def}")

        cur.execute("PRAGMA table_info(resource_object_manifest)")
        manifest_cols = {row["name"] for row in cur.fetchall()}
        for col_name, col_def in (
            ("resource_fingerprint", "TEXT NOT NULL DEFAULT ''"),
            ("object_fingerprint", "TEXT NOT NULL DEFAULT ''"),
            ("committed_fingerprint", "TEXT NOT NULL DEFAULT ''"),
            ("upload_options_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if col_name not in manifest_cols:
                cur.execute(f"ALTER TABLE resource_object_manifest ADD COLUMN {col_name} {col_def}")

        # Schema migration: add new resource_task columns for pipeline split
        cur.execute("PRAGMA table_info(resource_task)")
        task_cols = {row["name"] for row in cur.fetchall()}
        for col_name, col_def in (
            ("source_resource_id", "TEXT NOT NULL DEFAULT ''"),
            ("title", "TEXT NOT NULL DEFAULT ''"),
            ("pack_id", "TEXT NOT NULL DEFAULT ''"),
            ("pack_name", "TEXT NOT NULL DEFAULT ''"),
            ("source", "TEXT NOT NULL DEFAULT ''"),
            ("resource_path", "TEXT NOT NULL DEFAULT ''"),
            ("parent_resource_id", "TEXT NOT NULL DEFAULT ''"),
            ("child_resource_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("child_resource_count", "INTEGER DEFAULT 0"),
            ("contains_resource_types", "TEXT NOT NULL DEFAULT '[]'"),
            ("source_url", "TEXT NOT NULL DEFAULT ''"),
            ("download_url", "TEXT NOT NULL DEFAULT ''"),
            ("category", "TEXT NOT NULL DEFAULT ''"),
            ("tags", "TEXT NOT NULL DEFAULT '[]'"),
            ("license_name", "TEXT NOT NULL DEFAULT ''"),
            ("source_description", "TEXT NOT NULL DEFAULT ''"),
            ("member_count", "INTEGER DEFAULT 0"),
            ("missing_files", "TEXT NOT NULL DEFAULT '[]'"),
            ("auxiliary_metadata", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if col_name not in task_cols:
                cur.execute(f"ALTER TABLE resource_task ADD COLUMN {col_name} {col_def}")

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_task_source_id
            ON resource_task(source_resource_id)
        """)

        now = self._now()
        cur.execute(
            """UPDATE resource_task
               SET process_state = 'preview_ready', updated_at = ?
               WHERE process_state = 'refresh_ready'""",
            (now,),
        )
        cur.execute(
            """UPDATE resource_task
               SET process_state = 'description_failed', updated_at = ?
               WHERE process_state = 'refresh_failed'""",
            (now,),
        )

        self._conn.commit()

    def _now(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _json_dumps(self, value: Any) -> str:
        return json.dumps(value if value is not None else [], ensure_ascii=False)

    def _json_loads(self, value: Any, fallback):
        if value in (None, ""):
            return fallback
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return fallback
        return parsed

    def refresh_resource_fingerprint(self, task_id: int) -> str:
        now = self._now()
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            fingerprint = refresh_resource_fingerprint_for_connection(conn, task_id, now=now)
            conn.commit()
            return fingerprint
        finally:
            conn.close()

    # ---- CRUD for resource_task ----

    def insert_task(self, entity: ResourceProcessingEntity) -> int:
        """Insert a new resource task. Returns the auto-generated task id. Also inserts associated files."""
        now = self._now()
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
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
                    self._json_dumps(entity.child_resource_ids),
                    entity.child_resource_count,
                    self._json_dumps(entity.contains_resource_types),
                    entity.source_url,
                    entity.download_url,
                    entity.category,
                    self._json_dumps(entity.tags),
                    entity.license_name,
                    entity.source_description,
                    entity.member_count,
                    self._json_dumps(entity.missing_files),
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
            task_id = cur.lastrowid

            # Insert associated files
            for f in entity.files:
                conn.execute(
                    """INSERT INTO resource_file
                       (task_id, file_path, file_name, file_size, file_format,
                        content_md5, file_role, ks3_key, is_primary, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task_id, f.file_path, f.file_name, f.file_size, f.file_format,
                        f.content_md5, f.file_role, None, 1 if f.is_primary else 0, now,
                    ),
                )

            refresh_resource_fingerprint_for_connection(conn, int(task_id), now=now)
            conn.commit()
        finally:
            conn.close()
        return task_id

    def get_task_by_id(self, task_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM resource_task WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_task_state_by_source_id(self, source_resource_id: str) -> Optional[str]:
        """返回 source_resource_id 对应任务的 process_state，不存在则返回 None。"""
        row = self._conn.execute(
            "SELECT process_state FROM resource_task WHERE source_resource_id = ?",
            (source_resource_id,),
        ).fetchone()
        return row["process_state"] if row else None

    def get_tasks_by_md5(self, content_md5: str) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM resource_task WHERE content_md5 = ?", (content_md5,)
        ).fetchall()
        return [dict(r) for r in rows]

    def update_task_state(
        self,
        task_id: int,
        state: ProcessState,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        self._write(
            """UPDATE resource_task
               SET process_state = ?, last_error_code = ?,
                   last_error_message = ?, updated_at = ?
               WHERE id = ?""",
            (state.value, error_code, error_message, self._now(), task_id),
        )

    def record_task_error(
        self,
        task_id: int,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        """Record error info without changing process_state."""
        self._write(
            """UPDATE resource_task
               SET last_error_code = ?, last_error_message = ?, updated_at = ?
               WHERE id = ?""",
            (error_code, error_message, self._now(), task_id),
        )

    def update_file_md5(self, task_id: int, file_name: str, content_md5: str) -> None:
        """Update content_md5 for a specific file in resource_file."""
        now = self._now()
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute(
                """UPDATE resource_file
                   SET content_md5 = ?
                   WHERE task_id = ? AND file_name = ?""",
                (content_md5, task_id, file_name),
            )
            refresh_resource_fingerprint_for_connection(conn, task_id, now=now)
            conn.commit()
        finally:
            conn.close()

    def increment_retry(self, task_id: int) -> None:
        self._write(
            "UPDATE resource_task SET retry_count = retry_count + 1, updated_at = ? WHERE id = ?",
            (self._now(), task_id),
        )

    # ---- CRUD for resource_file ----

    def insert_file(self, task_id: int, file_info: "FileInfo") -> int:
        """Insert a file associated with a task."""
        now = self._now()
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            cur = conn.execute(
                """INSERT INTO resource_file
                   (task_id, file_path, file_name, file_size, file_format,
                    content_md5, file_role, ks3_key, is_primary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id, file_info.file_path, file_info.file_name, file_info.file_size,
                    file_info.file_format, file_info.content_md5, file_info.file_role,
                    None, 1 if file_info.is_primary else 0, now,
                ),
            )
            refresh_resource_fingerprint_for_connection(conn, task_id, now=now)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def get_files_by_task(self, task_id: int) -> List[dict]:
        """Return all files associated with a task."""
        rows = self._conn.execute(
            "SELECT * FROM resource_file WHERE task_id = ? ORDER BY is_primary DESC, id",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_file_ks3_key(self, file_id: int, ks3_key: str) -> None:
        """Update the KS3 storage key for a file."""
        now = self._now()
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            row = conn.execute("SELECT task_id FROM resource_file WHERE id = ?", (file_id,)).fetchone()
            conn.execute("UPDATE resource_file SET ks3_key = ? WHERE id = ?", (ks3_key, file_id))
            if row:
                refresh_resource_fingerprint_for_connection(conn, int(row["task_id"]), now=now)
            conn.commit()
        finally:
            conn.close()

    # ---- CRUD for resource_preview ----

    def delete_previews_by_task(self, task_id: int) -> int:
        """Delete all preview rows for a task and return the affected row count."""
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=300000")
        try:
            cur = conn.execute("DELETE FROM resource_preview WHERE task_id = ?", (task_id,))
            refresh_resource_fingerprint_for_connection(conn, task_id, now=self._now())
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()

    def insert_preview(self, task_id: int, preview: PreviewInfo) -> int:
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            content_hash = preview.content_hash or file_content_hash(preview.path)
            now = self._now()
            cur = conn.execute(
                """INSERT INTO resource_preview
                   (task_id, strategy, role, path, format, width, height, size,
                    renderer, used_placeholder, fail_reason, content_hash,
                    content_hash_algorithm, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    preview.strategy.value,
                    preview.role,
                    preview.path,
                    preview.format,
                    preview.width,
                    preview.height,
                    preview.size,
                    preview.renderer,
                    1 if preview.used_placeholder else 0,
                    preview.fail_reason,
                    content_hash,
                    CONTENT_HASH_ALGORITHM,
                    now,
                ),
            )
            refresh_resource_fingerprint_for_connection(conn, task_id, now=now)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def get_previews_by_task(self, task_id: int) -> List[dict]:
        """Return all previews associated with a task."""
        rows = self._conn.execute(
            "SELECT * FROM resource_preview WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_preview_by_task(self, task_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM resource_preview WHERE task_id = ? AND role = 'primary' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_active_previews_by_task(self, task_id: int) -> List[dict]:
        """Return the latest primary preview and gallery rows generated after it."""
        rows = self.get_previews_by_task(task_id)
        latest_primary_id = max((row["id"] for row in rows if row["role"] == "primary"), default=None)
        if latest_primary_id is None:
            return rows
        return [
            row
            for row in rows
            if row["id"] >= latest_primary_id and row["role"] in {"primary", "gallery"}
        ]

    # ---- CRUD for resource_description ----

    def insert_description(
        self,
        task_id: int,
        main_content: str,
        detail_content: str,
        full_description: str,
        prompt_version: str,
        quality_score: Optional[float] = None,
        usage_space: str = "",
        usage_category: str = "",
        usage_subcategories: Optional[list[str]] = None,
        usage_classification_reason: str = "",
        usage_classification_suggestion: Optional[dict] = None,
        usage_classification_version: str = "",
    ) -> int:
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            now = self._now()
            content_hash = description_content_hash(
                main_content=main_content,
                detail_content=detail_content,
                full_description=full_description,
                prompt_version=prompt_version,
                quality_score=quality_score,
                usage_space=usage_space,
                usage_category=usage_category,
                usage_subcategories=usage_subcategories,
                usage_classification_reason=usage_classification_reason,
                usage_classification_suggestion=usage_classification_suggestion,
                usage_classification_version=usage_classification_version,
            )
            cur = conn.execute(
                """INSERT INTO resource_description
                   (task_id, main_content, detail_content, full_description,
                    prompt_version, quality_score, usage_space, usage_category,
                    usage_subcategories, usage_classification_reason,
                    usage_classification_suggestion, usage_classification_version,
                    content_hash, content_hash_algorithm,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    main_content,
                    detail_content,
                    full_description,
                    prompt_version,
                    quality_score,
                    usage_space,
                    usage_category,
                    json.dumps(usage_subcategories or [], ensure_ascii=False),
                    usage_classification_reason,
                    json.dumps(usage_classification_suggestion or {}, ensure_ascii=False),
                    usage_classification_version,
                    content_hash,
                    CONTENT_HASH_ALGORITHM,
                    now,
                ),
            )
            refresh_resource_fingerprint_for_connection(conn, task_id, now=now)
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def get_description_by_task(self, task_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM resource_description WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_pack_child_description_rows(
        self,
        pack_task_id: int,
        *,
        pack_source_resource_id: str = "",
        child_source_ids: Optional[list[str]] = None,
    ) -> List[dict]:
        """Return latest child descriptions for a pack task.

        Children are matched by parent_resource_id first, then by explicit
        child_source_ids. Results are de-duplicated by task id.
        """

        rows_by_task_id: dict[int, dict] = {}

        def add_rows(where_sql: str, params: list[Any]) -> None:
            sql = f"""
                SELECT
                    rt.id AS task_id,
                    rt.source_resource_id,
                    rt.parent_resource_id,
                    rt.resource_type,
                    rt.title,
                    rt.resource_path,
                    rd.main_content,
                    rd.detail_content,
                    rd.full_description,
                    rd.prompt_version,
                    rd.quality_score
                FROM resource_task rt
                JOIN resource_description rd
                  ON rd.id = (
                    SELECT d2.id
                    FROM resource_description d2
                    WHERE d2.task_id = rt.id
                    ORDER BY d2.id DESC
                    LIMIT 1
                  )
                WHERE rt.id <> ?
                  AND rt.resource_type <> ?
                  AND {where_sql}
                ORDER BY rt.id
            """
            for row in self._conn.execute(sql, [pack_task_id, PACK_RESOURCE_TYPE, *params]).fetchall():
                rows_by_task_id[int(row["task_id"])] = dict(row)

        if pack_source_resource_id:
            add_rows("rt.parent_resource_id = ?", [pack_source_resource_id])

        ids = [str(value) for value in (child_source_ids or []) if str(value).strip()]
        for start in range(0, len(ids), 900):
            chunk = ids[start:start + 900]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            add_rows(f"rt.source_resource_id IN ({placeholders})", chunk)

        order = {source_id: index for index, source_id in enumerate(ids)}
        return sorted(
            rows_by_task_id.values(),
            key=lambda row: (
                order.get(str(row.get("source_resource_id") or ""), len(order)),
                int(row.get("task_id") or 0),
            ),
        )

    # ---- process_log ----

    def add_log(self, task_id: int, event: str, detail: str = "") -> int:
        self._write(
            "INSERT INTO process_log (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (task_id, event, detail, self._now()),
        )
        return 0  # lastrowid not available with _write; logs don't need it

    def get_logs(self, task_id: int) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM process_log WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- object-storage processing manifest ----

    def upsert_object_manifest(
        self,
        task_id: int,
        manifest: dict,
        *,
        upload_state: str = "uploaded",
        submit_state: str = "pending",
        resource_fingerprint: str = "",
        object_fingerprint: str = "",
        upload_options: Optional[dict] = None,
    ) -> None:
        now = self._now()
        manifest_json = json.dumps(manifest, ensure_ascii=False)
        upload_options_json = json.dumps(upload_options or {}, ensure_ascii=False, sort_keys=True)
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=300000")
        try:
            conn.execute(
                """INSERT INTO resource_object_manifest
                   (task_id, manifest_json, upload_state, submit_state,
                    resource_fingerprint, object_fingerprint, committed_fingerprint,
                    upload_options_json,
                    processing_job_id, processing_result_json, error_message,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, '', ?, '', '{}', '', ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                       manifest_json = excluded.manifest_json,
                       upload_state = excluded.upload_state,
                       submit_state = excluded.submit_state,
                       resource_fingerprint = excluded.resource_fingerprint,
                       object_fingerprint = excluded.object_fingerprint,
                       upload_options_json = excluded.upload_options_json,
                       processing_job_id = '',
                       processing_result_json = '{}',
                       error_message = '',
                       updated_at = excluded.updated_at""",
                (
                    task_id,
                    manifest_json,
                    upload_state,
                    submit_state,
                    resource_fingerprint,
                    object_fingerprint,
                    upload_options_json,
                    now,
                    now,
                ),
            )
            refreshed_fingerprint = refresh_resource_fingerprint_for_connection(conn, task_id, now=now)
            conn.execute(
                """UPDATE resource_object_manifest
                   SET resource_fingerprint = ?,
                       updated_at = ?
                   WHERE task_id = ?""",
                (refreshed_fingerprint, now, task_id),
            )
            conn.execute(
                """UPDATE resource_task
                   SET last_error_code = '', last_error_message = '', updated_at = ?
                   WHERE id = ? AND last_error_code = 'object_storage_upload_error'""",
                (now, task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_object_manifest(self, task_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM resource_object_manifest WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["manifest"] = self._json_loads(data.get("manifest_json"), {})
        data["processing_result"] = self._json_loads(data.get("processing_result_json"), {})
        data["upload_options"] = self._json_loads(data.get("upload_options_json"), {})
        return data

    def delete_object_manifest(self, task_id: int) -> int:
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=300000")
        try:
            cur = conn.execute("DELETE FROM resource_object_manifest WHERE task_id = ?", (task_id,))
            if cur.rowcount:
                refresh_resource_fingerprint_for_connection(conn, task_id, now=self._now())
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()

    def iter_object_manifests(
        self,
        *,
        limit: int | None = None,
        resource_type: str = "",
        source: str = "",
        submit_state: str = "",
    ):
        sql = """
            SELECT rom.*, rt.resource_type, rt.source
            FROM resource_object_manifest rom
            JOIN resource_task rt ON rt.id = rom.task_id
            WHERE rom.upload_state = 'uploaded'
        """
        params: list[Any] = []
        if submit_state:
            sql += " AND rom.submit_state = ?"
            params.append(submit_state)
        if resource_type:
            sql += " AND rt.resource_type = ?"
            params.append(resource_type)
        if source:
            sql += " AND rt.source = ?"
            params.append(source)
        sql += " ORDER BY rom.id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        for row in self._conn.execute(sql, params).fetchall():
            data = dict(row)
            data["manifest"] = self._json_loads(data.get("manifest_json"), {})
            data["processing_result"] = self._json_loads(data.get("processing_result_json"), {})
            data["upload_options"] = self._json_loads(data.get("upload_options_json"), {})
            yield data

    def iter_inflight_object_manifests(
        self,
        *,
        resource_types: Iterable[str] | None = None,
        source: str = "",
        limit: int | None = None,
    ):
        sql = """
            SELECT rom.task_id, rom.submit_state, rom.resource_fingerprint,
                   rom.processing_job_id, rom.processing_result_json,
                   rt.resource_type, rt.source
            FROM resource_object_manifest rom
            JOIN resource_task rt ON rt.id = rom.task_id
            WHERE rom.upload_state = 'uploaded'
              AND rom.submit_state IN ('queued', 'submitting')
        """
        params: list[Any] = []
        resource_type_values = []
        if isinstance(resource_types, str):
            resource_type_values.append(resource_types)
        else:
            resource_type_values.extend(resource_types or [])
        resource_type_values = list(dict.fromkeys(
            str(value).strip() for value in resource_type_values if str(value or "").strip()
        ))
        if len(resource_type_values) == 1:
            sql += " AND rt.resource_type = ?"
            params.append(resource_type_values[0])
        elif resource_type_values:
            placeholders = ",".join("?" for _ in resource_type_values)
            sql += f" AND rt.resource_type IN ({placeholders})"
            params.extend(resource_type_values)
        if source:
            sql += " AND rt.source = ?"
            params.append(source)
        sql += " ORDER BY rom.updated_at, rom.id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        for row in self._conn.execute(sql, params):
            data = dict(row)
            data["processing_result"] = self._json_loads(data.get("processing_result_json"), {})
            yield data

    def count_inflight_object_manifests(
        self,
        *,
        resource_types: Iterable[str] | None = None,
        source: str = "",
    ) -> int:
        sql = """
            SELECT COUNT(*)
            FROM resource_object_manifest rom
            JOIN resource_task rt ON rt.id = rom.task_id
            WHERE rom.upload_state = 'uploaded'
              AND rom.submit_state IN ('queued', 'submitting')
        """
        params: list[Any] = []
        values = list(dict.fromkeys(
            str(value).strip() for value in (resource_types or []) if str(value or "").strip()
        )) if not isinstance(resource_types, str) else [resource_types]
        if len(values) == 1:
            sql += " AND rt.resource_type = ?"
            params.append(values[0])
        elif values:
            placeholders = ",".join("?" for _ in values)
            sql += f" AND rt.resource_type IN ({placeholders})"
            params.extend(values)
        if source:
            sql += " AND rt.source = ?"
            params.append(source)
        return int(self._conn.execute(sql, params).fetchone()[0])

    def apply_processing_job_statuses(
        self,
        *,
        completed: Iterable[dict] = (),
        active: Iterable[dict] = (),
        failed: Iterable[dict] = (),
    ) -> dict[str, int]:
        """Apply one status-query batch in a single local transaction."""
        counts = {"completed": 0, "active": 0, "failed": 0}
        now = self._now()
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=300000")
        try:
            for item in completed:
                task_id = int(item["task_id"])
                job_id = str(item.get("job_id") or "")
                resource_fingerprint = str(item.get("resource_fingerprint") or "")
                result_json = json.dumps(item.get("result") or {}, ensure_ascii=False)
                cursor = conn.execute(
                    """UPDATE resource_object_manifest
                       SET submit_state = 'submitted',
                           processing_result_json = ?,
                           committed_fingerprint = ?,
                           error_message = '',
                           updated_at = ?
                       WHERE task_id = ? AND processing_job_id = ?""",
                    (result_json, resource_fingerprint, now, task_id, job_id),
                )
                if cursor.rowcount:
                    conn.execute(
                        """UPDATE resource_task
                           SET process_state = ?, last_error_code = '', last_error_message = '', updated_at = ?
                           WHERE id = ?""",
                        (ProcessState.COMMITTED.value, now, task_id),
                    )
                    counts["completed"] += 1
            for item in active:
                cursor = conn.execute(
                    """UPDATE resource_object_manifest
                       SET submit_state = 'queued', processing_result_json = ?,
                           error_message = '', updated_at = ?
                       WHERE task_id = ? AND processing_job_id = ?
                         AND submit_state IN ('queued', 'submitting')""",
                    (
                        json.dumps(item.get("result") or {}, ensure_ascii=False),
                        now,
                        int(item["task_id"]),
                        str(item.get("job_id") or ""),
                    ),
                )
                counts["active"] += int(cursor.rowcount or 0)
            for item in failed:
                job_id = str(item.get("job_id") or "")
                if job_id:
                    cursor = conn.execute(
                        """UPDATE resource_object_manifest
                           SET submit_state = 'submit_failed', error_message = ?, updated_at = ?
                           WHERE task_id = ? AND processing_job_id = ?""",
                        (str(item.get("error") or "processing job failed")[:1000], now, int(item["task_id"]), job_id),
                    )
                else:
                    cursor = conn.execute(
                        """UPDATE resource_object_manifest
                           SET submit_state = 'submit_failed', error_message = ?, updated_at = ?
                           WHERE task_id = ? AND submit_state = 'submitting'
                             AND processing_job_id = ''""",
                        (str(item.get("error") or "processing job id was not recorded")[:1000], now, int(item["task_id"])),
                    )
                counts["failed"] += int(cursor.rowcount or 0)
            conn.commit()
        finally:
            conn.close()
        return counts

    def iter_submit_candidate_task_ids(
        self,
        *,
        limit: int | None = None,
        resource_types: Iterable[str] | None = None,
        source: str = "",
        include_submitting: bool = True,
        force: bool = False,
    ):
        sql = """
            SELECT rom.task_id
            FROM resource_object_manifest rom
            JOIN resource_task rt ON rt.id = rom.task_id
            WHERE rom.upload_state = 'uploaded'
              AND rt.resource_type <> ?
              AND rt.process_state IN (?, ?, ?, ?, ?, ?, ?)
        """
        params: list[Any] = [
            PACK_RESOURCE_TYPE,
            ProcessState.DESCRIPTION_READY.value,
            ProcessState.CLASSIFY_READY.value,
            ProcessState.PACKAGE_READY.value,
            ProcessState.REGISTERED.value,
            ProcessState.UPLOADED.value,
            ProcessState.COMMITTED.value,
            ProcessState.SYNCED.value,
        ]
        if not force:
            states = ["pending", "submit_failed"]
            if include_submitting:
                states.append("submitting")
            placeholders = ",".join("?" for _ in states)
            sql += f" AND rom.submit_state IN ({placeholders})"
            params.extend(states)
        else:
            # A resumed force run must not create a second job while the first
            # force submission is still active.
            sql += " AND rom.submit_state NOT IN ('queued', 'submitting')"
        resource_type_values = []
        if isinstance(resource_types, str):
            resource_type_values.append(resource_types)
        else:
            resource_type_values.extend(resource_types or [])
        resource_type_values = list(dict.fromkeys(str(value).strip() for value in resource_type_values if str(value or "").strip()))
        if len(resource_type_values) == 1:
            sql += " AND rt.resource_type = ?"
            params.append(resource_type_values[0])
        elif resource_type_values:
            placeholders = ",".join("?" for _ in resource_type_values)
            sql += f" AND rt.resource_type IN ({placeholders})"
            params.extend(resource_type_values)
        if source:
            sql += " AND rt.source = ?"
            params.append(source)
        sql += " ORDER BY rom.id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        for row in self._conn.execute(sql, params).fetchall():
            yield row["task_id"]

    def mark_object_manifest_submitted(
        self,
        task_id: int,
        result: dict,
        *,
        resource_fingerprint: str = "",
    ) -> None:
        job_id = str(result.get("job_id") or "")
        now = self._now()
        self._write(
            """UPDATE resource_task
               SET process_state = ?,
                   last_error_code = '',
                   last_error_message = '',
                   updated_at = ?
               WHERE id = ?""",
            (ProcessState.COMMITTED.value, now, task_id),
        )
        if resource_fingerprint:
            self._write(
                """UPDATE resource_object_manifest
                   SET submit_state = 'submitted',
                       resource_fingerprint = CASE WHEN ? != '' THEN ? ELSE resource_fingerprint END,
                       processing_job_id = ?,
                       processing_result_json = ?,
                       committed_fingerprint = ?,
                       error_message = '',
                       updated_at = ?
                   WHERE task_id = ?""",
                (
                    resource_fingerprint,
                    resource_fingerprint,
                    job_id,
                    json.dumps(result, ensure_ascii=False),
                    resource_fingerprint,
                    now,
                    task_id,
                ),
            )
            return
        self._write(
            """UPDATE resource_object_manifest
               SET submit_state = 'submitted',
                   processing_job_id = ?,
                   processing_result_json = ?,
                   committed_fingerprint = resource_fingerprint,
                   error_message = '',
                   updated_at = ?
               WHERE task_id = ?""",
            (
                job_id,
                json.dumps(result, ensure_ascii=False),
                now,
                task_id,
            ),
        )

    def mark_object_manifest_queued(
        self,
        task_id: int,
        result: dict,
        *,
        resource_fingerprint: str = "",
    ) -> None:
        job_id = str(result.get("job_id") or "")
        if resource_fingerprint:
            self._write(
                """UPDATE resource_object_manifest
                   SET submit_state = 'queued',
                       resource_fingerprint = ?,
                       processing_job_id = ?,
                       processing_result_json = ?,
                       error_message = '',
                       updated_at = ?
                   WHERE task_id = ?""",
                (resource_fingerprint, job_id, json.dumps(result, ensure_ascii=False), self._now(), task_id),
            )
            return
        self._write(
            """UPDATE resource_object_manifest
               SET submit_state = 'queued',
                   processing_job_id = ?,
                   processing_result_json = ?,
                   error_message = '',
                   updated_at = ?
               WHERE task_id = ?""",
            (job_id, json.dumps(result, ensure_ascii=False), self._now(), task_id),
        )

    def claim_object_manifest_for_submit(
        self,
        task_id: int,
        *,
        resource_fingerprint: str,
        force: bool = False,
        stale_after_seconds: int = 1800,
        pre_submit_stale_after_seconds: int = 120,
    ) -> bool:
        """Atomically mark an uploaded manifest as being submitted by this client."""
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now = now_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        stale_before = (now_dt - datetime.timedelta(seconds=stale_after_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
        pre_submit_stale_before = (
            now_dt - datetime.timedelta(seconds=pre_submit_stale_after_seconds)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=300000")
        try:
            cur = conn.execute(
                """UPDATE resource_object_manifest
                   SET submit_state = 'submitting',
                       resource_fingerprint = CASE WHEN ? != '' THEN ? ELSE resource_fingerprint END,
                       processing_job_id = '',
                       processing_result_json = '{}',
                       error_message = '',
                       updated_at = ?
                   WHERE task_id = ?
                     AND upload_state = 'uploaded'
                     AND (
                        submit_state != 'submitting'
                        OR updated_at < ?
                        OR (processing_job_id = '' AND updated_at < ?)
                     )
                     AND (? OR committed_fingerprint != ?)""",
                (
                    resource_fingerprint,
                    resource_fingerprint,
                    now,
                    task_id,
                    stale_before,
                    pre_submit_stale_before,
                    1 if force else 0,
                    resource_fingerprint,
                ),
            )
            conn.commit()
            return bool(cur.rowcount)
        finally:
            conn.close()

    def claim_object_manifest_for_async_submit(
        self,
        task_id: int,
        *,
        resource_fingerprint: str,
        request_id: str,
        force: bool = False,
        stale_after_seconds: int = 1800,
        pre_submit_stale_after_seconds: int = 120,
    ) -> bool:
        """Persist the idempotency key while atomically claiming one task."""
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        now = now_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        stale_before = (now_dt - datetime.timedelta(seconds=stale_after_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
        pre_submit_stale_before = (now_dt - datetime.timedelta(seconds=pre_submit_stale_after_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=300000")
        try:
            cursor = conn.execute(
                """UPDATE resource_object_manifest
                   SET submit_state = 'submitting',
                       resource_fingerprint = ?,
                       processing_job_id = '',
                       processing_result_json = ?,
                       error_message = '',
                       updated_at = ?
                   WHERE task_id = ?
                     AND upload_state = 'uploaded'
                     AND (
                        submit_state != 'submitting'
                        OR updated_at < ?
                        OR (processing_job_id = '' AND updated_at < ?)
                     )
                     AND (? OR committed_fingerprint != ?)""",
                (
                    resource_fingerprint,
                    json.dumps({"request_id": request_id}, ensure_ascii=False),
                    now,
                    task_id,
                    stale_before,
                    pre_submit_stale_before,
                    1 if force else 0,
                    resource_fingerprint,
                ),
            )
            conn.commit()
            return bool(cursor.rowcount)
        finally:
            conn.close()

    def mark_object_manifest_async_queued(
        self,
        task_id: int,
        result: dict,
        *,
        resource_fingerprint: str,
    ) -> None:
        """Record accepted job and audit log in one local transaction."""
        now = self._now()
        job_id = str(result.get("job_id") or "")
        result_json = json.dumps(result, ensure_ascii=False)
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=300000")
        try:
            conn.execute(
                """UPDATE resource_object_manifest
                   SET submit_state = 'queued',
                       resource_fingerprint = ?,
                       processing_job_id = ?,
                       processing_result_json = ?,
                       error_message = '',
                       updated_at = ?
                   WHERE task_id = ?""",
                (resource_fingerprint, job_id, result_json, now, task_id),
            )
            conn.execute(
                "INSERT INTO process_log (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "processing_job_submitted", result_json, now),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_object_manifest_submitting_request(
        self,
        task_id: int,
        *,
        request_id: str,
        resource_fingerprint: str,
    ) -> None:
        self._write(
            """UPDATE resource_object_manifest
               SET submit_state = 'submitting',
                   resource_fingerprint = ?,
                   processing_job_id = '',
                   processing_result_json = ?,
                   error_message = '',
                   updated_at = ?
               WHERE task_id = ?""",
            (
                resource_fingerprint,
                json.dumps({"request_id": request_id}, ensure_ascii=False),
                self._now(),
                task_id,
            ),
        )

    def mark_object_manifest_submitting_job(
        self,
        task_id: int,
        result: dict,
        *,
        resource_fingerprint: str = "",
    ) -> None:
        job_id = str(result.get("job_id") or "")
        self._write(
            """UPDATE resource_object_manifest
               SET submit_state = 'submitting',
                   resource_fingerprint = CASE WHEN ? != '' THEN ? ELSE resource_fingerprint END,
                   processing_job_id = ?,
                   processing_result_json = ?,
                   error_message = '',
                   updated_at = ?
               WHERE task_id = ?""",
            (
                resource_fingerprint,
                resource_fingerprint,
                job_id,
                json.dumps(result, ensure_ascii=False),
                self._now(),
                task_id,
            ),
        )

    def mark_object_manifest_submit_failed(self, task_id: int, error_message: str) -> None:
        self._write(
            """UPDATE resource_object_manifest
               SET submit_state = 'submit_failed',
                   error_message = ?,
                   updated_at = ?
               WHERE task_id = ?""",
            (error_message[:1000], self._now(), task_id),
        )

    # ---- Resumption helpers ----

    def get_tasks_by_state(self, state: ProcessState) -> List[dict]:
        """Return all tasks in a given state (for resumption / batch retry)."""
        rows = self._conn.execute(
            "SELECT * FROM resource_task WHERE process_state = ?", (state.value,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_failed_tasks(self) -> List[dict]:
        """Return all tasks in any failed state."""
        failed_states = [
            ProcessState.PREVIEW_FAILED.value,
            ProcessState.DESCRIPTION_FAILED.value,
        ]
        placeholders = ",".join("?" * len(failed_states))
        rows = self._conn.execute(
            f"SELECT * FROM resource_task WHERE process_state IN ({placeholders})",
            failed_states,
        ).fetchall()
        return [dict(r) for r in rows]

    def _update_task_metadata(self, task_id: int, entity: ResourceProcessingEntity) -> None:
        self._write(
            """UPDATE resource_task
               SET content_md5 = ?, resource_type = ?, source_directory = ?,
                   source_resource_id = ?, title = ?, pack_id = ?, pack_name = ?,
                   source = ?, resource_path = ?, parent_resource_id = ?,
                   child_resource_ids = ?, child_resource_count = ?,
                   contains_resource_types = ?, source_url = ?, download_url = ?,
                   category = ?, tags = ?, license_name = ?, source_description = ?,
                   member_count = ?, missing_files = ?, auxiliary_metadata = ?,
                   updated_at = ?
               WHERE id = ?""",
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
                self._json_dumps(entity.child_resource_ids),
                entity.child_resource_count,
                self._json_dumps(entity.contains_resource_types),
                entity.source_url,
                entity.download_url,
                entity.category,
                self._json_dumps(entity.tags),
                entity.license_name,
                entity.source_description,
                entity.member_count,
                self._json_dumps(entity.missing_files),
                json.dumps(entity.auxiliary_metadata or {}, ensure_ascii=False),
                self._now(),
                task_id,
            ),
        )

    def upsert_task(self, entity: ResourceProcessingEntity) -> Tuple[int, bool]:
        """Deduplicate by content_md5, then source_resource_id.

        If a task with the same content_md5 already exists, returns its id and True.
        If it exists but has no files yet, inserts the entity's files.
        Otherwise inserts a new task and returns its id and False.
        """
        rows = self._conn.execute(
            "SELECT id FROM resource_task WHERE content_md5 = ?", (entity.content_md5,)
        ).fetchall()
        if not rows and entity.source_resource_id:
            # Fallback: dedup by source_resource_id when fingerprint changed
            rows = self._conn.execute(
                "SELECT id FROM resource_task WHERE source_resource_id = ?",
                (entity.source_resource_id,),
            ).fetchall()
        if rows:
            task_id = rows[0]["id"]
            self._update_task_metadata(task_id, entity)
            # Backfill files if task exists but resource_file is empty
            if entity.files:
                existing = self.get_files_by_task(task_id)
                if not existing:
                    now = self._now()
                    wconn = sqlite3.connect(self.db_path, timeout=300)
                    wconn.execute("PRAGMA journal_mode=WAL")
                    try:
                        for f in entity.files:
                            wconn.execute(
                                """INSERT INTO resource_file
                                   (task_id, file_path, file_name, file_size, file_format,
                                    content_md5, file_role, ks3_key, is_primary, created_at)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    task_id, f.file_path, f.file_name, f.file_size, f.file_format,
                                    f.content_md5, f.file_role, None, 1 if f.is_primary else 0, now,
                                ),
                            )
                        wconn.commit()
                    finally:
                        wconn.close()
            self.refresh_resource_fingerprint(task_id)
            return task_id, True
        return self.insert_task(entity), False

    def rebuild_entity_from_cache(self, task_id: int) -> Optional[ResourceProcessingEntity]:
        """Reconstruct a ResourceProcessingEntity from task + file + preview + description tables."""
        task = self.get_task_by_id(task_id)
        if task is None:
            return None

        files_rows = self.get_files_by_task(task_id)
        preview_rows = self.get_active_previews_by_task(task_id)
        desc_row = self.get_description_by_task(task_id)

        from ResourceProcessor.preview_metadata import FileInfo

        files = [
            FileInfo(
                file_path=r["file_path"],
                file_name=r["file_name"],
                file_size=r["file_size"],
                file_format=r["file_format"],
                content_md5=r["content_md5"],
                file_role=r["file_role"],
                is_primary=bool(r["is_primary"]),
            )
            for r in files_rows
        ]

        previews = [
            PreviewInfo(
                strategy=PreviewStrategy(r["strategy"]),
                role=r["role"],
                path=r["path"],
                format=r["format"],
                width=r["width"],
                height=r["height"],
                size=r["size"],
                renderer=r["renderer"],
                used_placeholder=bool(r["used_placeholder"]),
                fail_reason=r["fail_reason"],
                content_hash=r.get("content_hash") or "",
            )
            for r in preview_rows
        ]

        entity = ResourceProcessingEntity(
            resource_type=task["resource_type"],
            source_directory=task["source_directory"],
            files=files,
            content_md5=task["content_md5"],
            source=task["source"],
            pack_id=task["pack_id"],
            title=task["title"],
            pack_name=task["pack_name"],
            resource_path=task["resource_path"],
            source_resource_id=task["source_resource_id"],
            parent_resource_id=task["parent_resource_id"] or None,
            child_resource_ids=self._json_loads(task["child_resource_ids"], []),
            child_resource_count=task["child_resource_count"] or 0,
            contains_resource_types=self._json_loads(task["contains_resource_types"], []),
            source_url=task["source_url"],
            download_url=task["download_url"],
            category=task["category"],
            tags=self._json_loads(task["tags"], []),
            license_name=task["license_name"],
            source_description=task["source_description"],
            member_count=task["member_count"] or 0,
            missing_files=self._json_loads(task["missing_files"], []),
            auxiliary_metadata=self._json_loads(task["auxiliary_metadata"], {}),
            process_state=ProcessState(task["process_state"]),
            previews=previews,
            resource_id=task["resource_id"],
            retry_count=task["retry_count"],
            last_error_code=task["last_error_code"],
            last_error_message=task["last_error_message"],
        )

        if desc_row:
            entity.description_main = desc_row["main_content"]
            entity.description_detail = desc_row["detail_content"]
            entity.description_full = desc_row["full_description"]
            entity.prompt_version = desc_row["prompt_version"]
            entity.description_quality_score = desc_row["quality_score"]
            entity.usage_space = desc_row["usage_space"]
            entity.usage_category = desc_row["usage_category"]
            entity.usage_subcategories = self._json_loads(desc_row["usage_subcategories"], [])
            entity.usage_classification_reason = desc_row["usage_classification_reason"]
            entity.usage_classification_suggestion = self._json_loads(desc_row["usage_classification_suggestion"], {})
            entity.usage_classification_version = desc_row["usage_classification_version"]

        return entity

    def iter_tasks_by_state(
        self,
        state: ProcessState,
        *,
        limit: int | None = None,
        resource_type: str = "",
        source: str = "",
    ):
        """按 process_state 遍历 resource_task，逐行 yield task_id。"""
        sql = "SELECT id FROM resource_task WHERE process_state = ?"
        params: list = [state.value]
        if resource_type:
            sql += " AND resource_type = ?"
            params.append(resource_type)
        if source:
            sql += " AND source = ?"
            params.append(source)
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        for row in self._conn.execute(sql, params).fetchall():
            yield row["id"]

    def iter_tasks(
        self,
        *,
        limit: int | None = None,
        resource_type: str = "",
        resource_types: Iterable[str] | None = None,
        process_state: str = "",
        process_states: Iterable[str] | None = None,
        min_task_id: int | None = None,
        max_task_id: int | None = None,
        preview_created_after: str = "",
        source: str = "",
        exclude_uploaded_object_manifest: bool = False,
    ):
        """按筛选条件遍历 resource_task，逐行 yield task_id。"""
        if exclude_uploaded_object_manifest:
            sql = (
                "SELECT rt.id FROM resource_task rt "
                "LEFT JOIN resource_object_manifest rom "
                "ON rom.task_id = rt.id AND rom.upload_state = 'uploaded' "
                "WHERE rom.task_id IS NULL"
            )
        else:
            sql = "SELECT rt.id FROM resource_task rt WHERE 1 = 1"
        params: list = []
        resource_type_items: list[str] = []
        if resource_type:
            resource_type_items.append(resource_type)
        if isinstance(resource_types, str):
            resource_type_items.append(resource_types)
        else:
            resource_type_items.extend(resource_types or [])
        resource_type_values = []
        for value in resource_type_items:
            text = str(value or "").strip()
            if text:
                resource_type_values.append(text)
        resource_type_values = list(dict.fromkeys(resource_type_values))
        if len(resource_type_values) == 1:
            sql += " AND rt.resource_type = ?"
            params.append(resource_type_values[0])
        elif resource_type_values:
            placeholders = ",".join("?" for _ in resource_type_values)
            sql += f" AND rt.resource_type IN ({placeholders})"
            params.extend(resource_type_values)
        process_state_items: list[str] = []
        if process_state:
            process_state_items.append(process_state)
        if isinstance(process_states, str):
            process_state_items.append(process_states)
        else:
            process_state_items.extend(process_states or [])
        process_state_values = []
        for value in process_state_items:
            text = str(value or "").strip()
            if text:
                process_state_values.append(text)
        process_state_values = list(dict.fromkeys(process_state_values))
        if len(process_state_values) == 1:
            sql += " AND rt.process_state = ?"
            params.append(process_state_values[0])
        elif process_state_values:
            placeholders = ",".join("?" for _ in process_state_values)
            sql += f" AND rt.process_state IN ({placeholders})"
            params.extend(process_state_values)
        if min_task_id is not None:
            sql += " AND rt.id >= ?"
            params.append(int(min_task_id))
        if max_task_id is not None:
            sql += " AND rt.id <= ?"
            params.append(int(max_task_id))
        if preview_created_after:
            sql += """
                AND EXISTS (
                    SELECT 1 FROM resource_preview rp
                    WHERE rp.task_id = rt.id
                      AND rp.created_at >= ?
                )
            """
            params.append(preview_created_after)
        if source:
            sql += " AND rt.source = ?"
            params.append(source)
        sql += " ORDER BY rt.id"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        for row in self._conn.execute(sql, params).fetchall():
            yield row["id"]

    def count_tasks_by_state(self) -> dict[str, int]:
        """Return counts of tasks grouped by process_state."""
        rows = self._conn.execute(
            "SELECT process_state, COUNT(*) as cnt FROM resource_task GROUP BY process_state"
        ).fetchall()
        return {row["process_state"]: row["cnt"] for row in rows}

    def close(self):
        self._conn.close()
