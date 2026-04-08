"""FastAPI application entry-point.

Lifespan hook creates database tables and the Milvus collection on
startup, and closes connections on shutdown.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.deps import close_milvus, engine, get_milvus
from app.models.tables import Base
from app.routers import browse, health, resources, search
from app.services.milvus_search_client import ensure_collection

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    try:
        logger.info("Creating database tables …")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables ready.")
    except Exception as exc:
        logger.warning("Database init deferred (will retry on first request): %s", exc)

    try:
        logger.info("Ensuring Milvus collection …")
        ensure_collection(get_milvus())
    except Exception as exc:
        logger.warning("Milvus init deferred (will retry on first request): %s", exc)

    logger.info("Server ready — %s", "DEBUG mode" if settings.debug else "production mode")
    yield

    # --- shutdown ---
    close_milvus()
    await engine.dispose()
    logger.info("Connections closed.")


app = FastAPI(
    title="数字资源语义检索服务",
    description="注册、上传、提交、语义检索、下载数字资源",
    version="0.1.0",
    lifespan=lifespan,
    debug=settings.debug,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(resources.router)
app.include_router(search.router)
app.include_router(browse.router)
