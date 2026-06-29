"""Async HTTP client for the BGE-Reranker service with circuit breaker."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class RerankPair:
    index: int
    score: float


class RerankerClient:
    """Async client for the reranker HTTP service.

    Features:
    - Circuit breaker: opens after 3 consecutive failures, cools down for 30s
    - 1 retry with no backoff (the request is lightweight)
    - Graceful degradation: any failure returns empty list
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.reranker_url,
            timeout=httpx.Timeout(settings.reranker_timeout),
        )
        # Circuit breaker state
        self._failure_count: int = 0
        self._circuit_open_until: float = 0.0

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[RerankPair]:
        """Rerank documents against the query.

        Returns a list of RerankPair sorted by score descending.
        Returns empty list on any failure (graceful degradation).
        """
        if not documents:
            return []

        # Circuit breaker check
        if self._circuit_open_until > 0:
            if time.monotonic() < self._circuit_open_until:
                logger.debug("Reranker circuit breaker is open, skipping")
                return []
            # Cooldown expired — half-open state, allow one probe
            self._circuit_open_until = 0.0

        payload: dict = {"query": query, "documents": documents}
        if top_k is not None:
            payload["top_k"] = top_k

        for attempt in range(2):  # 1 initial + 1 retry
            try:
                resp = await self._client.post("/rerank", json=payload)
                resp.raise_for_status()
                data = resp.json()
                # Reset on success
                self._failure_count = 0
                return [
                    RerankPair(index=item["index"], score=item["score"])
                    for item in data.get("results", [])
                ]
            except (httpx.HTTPError, KeyError, ValueError):
                logger.debug("Reranker request failed (attempt %d)", attempt + 1, exc_info=True)

        # All retries exhausted — record failure
        self._failure_count += 1
        if self._failure_count >= 3:
            self._circuit_open_until = time.monotonic() + 30.0
            logger.warning("Reranker circuit breaker opened for 30s after %d consecutive failures", self._failure_count)
        return []

    async def health(self) -> dict:
        """Probe the reranker service health endpoint."""
        try:
            resp = await self._client.get("/health")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}

    async def close(self) -> None:
        await self._client.aclose()
