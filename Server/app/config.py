from __future__ import annotations

import multiprocessing
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
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
    ks3_access_key: str = "minioadmin"
    ks3_secret_key: str = "minioadmin"
    ks3_bucket: str = "resources"
    ks3_region: str = "cn-beijing-6"
    ks3_presign_expires: int = 3600

    # JWT
    jwt_secret: str = "dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # Server
    workers: int = multiprocessing.cpu_count() * 2 + 1
    debug: bool = False

    # Embedding — 用于 commit 生成向量 + search 时 query 向量化
    # .env 中以 SERVER_EMBEDDING_* 命名
    embedding_provider: str = Field(default="dashscope", alias="SERVER_EMBEDDING_PROVIDER")
    embedding_model: str = Field(default="text-embedding-v3", alias="SERVER_EMBEDDING_MODEL")
    embedding_dimension: int = Field(default=1024, alias="SERVER_EMBEDDING_DIMENSION")


settings = Settings()
