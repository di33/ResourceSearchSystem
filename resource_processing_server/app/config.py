from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_SERVICE_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILES = (
    _SERVICE_ROOT / ".env",
    _SERVICE_ROOT / ".env.local",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=tuple(str(path) for path in _ENV_FILES),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    debug: bool = False

    # Client API auth. RP_CLIENT_API_KEYS format:
    # client-a:key1|key2,client-b:key3
    client_api_keys: str = Field(default="", alias="RP_CLIENT_API_KEYS")
    admin_api_keys: str = Field(default="", alias="RP_ADMIN_API_KEYS")

    # Search server API.
    search_server_url: str = Field(default="http://localhost:8000", alias="RP_SEARCH_SERVER_URL")
    search_server_api_key: str = Field(default="", alias="RP_SEARCH_SERVER_API_KEY")
    search_server_bearer_token: str = Field(default="", alias="RP_SEARCH_SERVER_BEARER_TOKEN")
    search_server_timeout: float = Field(default=60.0, alias="RP_SEARCH_SERVER_TIMEOUT")

    # Object storage. Stable bucket/endpoint/region settings come from
    # resource_processing_server/storage_profiles.jsonc.
    allowed_object_prefixes: str = Field(default="", alias="RP_ALLOWED_OBJECT_PREFIXES")
    validate_object_exists: bool = Field(default=True, alias="RP_VALIDATE_OBJECT_EXISTS")

    # Generated assets.
    generated_preview_profile_id: str = Field(default="", alias="RP_GENERATED_PREVIEW_PROFILE_ID")
    generated_preview_prefix: str = Field(default="", alias="RP_GENERATED_PREVIEW_PREFIX")

    # Local work area.
    work_dir: str = Field(default=str(_REPO_ROOT / "data" / "resource_processing_server"), alias="RP_WORK_DIR")
    keep_work_dir: bool = Field(default=False, alias="RP_KEEP_WORK_DIR")
    snapshot_db_path: str = Field(
        default=str(_REPO_ROOT / "data" / "resource_processing_server" / "snapshots.db"),
        alias="RP_SNAPSHOT_DB_PATH",
    )
    database_url: str = Field(default="", alias="RP_DATABASE_URL")
    database_pool_min_size: int = Field(default=2, alias="RP_DATABASE_POOL_MIN_SIZE")
    database_pool_max_size: int = Field(default=12, alias="RP_DATABASE_POOL_MAX_SIZE")
    job_worker_concurrency: int = Field(default=4, alias="RP_JOB_WORKER_CONCURRENCY")
    job_worker_idle_seconds: float = Field(default=0.2, alias="RP_JOB_WORKER_IDLE_SECONDS")
    migrate_legacy_sqlite: bool = Field(default=True, alias="RP_MIGRATE_LEGACY_SQLITE")
    preview_max_size: int = Field(default=512, alias="RP_PREVIEW_MAX_SIZE")
    preview_renderer_url: str = Field(default="", alias="RP_PREVIEW_RENDERER_URL")
    preview_renderer_api_key: str = Field(default="", alias="RP_PREVIEW_RENDERER_API_KEY")
    preview_renderer_timeout: float = Field(default=300.0, alias="RP_PREVIEW_RENDERER_TIMEOUT")
    preview_renderer_keep_work_dir: bool = Field(default=False, alias="PR_KEEP_WORK_DIR")
    preview_renderer_url_expires: int = Field(default=900, alias="RP_PREVIEW_RENDERER_URL_EXPIRES")
    max_zip_members: int = Field(default=512, alias="RP_MAX_ZIP_MEMBERS")
    max_zip_member_bytes: int = Field(default=256 * 1024 * 1024, alias="RP_MAX_ZIP_MEMBER_BYTES")
    max_zip_extract_bytes: int = Field(default=1024 * 1024 * 1024, alias="RP_MAX_ZIP_EXTRACT_BYTES")
    max_zip_compression_ratio: float = Field(default=100.0, alias="RP_MAX_ZIP_COMPRESSION_RATIO")

    # Shared ResourceProcessor path.
    shared_resource_processor_path: str = Field(
        default=str(_REPO_ROOT / "Tools"),
        alias="RP_SHARED_RESOURCE_PROCESSOR_PATH",
    )

    # Description generation. Reuses existing ResourceProcessor providers.
    llm_provider: str = Field(default="mock", alias="RP_LLM_PROVIDER")
    llm_model: str = Field(default="", alias="RP_LLM_MODEL")
    pipeline_version: str = Field(default="resource-processing-server-v1", alias="RP_PIPELINE_VERSION")
    process_inline: bool = Field(default=False, alias="RP_PROCESS_INLINE")
    allow_resource_id_delete: bool = Field(default=False, alias="RP_ALLOW_RESOURCE_ID_DELETE")
    replay_failed_snapshots_on_startup: bool = Field(default=True, alias="RP_REPLAY_FAILED_SNAPSHOTS_ON_STARTUP")
    replay_failed_snapshots_startup_limit: int = Field(default=1000, alias="RP_REPLAY_FAILED_SNAPSHOTS_STARTUP_LIMIT")
    description_batch_enabled: bool = Field(default=True, alias="RP_DESCRIPTION_BATCH_ENABLED")
    description_batch_min_size: int = Field(default=20, alias="RP_DESCRIPTION_BATCH_MIN_SIZE")
    description_batch_max_size: int = Field(default=200, alias="RP_DESCRIPTION_BATCH_MAX_SIZE")
    description_batch_max_wait_seconds: float = Field(default=1.0, alias="RP_DESCRIPTION_BATCH_MAX_WAIT_SECONDS")

    @field_validator(
        "debug",
        "keep_work_dir",
        "validate_object_exists",
        "process_inline",
        "allow_resource_id_delete",
        "replay_failed_snapshots_on_startup",
        "description_batch_enabled",
        "preview_renderer_keep_work_dir",
        "migrate_legacy_sqlite",
        mode="before",
    )
    @classmethod
    def _parse_bool(cls, value):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "debug", "dev"}
        return value


def csv_items(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


settings = Settings()

os.makedirs(settings.work_dir, exist_ok=True)
os.makedirs(str(Path(settings.snapshot_db_path).parent), exist_ok=True)
