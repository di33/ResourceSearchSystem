from __future__ import annotations

from pathlib import Path

import pytest

from resource_contracts.auth import ClientAuthConfig, parse_client_api_keys, validate_client_api_key
from resource_contracts.path_safety import safe_file_name, safe_join_under
from resource_contracts.url_validation import UrlValidationError, validate_signed_source_url


def test_client_api_key_is_bound_to_client_id():
    config = ClientAuthConfig(
        client_keys=parse_client_api_keys("client-a:key-a|key-a2,client-b:key-b"),
        admin_keys=("admin-key",),
    )

    assert validate_client_api_key("client-a", "key-a", config)
    assert validate_client_api_key("client-a", "key-a2", config)
    assert validate_client_api_key("client-b", "admin-key", config)
    assert not validate_client_api_key("client-a", "key-b", config)
    assert not validate_client_api_key("client-a", "", config)


def test_safe_file_name_and_join_keep_paths_under_root(tmp_path: Path):
    assert safe_file_name(r"..\outside\source.png") == "source.png"
    path = safe_join_under(tmp_path, "../../client:asset", r"..\source.png")

    assert path.is_relative_to(tmp_path.resolve())
    assert ".." not in path.parts
    assert ":" not in path.name


def test_signed_source_url_requires_allowed_host():
    assert validate_signed_source_url(
        "https://assets.example.com/path/source.zip?sign=1",
        allowed_hosts="assets.example.com,*.cdn.example.com",
    )

    with pytest.raises(UrlValidationError, match="not allowed"):
        validate_signed_source_url(
            "https://evil.example.com/path/source.zip?sign=1",
            allowed_hosts="assets.example.com",
        )

    with pytest.raises(UrlValidationError, match="private host"):
        validate_signed_source_url(
            "http://127.0.0.1/private",
            allowed_hosts="127.0.0.1",
        )
