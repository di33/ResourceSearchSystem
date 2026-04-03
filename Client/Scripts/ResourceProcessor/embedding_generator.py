"""Embedding vector generation with unified interface, validation, and retry."""

from __future__ import annotations

import hashlib
import logging
import struct
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import List, Optional, Tuple


@dataclass
class EmbeddingResult:
    vector_data: List[float]
    vector_dimension: int
    embedding_checksum: str
    embedding_generate_time: float  # seconds

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> EmbeddingResult:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class BaseEmbeddingProvider(ABC):
    """Embedding Provider base class."""

    @abstractmethod
    async def generate_embedding(self, text: str) -> List[float]:
        ...

    @abstractmethod
    def expected_dimension(self) -> int:
        ...

    @abstractmethod
    def model_version(self) -> str:
        ...


class MockEmbeddingProvider(BaseEmbeddingProvider):
    """Returns a deterministic pseudo-vector based on the input text hash."""

    def __init__(self, dimension: int = 768, model_ver: str = "mock_embed_v1"):
        self._dimension = dimension
        self._model_ver = model_ver

    async def generate_embedding(self, text: str) -> List[float]:
        h = hashlib.md5(text.encode("utf-8")).hexdigest()
        seed = int(h[:8], 16)
        return [(seed + i) % 1000 / 1000.0 for i in range(self._dimension)]

    def expected_dimension(self) -> int:
        return self._dimension

    def model_version(self) -> str:
        return self._model_ver


class EmbeddingFactory:
    """Registry-based factory for embedding providers."""

    _registry: dict[str, type] = {}

    @classmethod
    def register(cls, name: str, provider_class: type):
        cls._registry[name] = provider_class

    @classmethod
    def create(cls, name: str, **kwargs) -> BaseEmbeddingProvider:
        if name not in cls._registry:
            raise ValueError(
                f"Unknown embedding provider: {name}. "
                f"Available: {list(cls._registry.keys())}"
            )
        return cls._registry[name](**kwargs)

    @classmethod
    def available_providers(cls) -> list[str]:
        return list(cls._registry.keys())


EmbeddingFactory.register("mock", MockEmbeddingProvider)


def _compute_checksum(vector: List[float]) -> str:
    """MD5 over the IEEE-754 single-precision byte representation."""
    raw = struct.pack(f"{len(vector)}f", *vector)
    return hashlib.md5(raw).hexdigest()


def _clean_text(text: str) -> str:
    """Collapse whitespace / control characters into single spaces and strip."""
    return " ".join(text.split()).strip()


def validate_embedding(vector: List[float], expected_dim: int) -> Tuple[bool, str]:
    """Return (passed, reason).  ``reason`` is empty on success."""
    if not isinstance(vector, list):
        return False, f"vector_data 必须为 list，实际为 {type(vector).__name__}"
    if len(vector) == 0:
        return False, "vector_data 为空"
    if len(vector) != expected_dim:
        return False, f"维度不一致：期望 {expected_dim}，实际 {len(vector)}"
    for i, v in enumerate(vector):
        if not isinstance(v, (int, float)):
            return False, f"vector_data[{i}] 类型不是 float: {type(v).__name__}"
    return True, ""


async def generate_embedding_with_retry(
    text: str,
    provider_name: str = "mock",
    max_retries: int = 2,
    **provider_kwargs,
) -> Tuple[Optional[EmbeddingResult], str]:
    """Generate an embedding vector with validation and automatic retry.

    Returns ``(result, error_message)``.
    *result* is ``None`` on failure; *error_message* is ``""`` on success.
    """
    cleaned = _clean_text(text)
    if not cleaned:
        return None, "输入文本为空"

    provider = EmbeddingFactory.create(provider_name, **provider_kwargs)
    expected_dim = provider.expected_dimension()

    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            t0 = time.monotonic()
            vector = await provider.generate_embedding(cleaned)
            elapsed = time.monotonic() - t0

            passed, reason = validate_embedding(vector, expected_dim)
            if not passed:
                last_error = reason
                logging.warning(
                    "Embedding validation failed (attempt=%d): %s", attempt, reason
                )
                continue

            checksum = _compute_checksum(vector)
            return EmbeddingResult(
                vector_data=vector,
                vector_dimension=len(vector),
                embedding_checksum=checksum,
                embedding_generate_time=round(elapsed, 4),
            ), ""
        except Exception as exc:
            last_error = str(exc)
            logging.warning(
                "Embedding generation error (attempt=%d): %s", attempt, exc
            )

    return None, last_error
