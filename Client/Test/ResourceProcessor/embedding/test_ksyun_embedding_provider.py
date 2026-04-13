"""Tests for Ksyun embedding provider — network calls are mocked."""

import asyncio
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("KSPMAS_API_KEY", "ks-test-key")

from ResourceProcessor.embedding.embedding_generator import (  # noqa: E402
    generate_embedding_with_retry,
)
from ResourceProcessor.embedding.ksyun_embedding_provider import (  # noqa: E402
    EmbeddingFactory,
    KsyunEmbeddingProvider,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fake_vector(dim: int = 1024):
    return [float(i) / dim for i in range(dim)]


def test_provider_requires_api_key():
    saved1 = os.environ.pop("KSPMAS_API_KEY", None)
    saved2 = os.environ.pop("KSC_API_KEY", None)
    try:
        with pytest.raises(ValueError, match="API Key"):
            KsyunEmbeddingProvider(api_key="")
    finally:
        if saved1:
            os.environ["KSPMAS_API_KEY"] = saved1
        if saved2:
            os.environ["KSC_API_KEY"] = saved2


def test_provider_default_params():
    p = KsyunEmbeddingProvider(api_key="ks-test")
    assert p.expected_dimension() == 1024
    assert p.model_version() == "embedding-3"


def test_ksyun_registered_in_factory():
    assert "ksyun" in EmbeddingFactory.available_providers()
    assert "kspmas" in EmbeddingFactory.available_providers()


def test_generate_embedding_success():
    provider = KsyunEmbeddingProvider(api_key="ks-test", dimension=1024)
    fake = _fake_vector(1024)

    with patch.object(provider, "_call_sync", return_value=fake):
        vector = _run(provider.generate_embedding("测试文本"))

    assert len(vector) == 1024
    assert vector == fake


def test_generate_with_retry_via_factory():
    fake = _fake_vector(1024)
    with patch(
        "ResourceProcessor.embedding.ksyun_embedding_provider.KsyunEmbeddingProvider._call_sync",
        return_value=fake,
    ):
        result, err = _run(generate_embedding_with_retry("测试", provider_name="ksyun"))

    assert err == ""
    assert result is not None
    assert result.vector_dimension == 1024
