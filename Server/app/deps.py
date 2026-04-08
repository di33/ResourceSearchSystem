"""Dependency injection — DB session, Milvus, KS3 clients.

All heavy connections are initialised once during app lifespan and torn
down on shutdown.  FastAPI ``Depends`` callables pull from the shared
state stored on ``app.state``.
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

import boto3
from pymilvus import MilvusClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Async SQLAlchemy engine (created once, reused across workers)
# ---------------------------------------------------------------------------

engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_min,
    max_overflow=settings.db_pool_max - settings.db_pool_min,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Milvus client (thread-safe singleton)
# ---------------------------------------------------------------------------

_milvus_client: MilvusClient | None = None


def get_milvus() -> MilvusClient:
    global _milvus_client
    if _milvus_client is None:
        uri = f"http://{settings.milvus_host}:{settings.milvus_port}"
        _milvus_client = MilvusClient(uri=uri, timeout=5)
        logger.info("Milvus client connected to %s", uri)
    return _milvus_client


def close_milvus() -> None:
    global _milvus_client
    if _milvus_client is not None:
        _milvus_client.close()
        _milvus_client = None


# ---------------------------------------------------------------------------
# S3-compatible KS3 / MinIO client
# ---------------------------------------------------------------------------

_s3_client = None


def get_s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            endpoint_url=settings.ks3_endpoint,
            aws_access_key_id=settings.ks3_access_key,
            aws_secret_access_key=settings.ks3_secret_key,
            region_name=settings.ks3_region,
        )
        logger.info("S3 client connected to %s", settings.ks3_endpoint)
    return _s3_client
