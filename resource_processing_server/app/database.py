from __future__ import annotations

from contextlib import contextmanager
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterator


logger = logging.getLogger(__name__)


def _postgres_sql(sql: str) -> str:
    """Translate the small qmark-SQL subset used by the persistence stores."""
    return sql.replace("?", "%s")


class PostgresConnection:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql: str, params=()):
        return self._connection.execute(_postgres_sql(sql), params)

    def executemany(self, sql: str, params_seq):
        cursor = self._connection.cursor()
        cursor.executemany(_postgres_sql(sql), params_seq)
        return cursor

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()


class PostgresDatabase:
    """Shared synchronous connection pool for short persistence operations."""

    def __init__(self, database_url: str, *, min_size: int = 1, max_size: int = 32):
        if not str(database_url or "").strip():
            raise ValueError("RP_DATABASE_URL is required")
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - exercised by container startup
            raise RuntimeError("psycopg and psycopg-pool are required for Postgres persistence") from exc

        self.database_url = database_url
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=max(1, int(min_size)),
            max_size=max(1, int(max_size)),
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self._pool.wait(timeout=60)

    @contextmanager
    def connect(self) -> Iterator[PostgresConnection]:
        with self._pool.connection() as connection:
            yield PostgresConnection(connection)

    def close(self) -> None:
        self._pool.close()

    def ping(self) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT 1 AS ok").fetchone()
        return bool(row and row.get("ok") == 1)


def migrate_legacy_sqlite(
    database: PostgresDatabase,
    sqlite_path: str,
    *,
    batch_size: int = 200,
) -> dict[str, int]:
    """Import snapshots and job history once so old client job IDs remain queryable."""
    path = Path(sqlite_path)
    counts = {"snapshots": 0, "delete_markers": 0, "jobs": 0}
    if not path.is_file():
        return counts

    migration_key = "legacy-sqlite-v1"
    with database.connect() as target:
        target.execute("SELECT pg_advisory_lock(hashtext('resource-processing-sqlite-migration'))")
        try:
            applied = target.execute(
                "SELECT 1 FROM processing_schema_migration WHERE migration_key = ?",
                (migration_key,),
            ).fetchone()
            if applied:
                return counts

            source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=300)
            source.row_factory = sqlite3.Row
            source.execute("PRAGMA query_only=ON")
            try:
                counts["snapshots"] = _copy_rows(
                    source,
                    target,
                    select_sql="SELECT * FROM processed_resource_snapshot ORDER BY id",
                    insert_sql="""
                        INSERT INTO processed_resource_snapshot
                            (client_id, client_resource_id, resource_type, resource_fingerprint,
                             snapshot_json, description_source, preview_source,
                             search_upsert_state, search_resource_id, last_upsert_error,
                             created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (client_id, client_resource_id) DO UPDATE SET
                            resource_type = excluded.resource_type,
                            resource_fingerprint = excluded.resource_fingerprint,
                            snapshot_json = excluded.snapshot_json,
                            description_source = excluded.description_source,
                            preview_source = excluded.preview_source,
                            search_upsert_state = excluded.search_upsert_state,
                            search_resource_id = excluded.search_resource_id,
                            last_upsert_error = excluded.last_upsert_error,
                            updated_at = excluded.updated_at
                    """,
                    columns=(
                        "client_id", "client_resource_id", "resource_type", "resource_fingerprint",
                        "snapshot_json", "description_source", "preview_source",
                        "search_upsert_state", "search_resource_id", "last_upsert_error",
                        "created_at", "updated_at",
                    ),
                    batch_size=batch_size,
                )
                counts["delete_markers"] = _copy_rows(
                    source,
                    target,
                    select_sql="SELECT * FROM processed_resource_delete_marker ORDER BY id",
                    insert_sql="""
                        INSERT INTO processed_resource_delete_marker
                            (client_id, client_resource_id, resource_id, idempotency_key,
                             reason, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (client_id, client_resource_id) DO UPDATE SET
                            resource_id = excluded.resource_id,
                            idempotency_key = excluded.idempotency_key,
                            reason = excluded.reason,
                            updated_at = excluded.updated_at
                    """,
                    columns=(
                        "client_id", "client_resource_id", "resource_id", "idempotency_key",
                        "reason", "created_at", "updated_at",
                    ),
                    batch_size=batch_size,
                )
                counts["jobs"] = _copy_legacy_jobs(source, target, batch_size=batch_size)
            finally:
                source.close()

            target.execute(
                "INSERT INTO processing_schema_migration (migration_key) VALUES (?)",
                (migration_key,),
            )
            target.commit()
        finally:
            target.rollback()
            target.execute("SELECT pg_advisory_unlock(hashtext('resource-processing-sqlite-migration'))")
            target.commit()

    logger.info("Migrated legacy SQLite state from %s: %s", path, counts)
    return counts


def _copy_rows(
    source: sqlite3.Connection,
    target: PostgresConnection,
    *,
    select_sql: str,
    insert_sql: str,
    columns: tuple[str, ...],
    batch_size: int,
) -> int:
    try:
        cursor = source.execute(select_sql)
    except sqlite3.OperationalError:
        return 0
    total = 0
    while True:
        rows = cursor.fetchmany(max(1, batch_size))
        if not rows:
            break
        target.executemany(insert_sql, [tuple(row[column] for column in columns) for row in rows])
        target.commit()
        total += len(rows)
        if total % 5000 == 0:
            logger.info("Migrated %s rows from legacy SQLite", total)
    return total


def _copy_legacy_jobs(
    source: sqlite3.Connection,
    target: PostgresConnection,
    *,
    batch_size: int,
) -> int:
    try:
        cursor = source.execute("SELECT * FROM processing_job ORDER BY created_at, job_id")
    except sqlite3.OperationalError:
        return 0

    total = 0
    while True:
        rows = cursor.fetchmany(max(1, batch_size))
        if not rows:
            break
        values = []
        for row in rows:
            values.append((
                row["job_id"], row["client_id"], row["client_resource_id"], row["state"],
                row["manifest_json"], f"legacy:{row['job_id']}", row["batch_id"],
                row["search_resource_id"], row["steps_json"], row["error"],
                row["created_at"], row["updated_at"],
            ))
        target.executemany(
            """INSERT INTO processing_job
                   (job_id, client_id, client_resource_id, state, manifest_json,
                    manifest_fingerprint, batch_id, search_resource_id, steps_json,
                    error, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (job_id) DO NOTHING""",
            values,
        )
        target.commit()
        total += len(rows)
    return total
