"""Tests for DashScope embedding provider — all dashscope calls are mocked."""

import asyncio
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("DASHSCOPE_API_KEY", "sk-test-key-for-unit-tests")

from ResourceProcessor.embedding.dashscope_embedding_provider import (
    DashScopeEmbeddingProvider,
    EmbeddingFactory,
)
from ResourceProcessor.embedding.embedding_generator import (
    generate_embedding_with_retry,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_provider_requires_api_key():
    saved = os.environ.pop("DASHSCOPE_API_KEY", None)
    try:
        with pytest.raises(ValueError, match="API Key"):
            DashScopeEmbeddingProvider(api_key="")
    finally:
        if saved:
            os.environ["DASHSCOPE_API_KEY"] = saved


def test_provider_default_params():
    p = DashScopeEmbeddingProvider(api_key="sk-test")
    assert p.expected_dimension() == 1024
    assert p.model_version() == "text-embedding-v3"


def test_provider_custom_params():
    p = DashScopeEmbeddingProvider(model="text-embedding-v4", dimension=2048, api_key="sk-test")
    assert p.expected_dimension() == 2048
    assert p.model_version() == "text-embedding-v4"


# ---------------------------------------------------------------------------
# Factory registration
# ---------------------------------------------------------------------------

def test_dashscope_registered_in_factory():
    assert "dashscope" in EmbeddingFactory.available_providers()
    assert "text-embedding-v3" in EmbeddingFactory.available_providers()


# ---------------------------------------------------------------------------
# generate_embedding (mocked API call)
# ---------------------------------------------------------------------------

def _fake_vector(dim: int = 1024):
    return [float(i) / dim for i in range(dim)]


def test_generate_embedding_success():
    provider = DashScopeEmbeddingProvider(api_key="sk-test", dimension=1024)
    fake = _fake_vector(1024)

    with patch.object(provider, "_call_sync", return_value=fake):
        vector = _run(provider.generate_embedding("测试文本"))

    assert len(vector) == 1024
    assert vector == fake


def test_generate_embedding_api_error():
    provider = DashScopeEmbeddingProvider(api_key="sk-test")

    with patch.object(provider, "_call_sync", side_effect=RuntimeError("Embedding API 失败")):
        with pytest.raises(RuntimeError, match="Embedding API 失败"):
            _run(provider.generate_embedding("测试"))


def test_generate_with_retry_via_factory():
    fake = _fake_vector(1024)

    with patch(
        "ResourceProcessor.embedding.dashscope_embedding_provider.DashScopeEmbeddingProvider._call_sync",
        return_value=fake,
    ):
        result, err = _run(generate_embedding_with_retry("测试文本", provider_name="dashscope"))

    assert err == ""
    assert result is not None
    assert result.vector_dimension == 1024
    assert len(result.vector_data) == 1024
    assert result.embedding_checksum  # non-empty


def test_generate_with_retry_custom_dimension():
    fake = _fake_vector(768)

    with patch(
        "ResourceProcessor.embedding.dashscope_embedding_provider.DashScopeEmbeddingProvider._call_sync",
        return_value=fake,
    ):
        result, err = _run(
            generate_embedding_with_retry("测试", provider_name="dashscope", dimension=768)
        )

    assert err == ""
    assert result is not None
    assert result.vector_dimension == 768
