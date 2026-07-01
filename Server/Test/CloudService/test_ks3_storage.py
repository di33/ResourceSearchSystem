import io
import time
from unittest.mock import patch

from app.services import ks3_storage
from app.services.ks3_storage import KS3Storage


class _FakeS3:
    def __init__(self):
        self.put_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        return {"ETag": '"etag"'}

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.put_calls[-1]["Body"]), "ETag": '"etag"'}


def test_upload_fileobj_uses_put_object_with_content_length():
    s3 = _FakeS3()
    storage = KS3Storage(s3)

    size, etag, md5 = storage.upload_fileobj("downloads/res/pkg.zip", io.BytesIO(b"payload"), "application/zip")

    assert size == 7
    assert etag == '"etag"'
    assert md5 == "321c3cf486ed509164edec1e1981fec8"
    assert s3.put_calls == [
        {
            "Bucket": storage.bucket,
            "Key": "downloads/res/pkg.zip",
            "Body": b"payload",
            "ContentLength": 7,
            "ContentType": "application/zip",
        }
    ]


def test_generate_presigned_download_url_adds_tencent_cdn_type_a_auth(monkeypatch):
    monkeypatch.setattr(ks3_storage.settings, "ks3_cdn_endpoint", "https://cdn.example.com")
    monkeypatch.setattr(ks3_storage.settings, "ks3_cdn_auth_enabled", True)
    monkeypatch.setattr(ks3_storage.settings, "ks3_cdn_auth_type", "A")
    monkeypatch.setattr(ks3_storage.settings, "ks3_cdn_auth_sign_param", "sign")
    monkeypatch.setattr(ks3_storage.settings, "ks3_cdn_auth_expires", 86400)
    monkeypatch.setattr(ks3_storage.settings, "ks3_cdn_auth_uid", "0")
    monkeypatch.setattr(ks3_storage.settings, "ks3_cdn_auth_rand", "0")
    monkeypatch.setattr(ks3_storage.settings, "ks3_cdn_auth_key_primary", "secret")

    storage = KS3Storage(_FakeS3())
    with patch.object(time, "time", return_value=1_700_000_000):
        url = storage.generate_presigned_download_url("previews/res-test/test file.webp")

    uri = "/previews/res-test/test%20file.webp"
    expires_at = 1_700_086_400
    md5 = "5e9c683001212ba8ffdf73e8bd57d225"
    assert url == f"https://cdn.example.com{uri}?sign={expires_at}-0-0-{md5}"


def test_generate_presigned_download_url_requires_cdn_auth_key(monkeypatch):
    monkeypatch.setattr(ks3_storage.settings, "ks3_cdn_endpoint", "https://cdn.example.com")
    monkeypatch.setattr(ks3_storage.settings, "ks3_cdn_auth_enabled", True)
    monkeypatch.setattr(ks3_storage.settings, "ks3_cdn_auth_type", "A")
    monkeypatch.setattr(ks3_storage.settings, "ks3_cdn_auth_key_primary", "")

    storage = KS3Storage(_FakeS3())

    try:
        storage.generate_presigned_download_url("previews/res-test/test.webp")
    except RuntimeError as exc:
        assert "KS3_CDN_AUTH_KEY_PRIMARY" in str(exc)
    else:
        raise AssertionError("expected missing CDN auth key to fail")
