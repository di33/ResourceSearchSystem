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


def _valid_api_keys() -> set[str]:
    """Parse and cache the configured API keys."""
    raw = settings.api_keys.strip()
    if not raw:
        return set()
    return {k.strip() for k in raw.split(",") if k.strip()}


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


def _check_api_key(key: str) -> bool:
    """Constant-time comparison of the provided key against configured keys."""
    return any(hmac.compare_digest(key, valid) for valid in _valid_api_keys())


async def require_search_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_scheme),
    api_key: Optional[str] = Depends(_apikey_scheme),
) -> dict:
    """Dependency that accepts either API Key or JWT (skipped when ``debug`` is True)."""
    if settings.debug:
        return {"sub": "dev-user", "role": "admin", "auth_mode": "debug"}

    # 1. Try X-API-Key header
    if api_key and _check_api_key(api_key):
        return {"sub": "agent", "role": "agent", "auth_mode": "api_key"}

    # 2. Try Authorization: ApiKey <key>
    if credentials and credentials.scheme.lower() == "apikey":
        if _check_api_key(credentials.credentials):
            return {"sub": "agent", "role": "agent", "auth_mode": "api_key"}

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
