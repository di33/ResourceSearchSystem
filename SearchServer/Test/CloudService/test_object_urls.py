import json
import time
from unittest.mock import patch

from app.services.object_urls import ObjectUrlGenerator, append_url_version


def test_append_url_version_handles_signed_and_unsigned_urls():
    assert append_url_version("https://cdn.example.com/a.webp", 42) == (
        "https://cdn.example.com/a.webp?v=42"
    )
    assert append_url_version("https://cdn.example.com/a.webp?sign=abc", 42) == (
        "https://cdn.example.com/a.webp?sign=abc&v=42"
    )
    assert append_url_version("", 42) == ""
    assert append_url_version("https://cdn.example.com/a.webp", None) == (
        "https://cdn.example.com/a.webp"
    )


def test_generate_download_url_adds_tencent_cdn_type_a_auth(monkeypatch):
    monkeypatch.setenv("STORAGE_PROFILES_JSON", json.dumps({
        "default_profile_id": "preview",
        "profiles": {
            "preview": {
                "bucket": "resources",
                "cdn_endpoint": "https://cdn.example.com",
                "url_mode": "cdn_type_a",
                "cdn_auth_key": "secret",
                "cdn_auth_algorithm": "md5",
                "cdn_auth_expires": 86400,
                "cdn_auth_time_format": "unix_decimal",
                "cdn_auth_scope": "file_suffix",
                "cdn_auth_file_suffixes": ["*"],
            }
        },
    }))

    urls = ObjectUrlGenerator()
    with patch.object(time, "time", return_value=1_700_000_000):
        url = urls.generate_download_url("previews/res-test/test file.webp")

    uri = "/previews/res-test/test%20file.webp"
    expires_at = 1_700_086_400
    md5 = "5e9c683001212ba8ffdf73e8bd57d225"
    assert url == f"https://cdn.example.com{uri}?sign={expires_at}-0-0-{md5}"


def test_generate_download_url_skips_auth_for_unmatched_suffix(monkeypatch):
    monkeypatch.setenv("STORAGE_PROFILES_JSON", json.dumps({
        "default_profile_id": "preview",
        "profiles": {
            "preview": {
                "bucket": "resources",
                "cdn_endpoint": "https://cdn.example.com",
                "url_mode": "cdn_type_a",
                "cdn_auth_scope": "file_suffix",
                "cdn_auth_file_suffixes": ["webp"],
            }
        },
    }))

    urls = ObjectUrlGenerator()

    url = urls.generate_download_url("docs/res-test/readme.txt")

    assert url == "https://cdn.example.com/docs/res-test/readme.txt"


def test_generate_download_url_uses_first_cdn_auth_key(monkeypatch):
    monkeypatch.setenv("STORAGE_PROFILES_JSON", json.dumps({
        "default_profile_id": "preview",
        "profiles": {
            "preview": {
                "bucket": "resources",
                "cdn_endpoint": "https://cdn.example.com",
                "url_mode": "cdn_type_a",
                "cdn_auth_keys": ["primary-secret", "secondary-secret"],
                "cdn_auth_expires": 86400,
            }
        },
    }))

    urls = ObjectUrlGenerator()
    with patch.object(time, "time", return_value=1_700_000_000):
        url = urls.generate_download_url("previews/res-test/test file.webp")

    uri = "/previews/res-test/test%20file.webp"
    expires_at = 1_700_086_400
    md5 = "a7a63e3816995d904ecad9102b09c7f6"
    assert url == f"https://cdn.example.com{uri}?sign={expires_at}-0-0-{md5}"


def test_generate_download_url_requires_cdn_auth_key(monkeypatch):
    monkeypatch.setenv("STORAGE_PROFILES_JSON", json.dumps({
        "default_profile_id": "preview",
        "profiles": {
            "preview": {
                "bucket": "resources",
                "cdn_endpoint": "https://cdn.example.com",
                "url_mode": "cdn_type_a",
                "cdn_auth_key": "",
            }
        },
    }))

    urls = ObjectUrlGenerator()

    try:
        urls.generate_download_url("previews/res-test/test.webp")
    except RuntimeError as exc:
        assert "CDN auth key" in str(exc)
    else:
        raise AssertionError("expected missing CDN auth key to fail")
