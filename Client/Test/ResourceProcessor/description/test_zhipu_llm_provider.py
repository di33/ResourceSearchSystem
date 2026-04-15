"""Tests for ZhipuAI LLM provider — all zhipuai calls are mocked."""

import asyncio
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("ZHIPUAI_API_KEY", "test-key-for-unit-tests")

from ResourceProcessor.description.zhipu_llm_provider import (
    PROMPT_VERSION,
    ZhipuLLMProvider,
    LLMFactory,
    _build_user_content_text,
    _build_user_content_vision,
    _encode_image_base64,
    _parse_response,
)
from ResourceProcessor.description.description_generator import (
    DescriptionInput,
    DescriptionResult,
    generate_resource_description,
)


def _make_input(preview_path: str = "fake.png") -> DescriptionInput:
    return DescriptionInput(
        preview_path=preview_path,
        resource_type="model",
        preview_strategy="gif",
        auxiliary_metadata={"format": "fbx", "polygons": "12000"},
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

def test_parse_response_normal():
    text = "主体：一个FBX角色模型，适用于RPG游戏开发。\n细节：面数12000，带骨骼绑定。"
    main, detail = _parse_response(text)
    assert "FBX" in main
    assert "12000" in detail


def test_parse_response_half_width_colon():
    text = "主体:半角冒号\n细节:也能正确解析"
    main, detail = _parse_response(text)
    assert main == "半角冒号"
    assert detail == "也能正确解析"


# ---------------------------------------------------------------------------
# _encode_image_base64
# ---------------------------------------------------------------------------

def test_encode_image_existing(tmp_path):
    f = tmp_path / "test.png"
    f.write_bytes(b"\x89PNG\r\n")
    result = _encode_image_base64(str(f))
    assert result is not None
    assert len(result) > 0


def test_encode_image_nonexistent():
    assert _encode_image_base64("/no/such/file.png") is None


# ---------------------------------------------------------------------------
# _build_user_content
# ---------------------------------------------------------------------------

def test_build_vision_content_with_image(tmp_path):
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG")
    content = _build_user_content_vision(_make_input(str(img)))
    types = [item.get("type") for item in content]
    assert "image_url" in types
    assert "text" in types


def test_build_vision_content_without_image():
    content = _build_user_content_vision(_make_input("/nonexistent.png"))
    types = [item.get("type") for item in content]
    assert "image_url" not in types
    assert "text" in types


def test_build_vision_content_with_audio_falls_back_to_text_only(tmp_path):
    audio = tmp_path / "coin.ogg"
    audio.write_bytes(b"OggS")
    content = _build_user_content_vision(
        DescriptionInput(
            preview_path=str(tmp_path / "preview.webp"),
            resource_type="audio_file",
            preview_strategy="static",
            auxiliary_metadata={"format": "ogg"},
            llm_input_path=str(audio),
            llm_input_type="audio",
        )
    )
    types = [item.get("type") for item in content]
    assert "image_url" not in types
    assert "text" in types


def test_build_text_content():
    text = _build_user_content_text(_make_input())
    assert "model" in text or "资源类型" in text
    assert "format" in text


# ---------------------------------------------------------------------------
# ZhipuLLMProvider construction
# ---------------------------------------------------------------------------

def test_provider_requires_api_key():
    saved = os.environ.pop("ZHIPUAI_API_KEY", None)
    try:
        with pytest.raises(ValueError, match="API Key"):
            ZhipuLLMProvider(api_key="")
    finally:
        if saved:
            os.environ["ZHIPUAI_API_KEY"] = saved


def test_provider_default_model():
    p = ZhipuLLMProvider(api_key="sk-test")
    assert p._model == "glm-5.1"
    assert not p.is_vision_model


def test_provider_vision_model():
    p = ZhipuLLMProvider(model="glm-4v-plus", api_key="sk-test")
    assert p.is_vision_model


# ---------------------------------------------------------------------------
# Factory registration
# ---------------------------------------------------------------------------

def test_zhipu_registered_in_factory():
    assert "zhipu" in LLMFactory.available_providers()
    assert "glm" in LLMFactory.available_providers()


# ---------------------------------------------------------------------------
# generate_description (mocked API call)
# ---------------------------------------------------------------------------

def test_generate_description_text_model():
    raw = "主体：这是一个高精度FBX角色模型，适用于RPG类游戏开发，包含完整骨骼绑定。\n细节：面数12000，采用PBR材质，支持多套动画状态，纹理分辨率2048x2048。"
    provider = ZhipuLLMProvider(model="glm-5.1", api_key="sk-test")

    with patch.object(provider, "_call_sync", return_value=raw):
        result = _run(provider.generate_description(_make_input()))

    assert isinstance(result, DescriptionResult)
    assert "FBX" in result.main_content
    assert "PBR" in result.detail_content
    assert result.prompt_version == PROMPT_VERSION


def test_generate_description_vision_model():
    raw = "主体：一张游戏UI贴图资源\n细节：PNG格式，卡通风格"
    provider = ZhipuLLMProvider(model="glm-4v-plus", api_key="sk-test")

    with patch.object(provider, "_call_sync", return_value=raw):
        result = _run(provider.generate_description(_make_input()))

    assert "UI" in result.main_content


def test_generate_description_api_error():
    provider = ZhipuLLMProvider(api_key="sk-test")

    with patch.object(provider, "_call_sync", side_effect=RuntimeError("API error")):
        with pytest.raises(RuntimeError, match="API error"):
            _run(provider.generate_description(_make_input()))


def test_generate_via_convenience_function():
    raw = "主体：一个3D模型\n细节：FBX格式带动画"

    with patch(
        "ResourceProcessor.description.zhipu_llm_provider.ZhipuLLMProvider._call_sync",
        return_value=raw,
    ):
        result = _run(generate_resource_description(_make_input(), provider_name="zhipu"))

    assert result.main_content == "一个3D模型"
    assert result.detail_content == "FBX格式带动画"
