from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENV_FILES = (
    _PROJECT_ROOT / ".env",
    _PROJECT_ROOT / ".env.local",
)


def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    return env


def _load_env_files_into_environ(paths: tuple[Path, ...]) -> None:
    protected_keys = set(os.environ)
    merged: dict[str, str] = {}
    for path in paths:
        merged.update(_load_dotenv(path))
    for key, value in merged.items():
        if value and key not in protected_keys:
            os.environ[key] = value


_load_env_files_into_environ(_ENV_FILES)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=tuple(str(path) for path in _ENV_FILES),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://resource:resource@localhost:5432/resource_upload"
    # Keep enough warm backends for upload and vector workers. New Postgres
    # backends pay a noticeable one-time pg_jieba dictionary load cost.
    db_pool_min: int = 48
    db_pool_max: int = 64

    # Milvus
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_collection: str = "resource_embeddings"

    # JWT
    jwt_secret: str = "dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # API Keys — comma-separated keys for Agent access (search endpoints).
    # ``api_keys`` is kept as a read-key compatibility fallback.
    api_keys: str = ""
    search_read_api_keys: str = Field(default="", alias="SEARCH_READ_API_KEYS")
    search_ingest_api_keys: str = Field(default="", alias="SEARCH_INGEST_API_KEYS")

    # Server
    workers: int = multiprocessing.cpu_count() * 2 + 1
    debug: bool = False

    # Embedding — 用于 commit 生成向量 + search 时 query 向量化
    # .env 中以 SERVER_EMBEDDING_* 命名
    embedding_provider: str = Field(default="ksyun", alias="SERVER_EMBEDDING_PROVIDER")
    embedding_model: str = Field(default="embedding-3", alias="SERVER_EMBEDDING_MODEL")
    embedding_dimension: int = Field(default=1024, alias="SERVER_EMBEDDING_DIMENSION")
    embedding_base_url: str = Field(default="https://kspmas.ksyun.com/v1", alias="SERVER_EMBEDDING_BASE_URL")
    embedding_timeout_seconds: float = Field(default=120.0, alias="SERVER_EMBEDDING_TIMEOUT_SECONDS")
    kspmas_api_key: str = Field(default="", alias="KSPMAS_API_KEY")
    ksc_api_key: str = Field(default="", alias="KSC_API_KEY")
    vector_sync_worker_enabled: bool = Field(default=True, alias="VECTOR_SYNC_WORKER_ENABLED")
    vector_sync_worker_interval: float = Field(default=0.2, alias="VECTOR_SYNC_WORKER_INTERVAL")
    vector_sync_worker_batch_size: int = Field(default=16, alias="VECTOR_SYNC_WORKER_BATCH_SIZE")
    vector_sync_worker_concurrency: int = Field(default=8, alias="VECTOR_SYNC_WORKER_CONCURRENCY")
    vector_sync_worker_stale_seconds: int = Field(default=600, alias="VECTOR_SYNC_WORKER_STALE_SECONDS")
    vector_sync_failed_retry_seconds: int = Field(default=60, alias="VECTOR_SYNC_FAILED_RETRY_SECONDS")

    # BM25 / FTS
    bm25_default_weight: float = 0.5
    search_text_config: str = "jiebacfg"  # PostgreSQL text search config name
    fts_worker_enabled: bool = Field(default=True, alias="FTS_WORKER_ENABLED")
    fts_worker_interval: float = Field(default=1.0, alias="FTS_WORKER_INTERVAL")
    fts_worker_batch_size: int = Field(default=200, alias="FTS_WORKER_BATCH_SIZE")

    # Reranker
    reranker_enabled: bool = True
    reranker_url: str = "http://reranker:8100"
    reranker_timeout: float = 3.0
    reranker_weight: float = 0.3

    @field_validator("debug", mode="before")
    @classmethod
    def _parse_debug(cls, value):
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"release", "prod", "production"}:
                return False
            if normalized in {"debug", "dev", "development"}:
                return True
        return value


settings = Settings()
