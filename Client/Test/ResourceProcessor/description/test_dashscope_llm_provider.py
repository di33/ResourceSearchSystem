"""Tests for DashScope LLM provider — all dashscope calls are mocked."""

import asyncio
import os
import types
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DASHSCOPE_API_KEY", "sk-test-key-for-unit-tests")

from ResourceProcessor.description.dashscope_llm_provider import (
    DashScopeLLMProvider,
    LLMFactory,
    PROMPT_VERSION,
    _build_user_content,
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
        resource_type="image",
        preview_strategy="static",
        auxiliary_metadata={"format": "png", "resolution": "512x512"},
    )


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

def test_parse_response_normal():
    text = "主体：这是一张测试图片资源，用于游戏开发。\n细节：PNG格式，分辨率512，色彩丰富。"
    main, detail = _parse_response(text)
    assert main == "这是一张测试图片资源，用于游戏开发。"
    assert detail == "PNG格式，分辨率512，色彩丰富。"


def test_parse_response_colon_variants():
    text = "主体:半角冒号也能解析\n细节:同样可以"
    main, detail = _parse_response(text)
    assert main == "半角冒号也能解析"
    assert detail == "同样可以"


def test_parse_response_fallback_no_prefix():
    text = "这行没有前缀\n第二行也没有"
    main, detail = _parse_response(text)
    assert main == "这行没有前缀"
    assert detail == "第二行也没有"


# ---------------------------------------------------------------------------
# _build_user_content
# ---------------------------------------------------------------------------

def test_build_user_content_with_existing_file(tmp_path):
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG")
    content = _build_user_content(_make_input(str(img)))
    assert any("image" in item for item in content)
    assert any("text" in item for item in content)


def test_build_user_content_missing_file():
    content = _build_user_content(_make_input("/nonexistent/file.png"))
    assert all("image" not in item for item in content)
    assert any("text" in item for item in content)


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
    assert any("audio" in item for item in content)
    assert all("image" not in item for item in content)
    assert any("text" in item for item in content)


# ---------------------------------------------------------------------------
# DashScopeLLMProvider construction
# ---------------------------------------------------------------------------

def test_provider_requires_api_key():
    saved = os.environ.pop("DASHSCOPE_API_KEY", None)
    try:
        with pytest.raises(ValueError, match="API Key"):
            DashScopeLLMProvider(api_key="")
    finally:
        if saved:
            os.environ["DASHSCOPE_API_KEY"] = saved


def test_provider_accepts_explicit_key():
    p = DashScopeLLMProvider(api_key="sk-explicit")
    assert p._api_key == "sk-explicit"


# ---------------------------------------------------------------------------
# Factory registration
# ---------------------------------------------------------------------------

def test_dashscope_registered_in_factory():
    assert "dashscope" in LLMFactory.available_providers()
    assert "qwen-vl" in LLMFactory.available_providers()


# ---------------------------------------------------------------------------
# generate_description (mocked API call)
# ---------------------------------------------------------------------------

def _mock_response(text: str):
    resp = MagicMock()
    resp.status_code = 200
    resp.output.choices = [
        MagicMock(message=MagicMock(content=[{"text": text}]))
    ]
    return resp


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_generate_description_success():
    raw = "主体：这是一个高质量图片素材，适用于游戏UI设计，色彩鲜明且构图规整。\n细节：PNG格式，分辨率512x512，采用卡通渲染风格，色调偏暖，细节丰富。"
    provider = DashScopeLLMProvider(api_key="sk-test")

    with patch.object(provider, "_call_sync", return_value=raw):
        result = _run(provider.generate_description(_make_input()))

    assert isinstance(result, DescriptionResult)
    assert "图片素材" in result.main_content
    assert "PNG" in result.detail_content
    assert result.full_description.startswith("主体：")
    assert result.prompt_version == PROMPT_VERSION


def test_generate_description_api_error():
    provider = DashScopeLLMProvider(api_key="sk-test")

    with patch.object(provider, "_call_sync", side_effect=RuntimeError("API 调用失败")):
        with pytest.raises(RuntimeError, match="API 调用失败"):
            _run(provider.generate_description(_make_input()))


def test_generate_via_convenience_function():
    raw = "主体：一个模型资源\n细节：FBX格式"

    with patch(
        "ResourceProcessor.description.dashscope_llm_provider.DashScopeLLMProvider._call_sync",
        return_value=raw,
    ):
        result = _run(generate_resource_description(_make_input(), provider_name="dashscope"))

    assert result.main_content == "一个模型资源"
    assert result.detail_content == "FBX格式"
