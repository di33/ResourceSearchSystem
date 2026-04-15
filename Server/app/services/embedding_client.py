"""Server-side embedding generation — used at commit time to produce vectors."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import List

import requests

from app.config import settings

logger = logging.getLogger(__name__)


def _generate_embedding_sync(text: str) -> List[float]:
    """Blocking call that delegates to the configured provider."""
    provider = settings.embedding_provider

    if provider == "ksyun":
        return _ksyun_embed(text)
    elif provider == "dashscope":
        return _dashscope_embed(text)
    elif provider == "zhipu":
        return _zhipu_embed(text)
    else:
        # Fallback: deterministic mock vector
        h = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)
        return [(h + i) % 1000 / 1000.0 for i in range(settings.embedding_dimension)]


def _ksyun_embed(text: str) -> List[float]:
    api_key = (
        settings.kspmas_api_key
        or settings.ksc_api_key
        or os.environ.get("KSPMAS_API_KEY", "")
        or os.environ.get("KSC_API_KEY", "")
    )
    if not api_key:
        raise RuntimeError("KSPMAS_API_KEY (or KSC_API_KEY) not set in environment")

    base_url = (
        settings.embedding_base_url
        or os.environ.get("SERVER_EMBEDDING_BASE_URL", "")
        or os.environ.get("KSPMAS_BASE_URL", "")
        or "https://kspmas.ksyun.com/v1"
    ).rstrip("/")

    payload = {
        "model": settings.embedding_model,
        "input": text,
    }
    if settings.embedding_dimension > 0:
        payload["dimensions"] = settings.embedding_dimension

    resp = requests.post(
        f"{base_url}/embeddings",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(
            f"Ksyun embeddings failed: code={resp.status_code}, body={resp.text[:300]}"
        )

    data = resp.json()
    rows = data.get("data") or []
    if not rows:
        raise RuntimeError("Ksyun embeddings response missing data")

    vector = rows[0].get("embedding")
    if not isinstance(vector, list):
        raise RuntimeError("Ksyun embeddings response format invalid")
    return vector


def _dashscope_embed(text: str) -> List[float]:
    from http import HTTPStatus
    import dashscope
    from dashscope import TextEmbedding

    dashscope.api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not dashscope.api_key:
        raise RuntimeError("DASHSCOPE_API_KEY not set in environment")

    model = settings.embedding_model
    dimension = settings.embedding_dimension

    response = TextEmbedding.call(
        model=model,
        input=text,
        dimension=dimension,
    )

    if response.status_code != HTTPStatus.OK:
        raise RuntimeError(
            f"DashScope Embedding failed: code={response.status_code}, "
            f"message={response.message}"
        )

    return response.output["embeddings"][0]["embedding"]


def _zhipu_embed(text: str) -> List[float]:
    from zhipuai import ZhipuAI

    api_key = os.environ.get("ZHIPUAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("ZHIPUAI_API_KEY not set in environment")

    client = ZhipuAI(api_key=api_key)
    model = settings.embedding_model
    dimension = settings.embedding_dimension

    response = client.embeddings.create(
        model=model,
        input=text,
        dimensions=dimension,
    )

    return response.data[0].embedding


async def generate_embedding(text: str, max_retries: int = 2) -> List[float]:
    """Async wrapper — runs blocking call in a thread pool with retry."""
    cleaned = " ".join(text.split()).strip()
    if not cleaned:
        raise ValueError("Embedding input text is empty after cleaning")

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.to_thread(_generate_embedding_sync, cleaned)
        except Exception as exc:
            last_exc = exc
            logger.warning("Embedding generation attempt %d failed: %s", attempt + 1, exc)
    raise RuntimeError(f"Embedding generation failed after {max_retries + 1} attempts: {last_exc}")


def get_model_version() -> str:
    """Return the configured embedding model name (e.g. 'text-embedding-v3')."""
    return settings.embedding_model

