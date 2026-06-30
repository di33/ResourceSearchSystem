from __future__ import annotations

import argparse
import base64
import hashlib
import sys
from pathlib import Path

import boto3


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from app.config import settings  # noqa: E402
from app.services.ks3_storage import build_s3_client_config  # noqa: E402


DEFAULT_PREFIXES = ("files/", "previews/", "downloads/")


def _add_delete_objects_content_md5(params, **_kwargs) -> None:
    """Add the Content-MD5 header required by Tencent COS DeleteObjects."""
    headers = params.get("headers", {})
    if "Content-MD5" in headers:
        return

    body = params.get("body", b"")
    if isinstance(body, str):
        body = body.encode("utf-8")
    elif isinstance(body, bytearray):
        body = bytes(body)
    elif not isinstance(body, bytes):
        position = body.tell()
        body = body.read()
        body.seek(position)

    headers["Content-MD5"] = base64.b64encode(hashlib.md5(body).digest()).decode("ascii")


def _build_client():
    client = boto3.client(
        "s3",
        endpoint_url=settings.ks3_endpoint,
        aws_access_key_id=settings.ks3_access_key,
        aws_secret_access_key=settings.ks3_secret_key,
        region_name=settings.ks3_region,
        config=build_s3_client_config(),
    )
    client.meta.events.register("before-call.s3.DeleteObjects", _add_delete_objects_content_md5)
    return client


def _iter_objects(s3, bucket: str, prefix: str):
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if key:
                yield key, int(obj.get("Size") or 0)


def _delete_batch(s3, bucket: str, batch: list[dict[str, str]]) -> None:
    if batch:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clear objects from the configured S3/COS bucket.",
    )
    parser.add_argument(
        "--prefix",
        action="append",
        dest="prefixes",
        help="Object prefix to delete. Can be repeated. Defaults to files/, previews/, downloads/.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Delete every object in the bucket instead of only resource prefixes.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete objects. Without this flag the command is a dry run.",
    )
    parser.add_argument(
        "--expect-bucket",
        default="",
        help="Abort unless the configured bucket name matches this value.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Number of objects per DeleteObjects request. S3 maximum is 1000.",
    )
    args = parser.parse_args()

    if args.batch_size < 1 or args.batch_size > 1000:
        parser.error("--batch-size must be between 1 and 1000")

    bucket = settings.ks3_bucket
    if args.expect_bucket and args.expect_bucket != bucket:
        print(f"Bucket mismatch: configured={bucket!r}, expected={args.expect_bucket!r}", file=sys.stderr)
        return 2

    prefixes = ("",) if args.all else tuple(args.prefixes or DEFAULT_PREFIXES)
    action = "DELETE" if args.yes else "DRY-RUN"
    print(f"mode={action}")
    print(f"bucket={bucket}")
    print(f"endpoint={settings.ks3_endpoint}")
    print(f"prefixes={', '.join(prefixes) if prefixes != ('',) else '<all>'}")

    s3 = _build_client()
    total_count = 0
    total_bytes = 0
    batch: list[dict[str, str]] = []

    for prefix in prefixes:
        prefix_count = 0
        prefix_bytes = 0
        for key, size in _iter_objects(s3, bucket, prefix):
            total_count += 1
            total_bytes += size
            prefix_count += 1
            prefix_bytes += size

            if args.yes:
                batch.append({"Key": key})
                if len(batch) >= args.batch_size:
                    _delete_batch(s3, bucket, batch)
                    print(f"deleted={total_count}")
                    batch.clear()

        print(f"prefix={prefix or '<all>'} count={prefix_count} bytes={prefix_bytes}")

    if args.yes and batch:
        _delete_batch(s3, bucket, batch)
        print(f"deleted={total_count}")

    print(f"total_count={total_count}")
    print(f"total_bytes={total_bytes}")
    if not args.yes:
        print("dry_run=true; add --yes to delete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
