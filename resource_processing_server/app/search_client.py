from __future__ import annotations

from typing import Any

import httpx

from resource_processing_server.app.config import settings


class SearchServerClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or settings.search_server_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if settings.search_server_api_key:
            headers["X-API-Key"] = settings.search_server_api_key
        if settings.search_server_bearer_token:
            headers["Authorization"] = f"Bearer {settings.search_server_bearer_token}"
        return headers

    async def upsert_resource(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=settings.search_server_timeout) as client:
            response = await client.post(
                f"{self.base_url}/resources/upsert",
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    async def delete_resource(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=settings.search_server_timeout) as client:
            response = await client.post(
                f"{self.base_url}/resources/delete",
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()
