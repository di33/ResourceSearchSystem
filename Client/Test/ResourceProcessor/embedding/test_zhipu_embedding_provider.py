"""Tests for ZhipuAI embedding provider — all zhipuai calls are mocked."""

import asyncio
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("ZHIPUAI_API_KEY", "test-key-for-unit-tests")

from ResourceProcessor.embedding.zhipu_embedding_provider import (
    ZhipuEmbeddingProvider,
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


def _fake_vector(dim: int = 2048):
    return [float(i) / dim for i in range(dim)]


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_provider_requires_api_key():
    saved = os.environ.pop("ZHIPUAI_API_KEY", None)
    try:
        with pytest.raises(ValueError, match="API Key"):
            ZhipuEmbeddingProvider(api_key="")
    finally:
        if saved:
            os.environ["ZHIPUAI_API_KEY"] = saved


def test_provider_default_params():
    p = ZhipuEmbeddingProvider(api_key="sk-test")
    assert p.expected_dimension() == 2048
    assert p.model_version() == "embedding-3"


def test_provider_custom_params():
    p = ZhipuEmbeddingProvider(dimension=1024, api_key="sk-test")
    assert p.expected_dimension() == 1024


# ---------------------------------------------------------------------------
# Factory registration
# ---------------------------------------------------------------------------

def test_zhipu_registered_in_factory():
    assert "zhipu" in EmbeddingFactory.available_providers()
    assert "embedding-3" in EmbeddingFactory.available_providers()


# ---------------------------------------------------------------------------
# generate_embedding (mocked API call)
# ---------------------------------------------------------------------------

def test_generate_embedding_success():
    provider = ZhipuEmbeddingProvider(api_key="sk-test", dimension=2048)
    fake = _fake_vector(2048)

    with patch.object(provider, "_call_sync", return_value=fake):
        vector = _run(provider.generate_embedding("测试文本"))

    assert len(vector) == 2048


def test_generate_embedding_api_error():
    provider = ZhipuEmbeddingProvider(api_key="sk-test")

    with patch.object(provider, "_call_sync", side_effect=RuntimeError("Embedding失败")):
        with pytest.raises(RuntimeError, match="Embedding失败"):
            _run(provider.generate_embedding("测试"))


def test_generate_with_retry_via_factory():
    fake = _fake_vector(2048)

    with patch(
        "ResourceProcessor.embedding.zhipu_embedding_provider.ZhipuEmbeddingProvider._call_sync",
        return_value=fake,
    ):
        result, err = _run(generate_embedding_with_retry("测试", provider_name="zhipu"))

    assert err == ""
    assert result is not None
    assert result.vector_dimension == 2048
    assert result.embedding_checksum


def test_generate_with_retry_custom_dimension():
    fake = _fake_vector(1024)

    with patch(
        "ResourceProcessor.embedding.zhipu_embedding_provider.ZhipuEmbeddingProvider._call_sync",
        return_value=fake,
    ):
        result, err = _run(
            generate_embedding_with_retry("测试", provider_name="zhipu", dimension=1024)
        )

    assert err == ""
    assert result is not None
    assert result.vector_dimension == 1024
