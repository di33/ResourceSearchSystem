"""FastAPI application entry-point.

Lifespan hook creates database tables and the Milvus collection on
startup, and closes connections on shutdown.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy import inspect, text

from app.config import settings
from app.deps import close_milvus, close_reranker, engine, get_milvus
from app.models.tables import Base
from app.routers import browse, health, ingest, resources, search
from app.routers.ingest import start_fts_worker, start_vector_sync_worker, stop_fts_worker, stop_vector_sync_worker
from app.services.milvus_search_client import ensure_collection

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_FTS_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "fts_setup.sql"


def _split_sql(raw: str) -> list[str]:
    """Split SQL into individual statements, respecting $$ dollar-quoted blocks."""
    statements: list[str] = []
    buf: list[str] = []
    in_dollar = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        # Track $$ dollar-quoted blocks (PL/pgSQL function bodies)
        i = 0
        while i < len(line) - 1:
            if line[i] == "$" and line[i + 1] == "$":
                in_dollar = not in_dollar
                i += 2
            else:
                i += 1
        buf.append(line)
        if stripped.endswith(";") and not in_dollar:
            stmt = "\n".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
    # Handle trailing statement without semicolon
    if buf:
        stmt = "\n".join(buf).strip()
        if stmt:
            statements.append(stmt)
    return statements


def _ensure_column(sync_conn, table_name: str, column_name: str, ddl: str) -> None:
    if sync_conn.dialect.name == "postgresql":
        # Multiple API/worker processes may initialize the schema concurrently.
        # Keep the DDL atomic so two starters cannot both pass an inspect check.
        sync_conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {ddl}"))
        return

    inspector = inspect(sync_conn)
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name in columns:
        return
    sync_conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl}"))


def _drop_column_if_exists(sync_conn, table_name: str, column_name: str) -> None:
    inspector = inspect(sync_conn)
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name not in columns:
        return
    sync_conn.execute(text(f"ALTER TABLE {table_name} DROP COLUMN {column_name}"))


def _ensure_varchar_capacity(sync_conn, table_name: str, column_name: str, size: int) -> None:
    inspector = inspect(sync_conn)
    columns = {column["name"]: column for column in inspector.get_columns(table_name)}
    column = columns.get(column_name)
    if column is None:
        return

    current_type = str(column.get("type") or "").lower()
    target = f"character varying({size})"
    if current_type == target or current_type == f"varchar({size})":
        return

    if sync_conn.dialect.name == "postgresql":
        sync_conn.execute(text(
            f"ALTER TABLE {table_name} "
            f"ALTER COLUMN {column_name} TYPE VARCHAR({size})"
        ))


def _ensure_additive_schema(sync_conn) -> None:
    """Apply additive compatibility columns for object-reference ingestion."""
    dialect = sync_conn.dialect.name
    text_type = "TEXT"
    string_32 = "VARCHAR(32)"
    string_128 = "VARCHAR(128)"
    not_null_empty = "NOT NULL DEFAULT ''"
    not_null_null_json = "NOT NULL DEFAULT 'null'"

    if dialect not in {"postgresql", "sqlite"}:
        logger.warning("Skipping additive schema checks for unsupported dialect: %s", dialect)
        return

    _ensure_column(sync_conn, "resource_task", "client_metadata_json", f"client_metadata_json {text_type} {not_null_null_json}")
    _ensure_column(sync_conn, "resource_task", "source_storage_profile_id", f"source_storage_profile_id {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_task", "source_object_key", f"source_object_key {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_task", "source_object_file_name", f"source_object_file_name {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_task", "source_object_file_format", f"source_object_file_format {string_32} {not_null_empty}")
    _ensure_column(sync_conn, "resource_task", "source_object_file_size", f"source_object_file_size INTEGER NOT NULL DEFAULT 0")
    _ensure_column(sync_conn, "resource_task", "source_object_checksum", f"source_object_checksum {string_128} {not_null_empty}")
    _ensure_column(sync_conn, "resource_task", "source_object_etag", f"source_object_etag {string_128} {not_null_empty}")
    _ensure_column(sync_conn, "resource_task", "package_storage_profile_id", f"package_storage_profile_id {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_task", "package_object_key", f"package_object_key {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_task", "file_structure_source", "file_structure_source VARCHAR(32) NOT NULL DEFAULT 'processor'")
    _ensure_column(sync_conn, "resource_task", "file_structure_state", "file_structure_state VARCHAR(32) NOT NULL DEFAULT 'complete'")
    _ensure_column(sync_conn, "resource_task", "vector_state", f"vector_state {string_32} {not_null_empty}")
    _ensure_column(sync_conn, "resource_task", "vector_error", f"vector_error {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "vector_sync_job", "embedding_text", f"embedding_text {text_type} {not_null_empty}")
    if dialect == "postgresql":
        _ensure_column(sync_conn, "vector_sync_job", "retry_after", "retry_after TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP")
    else:
        _ensure_column(sync_conn, "vector_sync_job", "retry_after", "retry_after DATETIME")

    _ensure_column(sync_conn, "resource_file", "storage_profile_id", f"storage_profile_id {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_file", "object_key", f"object_key {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_file", "path_in_package", f"path_in_package {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_file", "content_type", f"content_type {string_128} {not_null_empty}")
    _ensure_column(sync_conn, "resource_file", "etag", f"etag {string_128} {not_null_empty}")

    _ensure_column(sync_conn, "resource_preview", "storage_profile_id", f"storage_profile_id {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_preview", "object_key", f"object_key {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_preview", "content_type", f"content_type {string_128} {not_null_empty}")
    _ensure_column(sync_conn, "resource_preview", "origin", f"origin {string_32} {not_null_empty}")
    _ensure_varchar_capacity(sync_conn, "resource_description", "prompt_version", 128)

    try:
        sync_conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_resource_task_source_resource "
            "ON resource_task (source, source_resource_id)"
        ))
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_vector_sync_job_state_id "
            "ON vector_sync_job (state, id)"
        ))
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_vector_sync_job_pending_only_id "
            "ON vector_sync_job (id) WHERE state = 'pending'"
        ))
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_vector_sync_job_failed_retry_after_id "
            "ON vector_sync_job (retry_after, id) WHERE state = 'failed'"
        ))
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_vector_sync_job_running_updated_id "
            "ON vector_sync_job (updated_at, id) WHERE state = 'running'"
        ))
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_vector_sync_job_state_retry_after_id "
            "ON vector_sync_job (state, retry_after, id)"
        ))
        if sync_conn.dialect.name == "postgresql":
            sync_conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_vector_sync_job_pending_id "
                "ON vector_sync_job (id) WHERE state IN ('pending', 'failed')"
            ))
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_resource_embedding_task_id "
            "ON resource_embedding (task_id)"
        ))
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_resource_file_task_id "
            "ON resource_file (task_id)"
        ))
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_resource_preview_task_id "
            "ON resource_preview (task_id)"
        ))
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_resource_description_task_id "
            "ON resource_description (task_id)"
        ))
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_resource_task_browse_updated_id "
            "ON resource_task (updated_at DESC, id DESC)"
        ))
        if sync_conn.dialect.name == "postgresql":
            sync_conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_resource_description_pending_fts "
                "ON resource_description (id) WHERE search_vector IS NULL"
            ))
        sync_conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_process_log_task_id "
            "ON process_log (task_id)"
        ))
    except Exception as exc:
        logger.warning("Could not ensure ingestion helper indexes: %s", exc)


def _drop_obsolete_schema(sync_conn) -> None:
    """Remove legacy columns that conflict with current ingestion writes."""
    dialect = sync_conn.dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        logger.warning("Skipping obsolete schema cleanup for unsupported dialect: %s", dialect)
        return
    _drop_column_if_exists(sync_conn, "resource_task", "resource_path")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    try:
        logger.info("Creating database tables …")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_ensure_additive_schema)
            await conn.run_sync(_drop_obsolete_schema)
        logger.info("Database tables ready.")
    except Exception as exc:
        logger.warning("Database init deferred (will retry on first request): %s", exc)

    if _FTS_SQL_PATH.exists():
        logger.info("Executing FTS setup SQL …")
        fts_sql = _FTS_SQL_PATH.read_text(encoding="utf-8")
        async with engine.begin() as conn:
            for stmt in _split_sql(fts_sql):
                await conn.execute(text(stmt))
        logger.info("FTS setup complete.")
    else:
        raise RuntimeError(f"FTS SQL not found at {_FTS_SQL_PATH} — pg_jieba is required")

    try:
        logger.info("Ensuring Milvus collection …")
        ensure_collection(get_milvus())
    except Exception as exc:
        logger.warning("Milvus init deferred (will retry on first request): %s", exc)

    start_vector_sync_worker()
    start_fts_worker()
    logger.info("Server ready — %s", "DEBUG mode" if settings.debug else "production mode")
    yield

    # --- shutdown ---
    await stop_fts_worker()
    await stop_vector_sync_worker()
    await close_reranker()
    close_milvus()
    await engine.dispose()
    logger.info("Connections closed.")


app = FastAPI(
    title="数字资源语义检索服务",
    description="接收加工后的资源元数据，提供语义检索、预览和下载 URL",
    version="0.1.0",
    lifespan=lifespan,
    debug=settings.debug,
)

allowed_origins = ["*"] if settings.debug else []
if not allowed_origins:
    logger.warning("CORS is restricted — no origins allowed. Set allowed_origins for production.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def root():
    """根路径未挂业务页时浏览器会 404；重定向到 Swagger 文档。"""
    return RedirectResponse(url="/docs")


app.include_router(health.router)
app.include_router(ingest.router)
app.include_router(resources.router)
app.include_router(search.router)
app.include_router(browse.router)
