"""Ksyun StarFlow (OpenAI-compatible) text embedding provider."""

from __future__ import annotations

import asyncio
import os
from typing import List

import requests

from ResourceProcessor.embedding.embedding_generator import (
    BaseEmbeddingProvider,
    EmbeddingFactory,
)

_DEFAULT_BASE_URL = "https://kspmas.ksyun.com/v1"


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


class KsyunEmbeddingProvider(BaseEmbeddingProvider):
    """Ksyun OpenAI-compatible embeddings provider."""

    def __init__(
        self,
        model: str = "embedding-3",
        dimension: int = 1024,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 60,
    ):
        self._model = model
        self._dimension = dimension
        self._api_key = (
            api_key
            or os.environ.get("KSPMAS_API_KEY", "")
            or os.environ.get("KSC_API_KEY", "")
        )
        if not self._api_key:
            raise ValueError(
                "Ksyun API Key 未设置。请设置 KSPMAS_API_KEY（或 KSC_API_KEY）"
                "或在构造时传入 api_key。"
            )

        self._base_url = _normalize_base_url(
            base_url
            or os.environ.get("SERVER_EMBEDDING_BASE_URL", "")
            or os.environ.get("KSPMAS_BASE_URL", "")
            or _DEFAULT_BASE_URL
        )
        self._timeout = timeout

    def _call_sync(self, text: str) -> List[float]:
        payload = {
            "model": self._model,
            "input": text,
        }
        if self._dimension > 0:
            payload["dimensions"] = self._dimension

        resp = requests.post(
            f"{self._base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self._timeout,
        )
        if not resp.ok:
            raise RuntimeError(
                f"Ksyun embeddings 调用失败: code={resp.status_code}, "
                f"body={resp.text[:300]}"
            )

        data = resp.json()
        rows = data.get("data") or []
        if not rows:
            raise RuntimeError("Ksyun 返回缺少 embeddings data")
        vector = rows[0].get("embedding")
        if not isinstance(vector, list):
            raise RuntimeError("Ksyun 返回 embedding 格式错误")
        return vector

    async def generate_embedding(self, text: str) -> List[float]:
        return await asyncio.to_thread(self._call_sync, text)

    def expected_dimension(self) -> int:
        return self._dimension

    def model_version(self) -> str:
        return self._model


EmbeddingFactory.register("ksyun", KsyunEmbeddingProvider)
EmbeddingFactory.register("kspmas", KsyunEmbeddingProvider)
