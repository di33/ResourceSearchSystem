"""JWT + API Key authentication middleware.

In development mode (``settings.debug == True``), auth is bypassed so
Swagger UI works without tokens.  In production, every request must
carry a valid ``Authorization: Bearer <token>`` header (JWT) or
``X-API-Key`` header (API Key).

Search endpoints additionally accept API Key authentication via:
- ``X-API-Key: <key>`` header
- ``Authorization: ApiKey <key>`` header
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import settings

_DEFAULT_JWT_SECRET = "dev-secret-change-in-production"

logger = logging.getLogger(__name__)

_scheme = HTTPBearer(auto_error=False)
_apikey_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)

if settings.jwt_secret == _DEFAULT_JWT_SECRET and not settings.debug:
    logger.warning(
        "JWT secret is using the default value. "
        "Set JWT_SECRET in the environment or .env.local for production deployments."
    )


def _split_keys(raw: str) -> set[str]:
    return {k.strip() for k in str(raw or "").replace(";", ",").split(",") if k.strip()}


def _read_api_keys() -> set[str]:
    """Read/search API keys. ``API_KEYS`` remains a compatibility fallback."""
    raw = settings.search_read_api_keys.strip() or settings.api_keys.strip()
    return _split_keys(raw)


def _ingest_api_keys() -> set[str]:
    """Write/ingest API keys used by the resource processing server."""
    raw = settings.search_ingest_api_keys.strip()
    if not raw:
        return set()
    return _split_keys(raw)


def create_access_token(subject: str, extra: dict | None = None, expires_delta: timedelta | None = None) -> str:
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.jwt_expire_minutes))
    payload = {"sub": subject, "exp": expire}
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_scheme),
) -> dict:
    """Dependency that enforces JWT auth (skipped when ``debug`` is True)."""
    if settings.debug:
        return {"sub": "dev-user", "role": "admin"}

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_token(credentials.credentials)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload


def _check_api_key(key: str, valid_keys: set[str]) -> bool:
    """Constant-time comparison of the provided key against configured keys."""
    return any(hmac.compare_digest(key, valid) for valid in valid_keys)


def _claim_values(payload: dict, *names: str) -> set[str]:
    values: set[str] = set()
    for name in names:
        raw = payload.get(name)
        if raw is None:
            continue
        if isinstance(raw, str):
            parts = raw.replace(",", " ").split()
        elif isinstance(raw, (list, tuple, set)):
            parts = []
            for item in raw:
                parts.extend(str(item or "").replace(",", " ").split())
        else:
            parts = [str(raw)]
        values.update(part.strip().lower() for part in parts if part.strip())
    return values


def _jwt_allows_ingest(payload: dict) -> bool:
    grants = _claim_values(payload, "role", "roles", "scope", "scopes", "permissions")
    allowed = {"admin", "ingest", "search:ingest", "resources:write"}
    return bool(grants & allowed)


async def _require_api_or_jwt(
    *,
    credentials: Optional[HTTPAuthorizationCredentials],
    api_key: Optional[str],
    api_keys: set[str],
    auth_name: str,
) -> dict:
    if settings.debug:
        return {"sub": "dev-user", "role": "admin", "auth_mode": "debug"}

    # 1. Try X-API-Key header
    if api_key and _check_api_key(api_key, api_keys):
        return {"sub": auth_name, "role": auth_name, "auth_mode": "api_key"}

    # 2. Try Authorization: ApiKey <key>
    if credentials and credentials.scheme.lower() == "apikey":
        if _check_api_key(credentials.credentials, api_keys):
            return {"sub": auth_name, "role": auth_name, "auth_mode": "api_key"}

    # 3. Fall back to JWT Bearer
    if credentials and credentials.scheme.lower() == "bearer":
        try:
            payload = decode_token(credentials.credentials)
            payload["auth_mode"] = "jwt"
            return payload
        except JWTError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token: {exc}",
                headers={"WWW-Authenticate": "Bearer"},
            )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid authentication. Provide X-API-Key header, ApiKey token, or Bearer JWT.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_read_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_scheme),
    api_key: Optional[str] = Depends(_apikey_scheme),
) -> dict:
    """Dependency for read/search/browser endpoints."""
    return await _require_api_or_jwt(
        credentials=credentials,
        api_key=api_key,
        api_keys=_read_api_keys(),
        auth_name="reader",
    )


async def require_ingest_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_scheme),
    api_key: Optional[str] = Depends(_apikey_scheme),
) -> dict:
    """Dependency for resource-processing-server ingest endpoints."""
    auth = await _require_api_or_jwt(
        credentials=credentials,
        api_key=api_key,
        api_keys=_ingest_api_keys(),
        auth_name="ingest",
    )
    if auth.get("auth_mode") != "jwt" or _jwt_allows_ingest(auth):
        return auth
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Bearer JWT is not allowed to access ingest endpoints.",
    )


require_search_auth = require_read_auth
