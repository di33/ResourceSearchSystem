"""Server-side embedding generation — used at commit time to produce vectors."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import List

from app.config import settings

logger = logging.getLogger(__name__)


def _generate_embedding_sync(text: str) -> List[float]:
    """Blocking call that delegates to the configured provider."""
    provider = settings.embedding_provider

    if provider == "dashscope":
        return _dashscope_embed(text)
    elif provider == "zhipu":
        return _zhipu_embed(text)
    else:
        # Fallback: deterministic mock vector
        h = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)
        return [(h + i) % 1000 / 1000.0 for i in range(settings.embedding_dimension)]


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


async def generate_embedding(text: str) -> List[float]:
    """Async wrapper — runs blocking call in a thread pool."""
    return await asyncio.to_thread(_generate_embedding_sync, text)


def get_model_version() -> str:
    """Return the configured embedding model name (e.g. 'text-embedding-v3')."""
    return settings.embedding_model

