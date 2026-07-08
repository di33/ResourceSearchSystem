from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from resource_contracts.object_urls import generate_cdn_download_url

from .storage_profiles import StorageProfile, load_storage_profiles


_COS_FORBIDDEN_PERCENT_RE = re.compile(r"%(0[aAdD])")
_COS_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def default_bucket(bucket: str | None = None) -> str:
    return bucket or load_storage_profiles().default().bucket


@dataclass
class StorageObjectRef:
    storage_profile_id: str
    object_key: str
    file_name: str
    file_format: str
    size: int
    checksum: str
    etag: str = ""
    is_primary: bool = False

    def to_manifest_dict(self) -> dict[str, Any]:
        return asdict(self)


def file_md5(path: str | Path) -> str:
    hasher = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def safe_object_part(value: str) -> str:
    text = safe_object_path_part(value).strip(" .")
    return text if text not in {"", ".", ".."} else "resource"


def safe_object_path_part(value: str) -> str:
    text = str(value or "")
    if text in {"", ".", ".."}:
        return "_"
    text = text.replace("/", "_").replace("\\", "_")
    text = _COS_CONTROL_RE.sub("_", text)
    text = _COS_FORBIDDEN_PERCENT_RE.sub(r"_\1", text)
    return text if text not in {"", ".", ".."} else "_"


class ObjectStorageUploader:
    """S3-compatible uploader for client-side object storage writes.

    Stable bucket/endpoint/region/CDN settings come from storage profiles.
    Secrets are resolved through the env refs declared by the selected profile.
    """

    def __init__(
        self,
        *,
        storage_profile_id: str | None = None,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
        signature_version: str | None = None,
        addressing_style: str | None = None,
    ):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError("object storage upload requires boto3; install Tools/requirements.txt") from exc

        self.profile: StorageProfile = load_storage_profiles().get(storage_profile_id)
        self.bucket = self.profile.bucket
        if not self.bucket:
            raise ValueError("storage profile bucket is required")

        resolved_endpoint = endpoint or self.profile.endpoint
        resolved_access_key = access_key or self.profile.access_key
        resolved_secret_key = secret_key or self.profile.secret_key
        if not resolved_endpoint:
            raise ValueError("storage profile endpoint is required")
        if not resolved_access_key or not resolved_secret_key:
            raise ValueError("storage profile access_key/secret_key is required")

        self.client = boto3.client(
            "s3",
            endpoint_url=resolved_endpoint,
            aws_access_key_id=resolved_access_key,
            aws_secret_access_key=resolved_secret_key,
            region_name=region or self.profile.region,
            config=Config(
                signature_version=signature_version
                or self.profile.signature_version
                or "s3v4",
                s3={
                    "addressing_style": addressing_style
                    or self.profile.addressing_style
                    or "auto"
                },
            ),
        )

    def upload_file(
        self,
        file_path: str | Path,
        *,
        object_key: str,
        is_primary: bool = False,
        content_type: str = "",
    ) -> StorageObjectRef:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        resolved_bucket = self.bucket
        if not resolved_bucket:
            raise ValueError("object storage bucket is required")
        object_key = self.profile.validate_object_key(object_key)
        detected_type = content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        checksum = file_md5(path)
        size = path.stat().st_size
        with open(path, "rb") as handle:
            self.client.put_object(
                Bucket=resolved_bucket,
                Key=object_key,
                Body=handle,
                ContentLength=size,
                ContentType=detected_type,
            )
        head = self.client.head_object(Bucket=resolved_bucket, Key=object_key)
        suffix = path.suffix.lower().lstrip(".")
        return StorageObjectRef(
            storage_profile_id=self.profile.profile_id,
            object_key=object_key,
            file_name=path.name,
            file_format=suffix,
            size=int(head.get("ContentLength", size)),
            checksum=checksum,
            etag=str(head.get("ETag", "")).strip('"'),
            is_primary=is_primary,
        )

    def generate_download_url(self, object_key: str, *, expires: int = 900) -> str:
        key = self.profile.validate_object_key(object_key)
        url_mode = (self.profile.url_mode or "s3_presign").strip().lower()
        if url_mode in {"cdn_unsigned", "cdn_type_a"}:
            return generate_cdn_download_url(self.profile, key, expires=expires)
        if url_mode in {"s3_presign", "presign", "s3"}:
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=int(expires),
            )
        raise RuntimeError(f"unsupported read URL mode for storage profile {self.profile.profile_id}: {url_mode}")

    def delete_objects(self, object_keys: list[str]) -> int:
        deleted = 0
        for start in range(0, len(object_keys), 1000):
            chunk = [
                {"Key": self.profile.validate_object_key(key)}
                for key in object_keys[start:start + 1000]
                if str(key or "").strip()
            ]
            if not chunk:
                continue
            try:
                self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": chunk, "Quiet": True},
                )
            except Exception as exc:
                if not _is_missing_content_md5_error(exc):
                    raise
                for item in chunk:
                    self.client.delete_object(Bucket=self.bucket, Key=item["Key"])
            deleted += len(chunk)
        return deleted


def _is_missing_content_md5_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    code = str(error.get("Code") or "")
    message = str(error.get("Message") or exc)
    return code == "InvalidRequest" and "Content-MD5" in message
