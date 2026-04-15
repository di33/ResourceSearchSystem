"""JWT authentication middleware.

In development mode (``settings.debug == True``), auth is bypassed so
Swagger UI works without tokens.  In production, every request must
carry a valid ``Authorization: Bearer <token>`` header.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import settings

_DEFAULT_JWT_SECRET = "dev-secret-change-in-production"

_scheme = HTTPBearer(auto_error=False)

if settings.jwt_secret == _DEFAULT_JWT_SECRET and not settings.debug:
    import logging
    logging.getLogger(__name__).warning(
        "JWT secret is using the default value. "
        "Set JWT_SECRET in .env for production deployments."
    )


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
