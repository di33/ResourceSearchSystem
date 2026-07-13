"""Server-side embedding generation — used at commit time to produce vectors."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from typing import List

import requests

from app.config import settings

logger = logging.getLogger(__name__)
_thread_local = threading.local()


def _http_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


def _mock_embedding(text: str) -> List[float]:
    h = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)
    return [(h + i) % 1000 / 1000.0 for i in range(settings.embedding_dimension)]


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
        return _mock_embedding(text)


def _generate_embeddings_sync(texts: list[str]) -> list[List[float]]:
    provider = settings.embedding_provider
    if provider == "ksyun":
        return _ksyun_embed_batch(texts)
    return [_generate_embedding_sync(text) for text in texts]


def _ksyun_embed(text: str) -> List[float]:
    return _ksyun_embed_batch([text])[0]


def _ksyun_embed_batch(texts: list[str]) -> list[List[float]]:
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
        "input": texts[0] if len(texts) == 1 else texts,
    }
    if settings.embedding_dimension > 0:
        payload["dimensions"] = settings.embedding_dimension

    resp = _http_session().post(
        f"{base_url}/embeddings",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=max(1.0, float(settings.embedding_timeout_seconds)),
    )
    if not resp.ok:
        raise RuntimeError(
            f"Ksyun embeddings failed: code={resp.status_code}, body={resp.text[:300]}"
        )

    data = resp.json()
    rows = data.get("data") or []
    if not rows:
        raise RuntimeError("Ksyun embeddings response missing data")

    rows = sorted(rows, key=lambda item: int(item.get("index", 0)))
    vectors = [row.get("embedding") for row in rows]
    if len(vectors) != len(texts) or any(not isinstance(vector, list) for vector in vectors):
        raise RuntimeError("Ksyun embeddings response format invalid")
    return vectors


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
            if attempt < max_retries:
                await asyncio.sleep(min(10.0, 1.5 * (attempt + 1)))
    raise RuntimeError(f"Embedding generation failed after {max_retries + 1} attempts: {last_exc}")


async def generate_embeddings(texts: list[str], max_retries: int = 2) -> list[List[float]]:
    cleaned = [" ".join(text.split()).strip() for text in texts]
    if any(not text for text in cleaned):
        raise ValueError("Embedding input text is empty after cleaning")

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.to_thread(_generate_embeddings_sync, cleaned)
        except Exception as exc:
            last_exc = exc
            logger.warning("Batch embedding generation attempt %d failed: %s", attempt + 1, exc)
            if attempt < max_retries:
                await asyncio.sleep(min(10.0, 1.5 * (attempt + 1)))
    raise RuntimeError(f"Batch embedding generation failed after {max_retries + 1} attempts: {last_exc}")


def get_model_version() -> str:
    """Return the configured embedding model name (e.g. 'text-embedding-v3')."""
    return settings.embedding_model
