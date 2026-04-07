"""Tests for ResourceProcessor.embedding_generator module."""

import asyncio
from typing import List
from unittest.mock import AsyncMock, patch

import pytest

from ResourceProcessor.embedding.embedding_generator import (
    BaseEmbeddingProvider,
    EmbeddingFactory,
    EmbeddingResult,
    MockEmbeddingProvider,
    _clean_text,
    _compute_checksum,
    generate_embedding_with_retry,
    validate_embedding,
)


def _run(coro):
    """Helper to run async coroutines in sync tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# --- 1. EmbeddingResult serialization ---

def test_embedding_result_to_dict_roundtrip():
    original = EmbeddingResult(
        vector_data=[0.1, 0.2, 0.3],
        vector_dimension=3,
        embedding_checksum="abc123",
        embedding_generate_time=0.0042,
    )
    d = original.to_dict()
    restored = EmbeddingResult.from_dict(d)
    assert restored == original


# --- 2-4. MockEmbeddingProvider ---

def test_mock_provider_returns_correct_dimension():
    provider = MockEmbeddingProvider(dimension=768)
    vector = _run(provider.generate_embedding("hello world"))
    assert len(vector) == provider.expected_dimension()
    assert len(vector) == 768


def test_mock_provider_deterministic():
    provider = MockEmbeddingProvider()
    v1 = _run(provider.generate_embedding("same text"))
    v2 = _run(provider.generate_embedding("same text"))
    assert v1 == v2


def test_mock_provider_custom_dimension():
    provider = MockEmbeddingProvider(dimension=384, model_ver="custom_v2")
    vector = _run(provider.generate_embedding("test"))
    assert len(vector) == 384
    assert provider.expected_dimension() == 384
    assert provider.model_version() == "custom_v2"


# --- 5-6. EmbeddingFactory ---

def test_embedding_factory_mock_registered():
    assert "mock" in EmbeddingFactory.available_providers()
    provider = EmbeddingFactory.create("mock")
    assert isinstance(provider, MockEmbeddingProvider)


def test_embedding_factory_unknown_raises():
    with pytest.raises(ValueError, match="Unknown embedding provider"):
        EmbeddingFactory.create("nonexistent_provider")


# --- 7-10. validate_embedding ---

def test_validate_embedding_passes():
    ok, msg = validate_embedding([0.1, 0.2, 0.3], expected_dim=3)
    assert ok is True
    assert msg == ""


def test_validate_embedding_wrong_dimension():
    ok, msg = validate_embedding([0.1, 0.2], expected_dim=3)
    assert ok is False
    assert "维度不一致" in msg


def test_validate_embedding_empty_vector():
    ok, msg = validate_embedding([], expected_dim=3)
    assert ok is False
    assert "为空" in msg


def test_validate_embedding_non_float_element():
    ok, msg = validate_embedding([0.1, "not_a_float", 0.3], expected_dim=3)
    assert ok is False
    assert "不是 float" in msg


# --- 11. _clean_text ---

def test_clean_text_removes_whitespace():
    assert _clean_text("  hello   world  ") == "hello world"
    assert _clean_text("\n\thello\t\nworld\n") == "hello world"
    assert _clean_text("   ") == ""


# --- 12-13. generate_embedding_with_retry ---

def test_generate_with_retry_succeeds():
    result, err = _run(generate_embedding_with_retry("hello world"))
    assert result is not None
    assert err == ""
    assert isinstance(result, EmbeddingResult)
    assert result.vector_dimension == 768
    assert len(result.vector_data) == 768
    assert result.embedding_checksum != ""
    assert result.embedding_generate_time >= 0


def test_generate_with_retry_empty_text_fails():
    result, err = _run(generate_embedding_with_retry("   "))
    assert result is None
    assert "文本为空" in err


# --- 14. Retry on failure then succeed ---

class _FailThenSucceedProvider(BaseEmbeddingProvider):
    """Fails on the first call, succeeds on the second."""

    def __init__(self, dimension: int = 4):
        self._dimension = dimension
        self._call_count = 0

    async def generate_embedding(self, text: str) -> List[float]:
        self._call_count += 1
        if self._call_count == 1:
            raise RuntimeError("transient error")
        return [0.1] * self._dimension

    def expected_dimension(self) -> int:
        return self._dimension

    def model_version(self) -> str:
        return "fail_then_succeed_v1"


def test_generate_with_retry_retries_on_failure():
    EmbeddingFactory.register("_fail_then_succeed", _FailThenSucceedProvider)
    try:
        result, err = _run(
            generate_embedding_with_retry("test text", provider_name="_fail_then_succeed")
        )
        assert result is not None
        assert err == ""
        assert result.vector_dimension == 4
    finally:
        EmbeddingFactory._registry.pop("_fail_then_succeed", None)


# --- 15. Checksum consistency ---

def test_compute_checksum_consistency():
    vec = [0.1, 0.2, 0.3, 0.4, 0.5]
    c1 = _compute_checksum(vec)
    c2 = _compute_checksum(vec)
    assert c1 == c2
    assert isinstance(c1, str)
    assert len(c1) == 32  # MD5 hex digest length
