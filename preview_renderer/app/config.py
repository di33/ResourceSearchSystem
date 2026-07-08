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
    client_api_keys: str = Field(default="", alias="PR_CLIENT_API_KEYS")
    admin_api_keys: str = Field(default="", alias="PR_ADMIN_API_KEYS")
    work_dir: str = Field(default=str(_REPO_ROOT / "data" / "preview_renderer"), alias="PR_WORK_DIR")
    keep_work_dir: bool = Field(default=False, alias="PR_KEEP_WORK_DIR")
    preview_max_size: int = Field(default=512, alias="PR_PREVIEW_MAX_SIZE")
    shared_resource_processor_path: str = Field(
        default=str(_REPO_ROOT / "Tools"),
        alias="PR_SHARED_RESOURCE_PROCESSOR_PATH",
    )
    max_download_bytes: int = Field(default=2 * 1024 * 1024 * 1024, alias="PR_MAX_DOWNLOAD_BYTES")
    max_zip_members: int = Field(default=512, alias="PR_MAX_ZIP_MEMBERS")
    max_zip_member_bytes: int = Field(default=256 * 1024 * 1024, alias="PR_MAX_ZIP_MEMBER_BYTES")
    max_zip_extract_bytes: int = Field(default=1024 * 1024 * 1024, alias="PR_MAX_ZIP_EXTRACT_BYTES")
    max_zip_compression_ratio: float = Field(default=100.0, alias="PR_MAX_ZIP_COMPRESSION_RATIO")
    allowed_source_url_hosts: str = Field(default="", alias="PR_ALLOWED_SOURCE_URL_HOSTS")
    allow_private_source_url_hosts: bool = Field(default=False, alias="PR_ALLOW_PRIVATE_SOURCE_URL_HOSTS")

    @field_validator("debug", "keep_work_dir", "allow_private_source_url_hosts", mode="before")
    @classmethod
    def _parse_bool(cls, value):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "debug", "dev"}
        return value


settings = Settings()

os.makedirs(settings.work_dir, exist_ok=True)
