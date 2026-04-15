"""SQLite-backed local cache for resource processing state.

Provides persistent storage for breakpoint recovery, result reuse,
and error tracking across processing sessions.
"""

import contextlib
import datetime
import os
import sqlite3
import time as _time
from typing import Any, List, Optional, Tuple

from ResourceProcessor.preview_metadata import (
    PreviewInfo,
    PreviewStrategy,
    ProcessState,
    ResourceProcessingEntity,
)


class LocalCacheStore:
    """SQLite-backed local cache for resource processing state."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock_path = db_path + ".lock"
        self._conn = sqlite3.connect(db_path, timeout=300)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=300000")
        self._conn.execute("PRAGMA wal_autocheckpoint=0")
        self._create_tables()

    @contextlib.contextmanager
    def _file_lock(self, timeout: float = 120):
        """Cross-process file lock for serializing DB writes."""
        lock_fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            deadline = _time.monotonic() + timeout
            while True:
                try:
                    import msvcrt
                    msvcrt.locking(lock_fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if _time.monotonic() >= deadline:
                        raise TimeoutError(f"Could not acquire DB lock within {timeout}s")
                    _time.sleep(0.1)
            try:
                yield
            finally:
                try:
                    import msvcrt
                    msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            os.close(lock_fd)

    def _write(self, sql: str, params=()):
        """Execute a write SQL under file lock, auto-commit."""
        with self._file_lock():
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
                created_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS resource_embedding (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES resource_task(id),
                dimension INTEGER NOT NULL DEFAULT 0,
                checksum TEXT NOT NULL DEFAULT '',
                generate_time REAL NOT NULL DEFAULT 0.0,
                model_version TEXT NOT NULL DEFAULT '',
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
            CREATE INDEX IF NOT EXISTS idx_resource_task_md5
            ON resource_task(content_md5)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_file_md5
            ON resource_file(content_md5)
        """)

        # Migrate old schema: add missing columns if they don't exist
        cur.execute("PRAGMA table_info(resource_task)")
        task_cols = {row["name"] for row in cur.fetchall()}
        if "source_directory" not in task_cols:
            cur.execute("ALTER TABLE resource_task ADD COLUMN source_directory TEXT NOT NULL DEFAULT ''")
        if "source_path" in task_cols:
            pass  # keep old column for backward compat, it will be ignored going forward

        cur.execute("PRAGMA table_info(resource_preview)")
        preview_cols = {row["name"] for row in cur.fetchall()}
        if "role" not in preview_cols:
            cur.execute("ALTER TABLE resource_preview ADD COLUMN role TEXT NOT NULL DEFAULT 'primary'")

        # Schema migration: add new resource_task columns for pipeline split
        cur.execute("PRAGMA table_info(resource_task)")
        task_cols = {row["name"] for row in cur.fetchall()}
        for col_name, col_def in (
            ("source_resource_id", "TEXT NOT NULL DEFAULT ''"),
            ("title", "TEXT NOT NULL DEFAULT ''"),
            ("pack_name", "TEXT NOT NULL DEFAULT ''"),
            ("source", "TEXT NOT NULL DEFAULT ''"),
            ("resource_path", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col_name not in task_cols:
                cur.execute(f"ALTER TABLE resource_task ADD COLUMN {col_name} {col_def}")

        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_task_source_id
            ON resource_task(source_resource_id)
        """)

        self._conn.commit()

    def _now(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    # ---- CRUD for resource_task ----

    def insert_task(self, entity: ResourceProcessingEntity) -> int:
        """Insert a new resource task. Returns the auto-generated task id. Also inserts associated files."""
        now = self._now()
        with self._file_lock():
            conn = sqlite3.connect(self.db_path, timeout=300)
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                cur = conn.execute(
                    """INSERT INTO resource_task
                       (content_md5, resource_type, source_directory,
                        source_resource_id, title, pack_name, source, resource_path,
                        process_state, resource_id,
                        retry_count, last_error_code, last_error_message,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entity.content_md5,
                        entity.resource_type,
                        entity.source_directory,
                        entity.source_resource_id,
                        entity.title,
                        entity.pack_name,
                        entity.source,
                        entity.resource_path,
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

    def increment_retry(self, task_id: int) -> None:
        self._write(
            "UPDATE resource_task SET retry_count = retry_count + 1, updated_at = ? WHERE id = ?",
            (self._now(), task_id),
        )

    # ---- CRUD for resource_file ----

    def insert_file(self, task_id: int, file_info: "FileInfo") -> int:
        """Insert a file associated with a task."""
        now = self._now()
        with self._file_lock():
            conn = sqlite3.connect(self.db_path, timeout=300)
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
        self._write(
            "UPDATE resource_file SET ks3_key = ? WHERE id = ?",
            (ks3_key, file_id),
        )

    # ---- CRUD for resource_preview ----

    def insert_preview(self, task_id: int, preview: PreviewInfo) -> int:
        with self._file_lock():
            conn = sqlite3.connect(self.db_path, timeout=300)
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                cur = conn.execute(
                    """INSERT INTO resource_preview
                       (task_id, strategy, role, path, format, width, height, size,
                        renderer, used_placeholder, fail_reason, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                        self._now(),
                    ),
                )
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

    # ---- CRUD for resource_description ----

    def insert_description(
        self,
        task_id: int,
        main_content: str,
        detail_content: str,
        full_description: str,
        prompt_version: str,
        quality_score: Optional[float] = None,
    ) -> int:
        with self._file_lock():
            conn = sqlite3.connect(self.db_path, timeout=300)
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                cur = conn.execute(
                    """INSERT INTO resource_description
                       (task_id, main_content, detail_content, full_description,
                        prompt_version, quality_score, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task_id,
                        main_content,
                        detail_content,
                        full_description,
                        prompt_version,
                        quality_score,
                        self._now(),
                    ),
                )
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

    # ---- CRUD for resource_embedding ----

    def insert_embedding(
        self,
        task_id: int,
        dimension: int,
        checksum: str,
        generate_time: float,
        model_version: str,
    ) -> int:
        with self._file_lock():
            conn = sqlite3.connect(self.db_path, timeout=300)
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                cur = conn.execute(
                    """INSERT INTO resource_embedding
                       (task_id, dimension, checksum, generate_time, model_version, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (task_id, dimension, checksum, generate_time, model_version, self._now()),
                )
                conn.commit()
                return cur.lastrowid
            finally:
                conn.close()

    def get_embedding_by_task(self, task_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM resource_embedding WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

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
            ProcessState.EMBEDDING_FAILED.value,
        ]
        placeholders = ",".join("?" * len(failed_states))
        rows = self._conn.execute(
            f"SELECT * FROM resource_task WHERE process_state IN ({placeholders})",
            failed_states,
        ).fetchall()
        return [dict(r) for r in rows]

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
            # Backfill files if task exists but resource_file is empty
            if entity.files:
                existing = self.get_files_by_task(task_id)
                if not existing:
                    now = self._now()
                    with self._file_lock():
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
            return task_id, True
        return self.insert_task(entity), False

    def rebuild_entity_from_cache(self, task_id: int) -> Optional[ResourceProcessingEntity]:
        """Reconstruct a ResourceProcessingEntity from task + file + preview + description tables."""
        task = self.get_task_by_id(task_id)
        if task is None:
            return None

        files_rows = self.get_files_by_task(task_id)
        preview_rows = self.get_previews_by_task(task_id)
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
            )
            for r in preview_rows
        ]

        entity = ResourceProcessingEntity(
            resource_type=task["resource_type"],
            source_directory=task["source_directory"],
            files=files,
            content_md5=task["content_md5"],
            source=task["source"],
            title=task["title"],
            pack_name=task["pack_name"],
            resource_path=task["resource_path"],
            source_resource_id=task["source_resource_id"],
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

        return entity

    def count_tasks_by_state(self) -> dict[str, int]:
        """Return counts of tasks grouped by process_state."""
        rows = self._conn.execute(
            "SELECT process_state, COUNT(*) as cnt FROM resource_task GROUP BY process_state"
        ).fetchall()
        return {row["process_state"]: row["cnt"] for row in rows}

    def close(self):
        self._conn.close()
