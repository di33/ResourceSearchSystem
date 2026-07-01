from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
from typing import Optional

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
    db_pool_min: int = 10
    db_pool_max: int = 50

    # Milvus
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_collection: str = "resource_embeddings"

    # KS3 / MinIO (S3-compatible)
    ks3_endpoint: str = "http://localhost:9000"
    ks3_public_endpoint: Optional[str] = None
    ks3_cdn_endpoint: Optional[str] = None
    ks3_access_key: str = "minioadmin"
    ks3_secret_key: str = "minioadmin"
    ks3_bucket: str = "resources"
    ks3_region: str = "cn-beijing-6"
    ks3_presign_expires: int = 3600
    ks3_signature_version: str = "s3v4"
    ks3_addressing_style: str = "auto"
    ks3_cdn_auth_enabled: bool = False
    ks3_cdn_auth_type: str = "A"
    ks3_cdn_auth_sign_param: str = "sign"
    ks3_cdn_auth_expires: int = 86400
    ks3_cdn_auth_uid: str = "0"
    ks3_cdn_auth_rand: str = "0"
    ks3_cdn_auth_key_primary: str = ""
    ks3_cdn_auth_key_secondary: str = ""

    # JWT
    jwt_secret: str = "dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # API Keys — comma-separated keys for Agent access (search endpoints)
    api_keys: str = ""

    # Server
    workers: int = multiprocessing.cpu_count() * 2 + 1
    debug: bool = False

    # Embedding — 用于 commit 生成向量 + search 时 query 向量化
    # .env 中以 SERVER_EMBEDDING_* 命名
    embedding_provider: str = Field(default="ksyun", alias="SERVER_EMBEDDING_PROVIDER")
    embedding_model: str = Field(default="embedding-3", alias="SERVER_EMBEDDING_MODEL")
    embedding_dimension: int = Field(default=1024, alias="SERVER_EMBEDDING_DIMENSION")
    embedding_base_url: str = Field(default="https://kspmas.ksyun.com/v1", alias="SERVER_EMBEDDING_BASE_URL")
    kspmas_api_key: str = Field(default="", alias="KSPMAS_API_KEY")
    ksc_api_key: str = Field(default="", alias="KSC_API_KEY")

    # BM25 / FTS
    bm25_default_weight: float = 0.5
    search_text_config: str = "jiebacfg"  # PostgreSQL text search config name

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
