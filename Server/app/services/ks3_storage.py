"""KS3 / MinIO object-storage wrapper (S3-compatible)."""

from __future__ import annotations

import logging
import mimetypes
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from app.config import settings

logger = logging.getLogger(__name__)


class KS3Storage:
    """Thin wrapper around boto3 S3 client for file and preview storage."""

    def __init__(self, s3_client):
        self.s3 = s3_client
        self.bucket = settings.ks3_bucket
        self.presign_s3 = self.s3
        if settings.ks3_public_endpoint:
            # Use a browser-reachable endpoint when generating pre-signed URLs.
            self.presign_s3 = boto3.client(
                "s3",
                endpoint_url=settings.ks3_public_endpoint,
                aws_access_key_id=settings.ks3_access_key,
                aws_secret_access_key=settings.ks3_secret_key,
                region_name=settings.ks3_region,
            )

    # ---- upload ----

    def upload_file(self, key: str, file_path: str, content_type: Optional[str] = None) -> int:
        """Upload a local file to the bucket. Returns uploaded byte count."""
        if content_type is None:
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

        self.s3.upload_file(
            Filename=file_path,
            Bucket=self.bucket,
            Key=key,
            ExtraArgs={"ContentType": content_type},
        )
        head = self.s3.head_object(Bucket=self.bucket, Key=key)
        size = head.get("ContentLength", 0)
        logger.info("Uploaded %s (%d bytes)", key, size)
        return size

    def upload_fileobj(self, key: str, fileobj, content_type: str = "application/octet-stream") -> tuple[int, str]:
        """Upload from a file-like object. Returns (content_length, etag)."""
        self.s3.upload_fileobj(
            Fileobj=fileobj,
            Bucket=self.bucket,
            Key=key,
            ExtraArgs={"ContentType": content_type},
        )
        head = self.s3.head_object(Bucket=self.bucket, Key=key)
        return head.get("ContentLength", 0), head.get("ETag", "")

    # ---- presigned URLs ----

    def generate_presigned_download_url(self, key: str, expires: int | None = None) -> str:
        expires = expires or settings.ks3_presign_expires
        return self.presign_s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )

    def generate_presigned_upload_url(self, key: str, content_type: str, expires: int | None = None) -> str:
        expires = expires or settings.ks3_presign_expires
        return self.presign_s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires,
        )

    # ---- query ----

    def head(self, key: str) -> dict | None:
        try:
            return self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return None
            raise

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    def list_keys(self, prefix: str, max_keys: int = 100) -> list[str]:
        """List object keys under a prefix."""
        resp = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix, MaxKeys=max_keys)
        items = resp.get("Contents", [])
        return [obj.get("Key", "") for obj in items if obj.get("Key")]

    # ---- delete ----

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)
        logger.info("Deleted %s", key)
