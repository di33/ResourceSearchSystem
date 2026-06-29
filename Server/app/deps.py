"""Dependency injection — DB session, Milvus, KS3 clients.

All heavy connections are initialised once during app lifespan and torn
down on shutdown.  FastAPI ``Depends`` callables pull from the shared
state stored on ``app.state``.
"""

from __future__ import annotations

import logging
import time
from typing import AsyncGenerator

import boto3
from pymilvus import MilvusClient
from pymilvus.exceptions import MilvusException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.services.ks3_storage import build_s3_client_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Reranker client — lazy singleton
# ---------------------------------------------------------------------------

_reranker = None


def get_reranker():
    """Return a shared RerankerClient instance (created on first call)."""
    global _reranker
    if _reranker is None:
        from app.services.reranker_client import RerankerClient
        _reranker = RerankerClient()
    return _reranker


async def close_reranker() -> None:
    global _reranker
    if _reranker is not None:
        await _reranker.close()
        _reranker = None


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
# Milvus client — proxy with automatic reconnection
# ---------------------------------------------------------------------------

_CONNECTION_ERRORS = ("closed", "unavailable", "failed to connect", "connection refused")


def _is_connection_error(exc: BaseException) -> bool:
    """Check whether the exception indicates a dead / unreachable gRPC channel."""
    msg = str(exc).lower()
    return any(keyword in msg for keyword in _CONNECTION_ERRORS)


def _connect_milvus(max_retries: int = 6, retry_delay: float = 5.0) -> MilvusClient:
    uri = f"http://{settings.milvus_host}:{settings.milvus_port}"
    last_exc: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        try:
            client = MilvusClient(uri=uri, timeout=5)
            logger.info("Milvus client connected to %s", uri)
            return client
        except MilvusException as exc:
            last_exc = exc
            if attempt < max_retries:
                logger.warning(
                    "Milvus not ready (attempt %d/%d) — retrying in %.0fs …",
                    attempt,
                    max_retries,
                    retry_delay,
                )
                time.sleep(retry_delay)
    raise last_exc  # type: ignore[misc]


class _ResilientMilvusClient:
    """Proxy around ``MilvusClient`` that auto-reconnects on gRPC failures.

    Any callable attribute (``search``, ``insert``, ``has_collection``, …)
    is transparently wrapped: if the call raises a ``MilvusException`` that
    looks like a connection error, the proxy closes the dead client, creates
    a fresh one, and retries the call **once**.
    """

    def __init__(self) -> None:
        self._client: MilvusClient = _connect_milvus()

    # -- public helpers -------------------------------------------------------

    @property
    def raw(self) -> MilvusClient:
        """Access the underlying ``MilvusClient`` (e.g. for shutdown)."""
        return self._client

    def reconnect(self) -> None:
        """Force a fresh connection."""
        try:
            self._client.close()
        except Exception:
            pass
        self._client = _connect_milvus()

    # -- proxy machinery ------------------------------------------------------

    def __getattr__(self, name: str):
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def _wrapper(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            except MilvusException as exc:
                if not _is_connection_error(exc):
                    raise
                logger.warning(
                    "Milvus %s failed (%s) — reconnecting and retrying …",
                    name,
                    exc,
                )
                self.reconnect()
                return getattr(self._client, name)(*args, **kwargs)

        return _wrapper


_milvus: _ResilientMilvusClient | None = None


def get_milvus() -> MilvusClient:
    """Return a Milvus client that transparently reconnects on failure.

    The return type is nominally ``MilvusClient`` for type-checker happiness;
    at runtime it is a ``_ResilientMilvusClient`` proxy that delegates all
    attribute access to the real client.
    """
    global _milvus
    if _milvus is None:
        _milvus = _ResilientMilvusClient()
    return _milvus  # type: ignore[return-value]


def close_milvus() -> None:
    global _milvus
    if _milvus is not None:
        try:
            _milvus.raw.close()
        except Exception:
            pass
        _milvus = None


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
            config=build_s3_client_config(),
        )
        logger.info("S3 client connected to %s", settings.ks3_endpoint)
    return _s3_client
