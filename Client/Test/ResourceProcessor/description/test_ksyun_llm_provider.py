"""Tests for Ksyun LLM provider — network calls are mocked."""

import asyncio
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("KSPMAS_API_KEY", "ks-test-key")

from ResourceProcessor.description.description_generator import (  # noqa: E402
    DescriptionInput,
    DescriptionResult,
    generate_resource_description,
)
from ResourceProcessor.description.ksyun_llm_provider import (  # noqa: E402
    KsyunLLMProvider,
    LLMFactory,
    PROMPT_VERSION,
    _build_user_content,
    _parse_response,
)


def _make_input(preview_path: str = "fake.png") -> DescriptionInput:
    return DescriptionInput(
        preview_path=preview_path,
        resource_type="image",
        preview_strategy="static",
        auxiliary_metadata={"format": "png", "resolution": "512x512"},
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_parse_response_normal():
    text = "主体：一张游戏贴图资源\n细节：PNG格式，512分辨率"
    main, detail = _parse_response(text)
    assert "游戏贴图" in main
    assert "PNG" in detail


def test_build_user_content_without_image():
    content = _build_user_content(_make_input("/nonexistent/path.png"))
    types = [c.get("type", "") for c in content]
    assert "image_url" not in types
    assert "text" in types


def test_build_user_content_with_audio(tmp_path):
    audio = tmp_path / "coin.ogg"
    audio.write_bytes(b"OggS")
    content = _build_user_content(
        DescriptionInput(
            preview_path=str(tmp_path / "preview.webp"),
            resource_type="audio_file",
            preview_strategy="static",
            auxiliary_metadata={"format": "ogg"},
            llm_input_path=str(audio),
            llm_input_type="audio",
        )
    )
    types = [c.get("type", "") for c in content]
    assert "input_audio" in types
    assert "image_url" not in types
    audio_block = next(c for c in content if c.get("type") == "input_audio")
    assert audio_block["input_audio"]["format"] == "ogg"


def test_provider_requires_api_key():
    saved1 = os.environ.pop("KSPMAS_API_KEY", None)
    saved2 = os.environ.pop("KSC_API_KEY", None)
    try:
        with pytest.raises(ValueError, match="API Key"):
            KsyunLLMProvider(api_key="")
    finally:
        if saved1:
            os.environ["KSPMAS_API_KEY"] = saved1
        if saved2:
            os.environ["KSC_API_KEY"] = saved2


def test_ksyun_registered_in_factory():
    assert "ksyun" in LLMFactory.available_providers()
    assert "kspmas" in LLMFactory.available_providers()


def test_generate_description_success():
    provider = KsyunLLMProvider(api_key="ks-test")
    raw = "主体：一个高质量角色贴图\n细节：PNG格式，卡通渲染"

    with patch.object(provider, "_call_sync", return_value=raw):
        result = _run(provider.generate_description(_make_input()))

    assert isinstance(result, DescriptionResult)
    assert "角色贴图" in result.main_content
    assert "PNG" in result.detail_content
    assert result.prompt_version == PROMPT_VERSION


def test_generate_via_convenience_function():
    raw = "主体：一个3D模型\n细节：FBX格式"
    with patch(
        "ResourceProcessor.description.ksyun_llm_provider.KsyunLLMProvider._call_sync",
        return_value=raw,
    ):
        result = _run(generate_resource_description(_make_input(), provider_name="ksyun"))

    assert result.main_content == "一个3D模型"
    assert result.detail_content == "FBX格式"
