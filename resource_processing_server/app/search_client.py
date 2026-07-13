from __future__ import annotations

import asyncio
from typing import Any

import httpx

from resource_processing_server.app.config import settings


class SearchServerClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.search_server_url).rstrip("/")
        self._client = httpx.AsyncClient(timeout=settings.search_server_timeout)

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if settings.search_server_api_key:
            headers["X-API-Key"] = settings.search_server_api_key
        if settings.search_server_bearer_token:
            headers["Authorization"] = f"Bearer {settings.search_server_bearer_token}"
        return headers

    async def upsert_resource(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_with_retries("/resources/upsert", payload, retries=3)

    async def delete_resource(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_with_retries("/resources/delete", payload, retries=3)

    async def _post_with_retries(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        retries: int,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(max(1, retries)):
            try:
                response = await self._client.post(
                    f"{self.base_url}{path}",
                    json=payload,
                    headers=self._headers(),
                )
                if response.status_code not in {502, 503, 504}:
                    response.raise_for_status()
                    return response.json()
                last_error = httpx.HTTPStatusError(
                    f"retryable SearchServer status {response.status_code}",
                    request=response.request,
                    response=response,
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
            if attempt + 1 < retries:
                await asyncio.sleep(0.2 * (attempt + 1))
        if last_error is not None:
            raise last_error
        raise RuntimeError("SearchServer request failed without an error")

    async def close(self) -> None:
        await self._client.aclose()
