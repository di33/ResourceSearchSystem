from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from resource_contracts.object_urls import generate_cdn_download_url
from resource_contracts.path_safety import safe_file_name, safe_join_under
from resource_processing_server.ObjectStorageUpload.storage_profiles import load_storage_profiles
from resource_processing_server.app.config import csv_items, settings
from resource_processing_server.app.models import ObjectRef, PreviewRef

_TOOLS_ROOT = Path(settings.shared_resource_processor_path).resolve()
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))


class ObjectValidationError(ValueError):
    pass


_COS_FORBIDDEN_PERCENT_RE = re.compile(r"%(0[aAdD])")
_COS_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _has_unsupported_cos_key_chars(key: str) -> bool:
    return bool(_COS_CONTROL_RE.search(key) or _COS_FORBIDDEN_PERCENT_RE.search(key))


def _safe_key_part(value: str) -> str:
    text = str(value or "")
    if text in {"", ".", ".."}:
        return "resource"
    text = text.replace("/", "_").replace("\\", "_")
    text = _COS_CONTROL_RE.sub("_", text)
    text = _COS_FORBIDDEN_PERCENT_RE.sub(r"_\1", text)
    text = text.strip(" .")
    return text if text not in {"", ".", ".."} else "resource"


def preview_object_name(local_path: str | Path, *, use_primary: bool, gallery_index: int) -> str:
    suffix = Path(local_path).suffix.lower() or ".webp"
    base = "primary" if use_primary else f"gallery-{gallery_index:03d}"
    return f"{base}{suffix}"


class ObjectStorage:
    def __init__(self):
        self.allowed_prefixes = csv_items(settings.allowed_object_prefixes)
        self.profiles = load_storage_profiles()
        self.clients = {}

    def _profile(self, profile_id: str = ""):
        return self.profiles.get(profile_id or None)

    def _client(self, profile_id: str = ""):
        profile = self._profile(profile_id)
        client = self.clients.get(profile.profile_id)
        if client is not None:
            return client
        if not profile.endpoint:
            raise ObjectValidationError(f"storage profile endpoint is required: {profile.profile_id}")
        if not profile.access_key or not profile.secret_key:
            raise ObjectValidationError(f"storage profile access_key/secret_key is required: {profile.profile_id}")
        client = boto3.client(
            "s3",
            endpoint_url=profile.endpoint,
            aws_access_key_id=profile.access_key,
            aws_secret_access_key=profile.secret_key,
            region_name=profile.region,
            config=Config(
                signature_version=profile.signature_version or "s3v4",
                s3={"addressing_style": profile.addressing_style or "auto"},
            ),
        )
        self.clients[profile.profile_id] = client
        return client

    def validate_ref(self, ref: ObjectRef | PreviewRef) -> None:
        profile = self._profile(ref.storage_profile_id)
        try:
            key = profile.validate_object_key(ref.object_key)
        except ValueError as exc:
            raise ObjectValidationError(str(exc)) from exc
        if _has_unsupported_cos_key_chars(key):
            raise ObjectValidationError(f"invalid object_key: {ref.object_key}")
        if self.allowed_prefixes and not any(key.startswith(prefix) for prefix in self.allowed_prefixes):
            raise ObjectValidationError(f"object_key prefix is not allowed: {ref.object_key}")
        if settings.validate_object_exists:
            self.head(ref.storage_profile_id, ref.object_key)

    def head(self, storage_profile_id: str, object_key: str) -> dict:
        profile = self._profile(storage_profile_id)
        try:
            return self._client(profile.profile_id).head_object(Bucket=profile.bucket, Key=object_key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise ObjectValidationError(f"object not found: {profile.profile_id}/{object_key}") from exc
            raise

    def download_ref(self, ref: ObjectRef | PreviewRef, target_dir: Path, filename: str = "") -> Path:
        self.validate_ref(ref)
        profile = self._profile(ref.storage_profile_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        name = safe_file_name(filename or getattr(ref, "file_name", "") or Path(ref.object_key).name, "object")
        target = safe_join_under(target_dir, name, fallback="object")
        self._client(profile.profile_id).download_file(profile.bucket, ref.object_key, str(target))
        return target

    def generate_read_url(self, ref: ObjectRef | PreviewRef, *, expires: int | None = None) -> str:
        self.validate_ref(ref)
        profile = self._profile(ref.storage_profile_id)
        key = profile.validate_object_key(ref.object_key)
        url_mode = (profile.url_mode or "s3_presign").strip().lower()
        if url_mode in {"cdn_unsigned", "cdn_type_a"}:
            return generate_cdn_download_url(profile, key, expires=expires)
        if url_mode in {"s3_presign", "presign", "s3"}:
            return self._client(profile.profile_id).generate_presigned_url(
                "get_object",
                Params={"Bucket": profile.bucket, "Key": key},
                ExpiresIn=int(expires or settings.preview_renderer_url_expires),
            )
        raise RuntimeError(f"unsupported read URL mode for storage profile {profile.profile_id}: {url_mode}")

    def upload_preview(
        self,
        local_path: str,
        *,
        client_id: str,
        client_resource_id: str,
        storage_profile_id: str = "",
        preview_name: str = "",
        role: str = "primary",
        renderer: str = "resource-processing-server",
    ) -> PreviewRef:
        source = Path(local_path)
        profile = self._profile(settings.generated_preview_profile_id or storage_profile_id)
        if not profile.bucket:
            raise ObjectValidationError("generated preview profile bucket is not configured")
        prefix = settings.generated_preview_prefix.strip("/")
        if not prefix and profile.allowed_prefixes:
            prefix = profile.allowed_prefixes[0].strip("/")
        name = _safe_key_part(preview_name or source.name)
        key = "/".join(
            part for part in (
                prefix,
                _safe_key_part(client_id),
                "previews",
                _safe_key_part(client_resource_id),
                name,
            )
            if part
        )
        self._client(profile.profile_id).upload_file(
            str(source),
            profile.bucket,
            key,
        )
        stat = source.stat()
        return PreviewRef(
            role=role,
            storage_profile_id=profile.profile_id,
            object_key=key,
            size=stat.st_size,
            strategy="static",
            origin="generated",
            renderer=renderer,
        )

    def delete_refs(self, refs: list[dict[str, Any]]) -> int:
        grouped: dict[str, list[str]] = {}
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            profile_id = str(ref.get("storage_profile_id") or "")
            key = str(ref.get("object_key") or "").strip()
            if not key:
                continue
            profile = self._profile(profile_id)
            try:
                key = profile.validate_object_key(key)
            except ValueError as exc:
                raise ObjectValidationError(str(exc)) from exc
            if _has_unsupported_cos_key_chars(key):
                raise ObjectValidationError(f"invalid object_key: {key}")
            if self.allowed_prefixes and not any(key.startswith(prefix) for prefix in self.allowed_prefixes):
                raise ObjectValidationError(f"object_key prefix is not allowed: {key}")
            dedupe_key = (profile.profile_id, key)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            grouped.setdefault(profile.profile_id, []).append(key)

        deleted = 0
        for profile_id, keys in grouped.items():
            profile = self._profile(profile_id)
            client = self._client(profile.profile_id)
            for start in range(0, len(keys), 1000):
                chunk = [{"Key": key} for key in keys[start:start + 1000]]
                if not chunk:
                    continue
                try:
                    client.delete_objects(
                        Bucket=profile.bucket,
                        Delete={"Objects": chunk, "Quiet": True},
                    )
                except Exception as exc:
                    if not _is_missing_content_md5_error(exc):
                        raise
                    for item in chunk:
                        client.delete_object(Bucket=profile.bucket, Key=item["Key"])
                deleted += len(chunk)
        return deleted


def local_file_md5(path: str) -> str:
    import hashlib

    hasher = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _is_missing_content_md5_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    code = str(error.get("Code") or "")
    message = str(error.get("Message") or exc)
    return code == "InvalidRequest" and "Content-MD5" in message
