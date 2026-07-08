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
from app.services.milvus_search_client import ensure_collection

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
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
    inspector = inspect(sync_conn)
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name in columns:
        return
    sync_conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {ddl}"))


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
    _ensure_column(sync_conn, "resource_task", "vector_state", f"vector_state {string_32} {not_null_empty}")
    _ensure_column(sync_conn, "resource_task", "vector_error", f"vector_error {text_type} {not_null_empty}")

    _ensure_column(sync_conn, "resource_file", "storage_profile_id", f"storage_profile_id {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_file", "object_key", f"object_key {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_file", "path_in_package", f"path_in_package {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_file", "content_type", f"content_type {string_128} {not_null_empty}")
    _ensure_column(sync_conn, "resource_file", "etag", f"etag {string_128} {not_null_empty}")

    _ensure_column(sync_conn, "resource_preview", "storage_profile_id", f"storage_profile_id {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_preview", "object_key", f"object_key {text_type} {not_null_empty}")
    _ensure_column(sync_conn, "resource_preview", "content_type", f"content_type {string_128} {not_null_empty}")
    _ensure_column(sync_conn, "resource_preview", "origin", f"origin {string_32} {not_null_empty}")

    try:
        sync_conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_resource_task_source_resource "
            "ON resource_task (source, source_resource_id)"
        ))
    except Exception as exc:
        logger.warning("Could not ensure resource identity unique index: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    try:
        logger.info("Creating database tables …")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(_ensure_additive_schema)
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

    logger.info("Server ready — %s", "DEBUG mode" if settings.debug else "production mode")
    yield

    # --- shutdown ---
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
