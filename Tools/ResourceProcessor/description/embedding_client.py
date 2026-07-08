"""Provider-neutral embeddings used for pack description aggregation."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Iterable

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://kspmas.ksyun.com/v1"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env(name: str, legacy_name: str = "", default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if legacy_name:
        legacy_value = os.environ.get(legacy_name, "").strip()
        if legacy_value:
            return legacy_value
    return default


def _env_int_compat(name: str, legacy_name: str, default: int) -> int:
    raw = _env(name, legacy_name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _embedding_provider() -> str:
    return _env("RESOURCE_EMBEDDING_PROVIDER", "CLIENT_EMBEDDING_PROVIDER", "ksyun").lower()


def _embedding_model() -> str:
    return _env("RESOURCE_EMBEDDING_MODEL", "CLIENT_EMBEDDING_MODEL", "qwen3-embedding-8b")


def _embedding_dimension() -> int:
    return _env_int_compat("RESOURCE_EMBEDDING_DIMENSION", "CLIENT_EMBEDDING_DIMENSION", 4096)


def _embedding_batch_size() -> int:
    return _env_int_compat("RESOURCE_EMBEDDING_BATCH_SIZE", "CLIENT_EMBEDDING_BATCH_SIZE", 64)


def _clean_texts(texts: Iterable[str]) -> list[str]:
    cleaned = [" ".join(str(text or "").split()).strip() for text in texts]
    if any(not text for text in cleaned):
        raise ValueError("Embedding input text is empty after cleaning")
    return cleaned


def _mock_embed_batch(texts: list[str]) -> list[list[float]]:
    dimension = _embedding_dimension()
    vectors: list[list[float]] = []
    for text in texts:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = []
        for index in range(dimension):
            byte = digest[index % len(digest)]
            values.append(((byte + index) % 251) / 250.0)
        vectors.append(values)
    return vectors


def _ksyun_embed_batch(texts: list[str]) -> list[list[float]]:
    api_key = (
        os.environ.get("KSPMAS_API_KEY", "")
        or os.environ.get("KSC_API_KEY", "")
    )
    if not api_key:
        raise RuntimeError("KSPMAS_API_KEY (or KSC_API_KEY) not set in environment")

    base_url = (
        _env("RESOURCE_EMBEDDING_BASE_URL", "CLIENT_EMBEDDING_BASE_URL")
        or os.environ.get("KSPMAS_BASE_URL", "")
        or _DEFAULT_BASE_URL
    ).rstrip("/")

    payload: dict[str, object] = {
        "model": _embedding_model(),
        "input": texts,
    }
    dimension = _embedding_dimension()
    if dimension > 0:
        payload["dimensions"] = dimension

    resp = requests.post(
        f"{base_url}/embeddings",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=_env_int_compat("RESOURCE_EMBEDDING_TIMEOUT", "CLIENT_EMBEDDING_TIMEOUT", 60),
    )
    if not resp.ok:
        raise RuntimeError(
            f"Ksyun embeddings failed: code={resp.status_code}, body={resp.text[:300]}"
        )

    rows = resp.json().get("data") or []
    if len(rows) != len(texts):
        raise RuntimeError(
            f"Ksyun embeddings response count mismatch: got {len(rows)} expected {len(texts)}"
        )
    rows = sorted(rows, key=lambda row: int(row.get("index", 0)))
    vectors = [row.get("embedding") for row in rows]
    if not all(isinstance(vector, list) for vector in vectors):
        raise RuntimeError("Ksyun embeddings response format invalid")
    return vectors


def _dashscope_embed_batch(texts: list[str]) -> list[list[float]]:
    from http import HTTPStatus

    import dashscope
    from dashscope import TextEmbedding

    dashscope.api_key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not dashscope.api_key:
        raise RuntimeError("DASHSCOPE_API_KEY not set in environment")

    response = TextEmbedding.call(
        model=_embedding_model(),
        input=texts,
        dimension=_embedding_dimension(),
    )
    if response.status_code != HTTPStatus.OK:
        raise RuntimeError(
            f"DashScope Embedding failed: code={response.status_code}, "
            f"message={response.message}"
        )

    rows = response.output["embeddings"]
    rows = sorted(rows, key=lambda row: int(row.get("text_index", row.get("index", 0))))
    return [row["embedding"] for row in rows]


def _zhipu_embed_batch(texts: list[str]) -> list[list[float]]:
    from zhipuai import ZhipuAI

    api_key = os.environ.get("ZHIPUAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("ZHIPUAI_API_KEY not set in environment")

    client = ZhipuAI(api_key=api_key)
    response = client.embeddings.create(
        model=_embedding_model(),
        input=texts,
        dimensions=_embedding_dimension(),
    )
    return [item.embedding for item in response.data]


def _embed_batch_sync(texts: list[str]) -> list[list[float]]:
    provider = _embedding_provider()
    if provider == "ksyun":
        return _ksyun_embed_batch(texts)
    if provider == "dashscope":
        return _dashscope_embed_batch(texts)
    if provider == "zhipu":
        return _zhipu_embed_batch(texts)
    if provider == "mock":
        return _mock_embed_batch(texts)
    raise RuntimeError(f"Unknown RESOURCE_EMBEDDING_PROVIDER: {provider}")


async def generate_embeddings(texts: Iterable[str], max_retries: int = 2) -> list[list[float]]:
    cleaned = _clean_texts(texts)
    if not cleaned:
        return []

    results: list[list[float]] = []
    batch_size = _embedding_batch_size()
    for start in range(0, len(cleaned), batch_size):
        batch = cleaned[start:start + batch_size]
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                vectors = await asyncio.to_thread(_embed_batch_sync, batch)
                results.extend(vectors)
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Embedding generation attempt %d failed: %s",
                    attempt + 1,
                    exc,
                )
        else:
            raise RuntimeError(
                f"Embedding generation failed after {max_retries + 1} attempts: {last_exc}"
            )
    return results


def get_model_version() -> str:
    return _embedding_model()
