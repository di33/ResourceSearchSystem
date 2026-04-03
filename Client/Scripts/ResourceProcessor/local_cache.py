"""SQLite-backed local cache for resource processing state.

Provides persistent storage for breakpoint recovery, result reuse,
and error tracking across processing sessions.
"""

import datetime
import sqlite3
from typing import List, Optional

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
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        cur = self._conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS resource_task (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_md5 TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_size INTEGER NOT NULL,
                source_format TEXT NOT NULL,
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
            CREATE TABLE IF NOT EXISTS resource_preview (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES resource_task(id),
                strategy TEXT NOT NULL,
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

        self._conn.commit()

    def _now(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    # ---- CRUD for resource_task ----

    def insert_task(self, entity: ResourceProcessingEntity) -> int:
        """Insert a new resource task. Returns the auto-generated task id."""
        now = self._now()
        cur = self._conn.execute(
            """INSERT INTO resource_task
               (content_md5, resource_type, source_path, source_name,
                source_size, source_format, process_state, resource_id,
                retry_count, last_error_code, last_error_message,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entity.content_md5,
                entity.resource_type,
                entity.source_path,
                entity.source_name,
                entity.source_size,
                entity.source_format,
                entity.process_state.value,
                entity.resource_id,
                entity.retry_count,
                entity.last_error_code,
                entity.last_error_message,
                now,
                now,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_task_by_id(self, task_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM resource_task WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

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
        self._conn.execute(
            """UPDATE resource_task
               SET process_state = ?, last_error_code = ?,
                   last_error_message = ?, updated_at = ?
               WHERE id = ?""",
            (state.value, error_code, error_message, self._now(), task_id),
        )
        self._conn.commit()

    def increment_retry(self, task_id: int) -> None:
        self._conn.execute(
            "UPDATE resource_task SET retry_count = retry_count + 1, updated_at = ? WHERE id = ?",
            (self._now(), task_id),
        )
        self._conn.commit()

    # ---- CRUD for resource_preview ----

    def insert_preview(self, task_id: int, preview: PreviewInfo) -> int:
        cur = self._conn.execute(
            """INSERT INTO resource_preview
               (task_id, strategy, path, format, width, height, size,
                renderer, used_placeholder, fail_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                preview.strategy.value,
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
        self._conn.commit()
        return cur.lastrowid

    def get_preview_by_task(self, task_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM resource_preview WHERE task_id = ? ORDER BY id DESC LIMIT 1",
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
        cur = self._conn.execute(
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
        self._conn.commit()
        return cur.lastrowid

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
        cur = self._conn.execute(
            """INSERT INTO resource_embedding
               (task_id, dimension, checksum, generate_time, model_version, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (task_id, dimension, checksum, generate_time, model_version, self._now()),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_embedding_by_task(self, task_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM resource_embedding WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return dict(row) if row else None

    # ---- process_log ----

    def add_log(self, task_id: int, event: str, detail: str = "") -> int:
        cur = self._conn.execute(
            "INSERT INTO process_log (task_id, event, detail, created_at) VALUES (?, ?, ?, ?)",
            (task_id, event, detail, self._now()),
        )
        self._conn.commit()
        return cur.lastrowid

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

    def close(self):
        self._conn.close()
