from __future__ import annotations

from contextlib import contextmanager
import datetime
import json
import sqlite3
import threading
from typing import Any, Iterator

from resource_processing_server.app.config import settings
from resource_processing_server.app.database import PostgresDatabase


_SQLITE_LOCK = threading.RLock()


def _configure_sqlite_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.Error:
        # Windows bind mounts can make journal-mode changes unreliable. Prefer
        # continuing in SQLite's current mode over failing normal reads/writes.
        pass
    conn.execute("PRAGMA busy_timeout=300000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def sqlite_connection(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open one SQLite connection under the process-wide snapshots DB lock."""
    with _SQLITE_LOCK:
        conn = sqlite3.connect(db_path, timeout=300)
        try:
            yield _configure_sqlite_connection(conn)
        finally:
            conn.close()


class ProcessedSnapshotStore:
    """Stores the latest successful processing snapshot per client resource.

    The snapshot is a replayable SearchServer upsert payload. It does not store
    binaries or embeddings.
    """

    def __init__(self, db_path: str | None = None, *, database: PostgresDatabase | None = None):
        self.db_path = db_path or settings.snapshot_db_path
        self.database = database
        self._create_tables()

    def _connect(self):
        if self.database is not None:
            return self.database.connect()
        return sqlite_connection(self.db_path)

    def _create_tables(self) -> None:
        with self._connect() as conn:
            id_sql = "BIGSERIAL PRIMARY KEY" if self.database is not None else "INTEGER PRIMARY KEY AUTOINCREMENT"
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS processed_resource_snapshot (
                    id {id_sql},
                    client_id TEXT NOT NULL,
                    client_resource_id TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_fingerprint TEXT NOT NULL DEFAULT '',
                    snapshot_json TEXT NOT NULL,
                    description_source TEXT NOT NULL DEFAULT '',
                    preview_source TEXT NOT NULL DEFAULT '',
                    search_upsert_state TEXT NOT NULL DEFAULT 'pending',
                    search_resource_id TEXT NOT NULL DEFAULT '',
                    last_upsert_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(client_id, client_resource_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_processed_snapshot_upsert_state
                ON processed_resource_snapshot(search_upsert_state)
            """)
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS processed_resource_delete_marker (
                    id {id_sql},
                    client_id TEXT NOT NULL,
                    client_resource_id TEXT NOT NULL,
                    resource_id TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(client_id, client_resource_id)
                )
            """)
            conn.commit()

    def _now(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def save_pending(self, payload: dict[str, Any], *, resource_fingerprint: str = "") -> None:
        now = self._now()
        processing = payload.get("processing") or {}
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO processed_resource_snapshot
                   (client_id, client_resource_id, resource_type, resource_fingerprint,
                    snapshot_json, description_source, preview_source,
                    search_upsert_state, search_resource_id, last_upsert_error,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', '', ?, ?)
                   ON CONFLICT(client_id, client_resource_id) DO UPDATE SET
                       resource_type = excluded.resource_type,
                       resource_fingerprint = excluded.resource_fingerprint,
                       snapshot_json = excluded.snapshot_json,
                       description_source = excluded.description_source,
                       preview_source = excluded.preview_source,
                       search_upsert_state = 'pending',
                       search_resource_id = '',
                       last_upsert_error = '',
                       updated_at = excluded.updated_at""",
                (
                    str(payload.get("client_id") or ""),
                    str(payload.get("client_resource_id") or ""),
                    str(payload.get("resource_type") or ""),
                    resource_fingerprint,
                    json.dumps(payload, ensure_ascii=False),
                    str(processing.get("description_source") or ""),
                    str(processing.get("preview_source") or ""),
                    now,
                    now,
                ),
            )
            conn.commit()

    def mark_upserted(self, *, client_id: str, client_resource_id: str, search_resource_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE processed_resource_snapshot
                   SET search_upsert_state = 'upserted',
                       search_resource_id = ?,
                       last_upsert_error = '',
                       updated_at = ?
                   WHERE client_id = ? AND client_resource_id = ?""",
                (search_resource_id, self._now(), client_id, client_resource_id),
            )
            conn.commit()

    def mark_upsert_failed(self, *, client_id: str, client_resource_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE processed_resource_snapshot
                   SET search_upsert_state = 'upsert_failed',
                       last_upsert_error = ?,
                       updated_at = ?
                   WHERE client_id = ? AND client_resource_id = ?""",
                (error[:2000], self._now(), client_id, client_resource_id),
            )
            conn.commit()

    def iter_snapshots(
        self,
        *,
        client_id: str = "",
        search_upsert_state: str = "",
        limit: int | None = None,
    ):
        sql = "SELECT * FROM processed_resource_snapshot WHERE 1 = 1"
        params: list[Any] = []
        if client_id:
            sql += " AND client_id = ?"
            params.append(client_id)
        if search_upsert_state:
            sql += " AND search_upsert_state = ?"
            params.append(search_upsert_state)
        sql += " ORDER BY updated_at DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        for row in rows:
            data = dict(row)
            data["snapshot"] = json.loads(data.get("snapshot_json") or "{}")
            yield data

    def get(self, *, client_id: str, client_resource_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT * FROM processed_resource_snapshot
                   WHERE client_id = ? AND client_resource_id = ?""",
                (client_id, client_resource_id),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["snapshot"] = json.loads(data.get("snapshot_json") or "{}")
        return data

    def delete(self, *, client_id: str, client_resource_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """DELETE FROM processed_resource_snapshot
                   WHERE client_id = ? AND client_resource_id = ?""",
                (client_id, client_resource_id),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def mark_deleted(
        self,
        *,
        client_id: str,
        client_resource_id: str,
        resource_id: str = "",
        idempotency_key: str = "",
        reason: str = "",
    ) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO processed_resource_delete_marker
                   (client_id, client_resource_id, resource_id, idempotency_key, reason, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(client_id, client_resource_id) DO UPDATE SET
                       resource_id = excluded.resource_id,
                       idempotency_key = excluded.idempotency_key,
                       reason = excluded.reason,
                       updated_at = excluded.updated_at""",
                (client_id, client_resource_id, resource_id, idempotency_key, reason[:1000], now, now),
            )
            conn.commit()

    def is_deleted(self, *, client_id: str, client_resource_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM processed_resource_delete_marker
                   WHERE client_id = ? AND client_resource_id = ?""",
                (client_id, client_resource_id),
            ).fetchone()
        return row is not None

    def clear_delete_marker(self, *, client_id: str, client_resource_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """DELETE FROM processed_resource_delete_marker
                   WHERE client_id = ? AND client_resource_id = ?""",
                (client_id, client_resource_id),
            )
            conn.commit()
            return int(cursor.rowcount or 0)
