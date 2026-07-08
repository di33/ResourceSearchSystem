from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.config import settings
from app.middleware.auth import create_access_token, require_ingest_auth, require_read_auth
from app.routers import browse, resources


@pytest.mark.asyncio
async def test_read_and_ingest_api_keys_are_separated(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "api_keys", "")
    monkeypatch.setattr(settings, "search_read_api_keys", "read-key")
    monkeypatch.setattr(settings, "search_ingest_api_keys", "ingest-key")

    read_auth = await require_read_auth(credentials=None, api_key="read-key")
    ingest_auth = await require_ingest_auth(credentials=None, api_key="ingest-key")

    assert read_auth["role"] == "reader"
    assert ingest_auth["role"] == "ingest"
    with pytest.raises(HTTPException):
        await require_ingest_auth(credentials=None, api_key="read-key")
    with pytest.raises(HTTPException):
        await require_read_auth(credentials=None, api_key="ingest-key")


@pytest.mark.asyncio
async def test_legacy_api_keys_are_read_only(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "api_keys", "legacy-read-key")
    monkeypatch.setattr(settings, "search_read_api_keys", "")
    monkeypatch.setattr(settings, "search_ingest_api_keys", "")

    read_auth = await require_read_auth(credentials=None, api_key="legacy-read-key")

    assert read_auth["role"] == "reader"
    with pytest.raises(HTTPException):
        await require_ingest_auth(credentials=None, api_key="legacy-read-key")


@pytest.mark.asyncio
async def test_ingest_bearer_jwt_requires_ingest_grant(monkeypatch):
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "search_read_api_keys", "")
    monkeypatch.setattr(settings, "search_ingest_api_keys", "")

    reader_token = create_access_token("reader-user", {"role": "reader"})
    reader_credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=reader_token)

    with pytest.raises(HTTPException) as excinfo:
        await require_ingest_auth(credentials=reader_credentials, api_key=None)
    assert excinfo.value.status_code == 403

    ingest_token = create_access_token("ingest-worker", {"scope": "search:ingest"})
    ingest_credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=ingest_token)

    auth = await require_ingest_auth(credentials=ingest_credentials, api_key=None)

    assert auth["sub"] == "ingest-worker"
    assert auth["auth_mode"] == "jwt"


def test_browse_router_requires_read_auth():
    assert any(dependency.dependency is require_read_auth for dependency in browse.router.dependencies)


def test_resources_router_requires_read_auth():
    assert any(dependency.dependency is require_read_auth for dependency in resources.router.dependencies)
