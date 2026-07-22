"""Tests for Ksyun LLM provider — network calls are mocked."""

import asyncio
import os
import threading
import time
from unittest.mock import patch

import pytest

os.environ.setdefault("KSPMAS_API_KEY", "ks-test-key")

from ResourceProcessor.description.description_generator import (  # noqa: E402
    DescriptionInput,
    DescriptionResult,
    generate_resource_description,
)
from ResourceProcessor.description.ksyun_llm_provider import (  # noqa: E402
    DescriptionRefusalResponse,
    InvalidDescriptionResponse,
    KsyunLLMProvider,
    LLMFactory,
    PROMPT_VERSION,
    _build_classification_user_content,
    _build_user_content,
    _encode_image_data_uri,
    _prepare_audio_input,
    _parse_description_response_strict,
    _parse_response,
    _run_in_daemon_thread,
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


def test_parse_description_response_strict_accepts_valid_contract():
    main, detail, score = _parse_description_response_strict(
        '{"main_content":"像素水晶图标","detail_content":"蓝色透明晶体。",'
        '"description_quality_score":0.8}'
    )
    assert main == "像素水晶图标"
    assert detail == "蓝色透明晶体。"
    assert score == 0.8


def test_parse_description_response_strict_allows_missing_quality_score():
    main, detail, score = _parse_description_response_strict(
        '{"main_content":"像素水晶图标","detail_content":"蓝色透明晶体。"}'
    )
    assert main == "像素水晶图标"
    assert detail == "蓝色透明晶体。"
    assert score is None


def test_parse_description_response_strict_uses_json_after_think_close_tag():
    response = (
        '{"main_content":"草稿主体","detail_content":"草稿细节"}'
        '</think>'
        '{"main_content":"最终主体","detail_content":"最终细节"}'
    )

    main, detail, score = _parse_description_response_strict(response)

    assert main == "最终主体"
    assert detail == "最终细节"
    assert score is None


def test_parse_description_response_strict_rejects_invalid_json_after_think_close_tag():
    response = (
        '{"main_content":"草稿主体","detail_content":"草稿细节"}'
        '</think>not-json'
    )

    with pytest.raises(InvalidDescriptionResponse, match="not valid JSON") as caught:
        _parse_description_response_strict(response)

    assert caught.value.raw_response == response


@pytest.mark.parametrize(
    "response, message",
    [
        ('{"main_content":"主体","description_quality_score":0.5}', "missing required fields"),
        ('{"main_content":"主体","detail_content":"细节","description_quality_score":2}', "between 0 and 1"),
        ('{"main_content":"","detail_content":"细节","description_quality_score":null}', "main_content"),
        ('{"main_content":"主体","detail_content":"细节","description_quality_score":null,"error":"x"}', "unexpected fields"),
    ],
)
def test_parse_description_response_strict_rejects_invalid_contract(response, message):
    with pytest.raises(InvalidDescriptionResponse, match=message):
        _parse_description_response_strict(response)


def test_generate_description_text_rejects_plain_text_response():
    provider = KsyunLLMProvider(api_key="ks-test")
    with patch.object(
        provider,
        "_call_sync",
        return_value="The request was rejected because it was considered high risk",
    ), pytest.raises(DescriptionRefusalResponse, match="safety policy") as caught:
        _run(provider.generate_description_text(_make_input()))
    assert caught.value.raw_response == (
        "The request was rejected because it was considered high risk"
    )


@pytest.mark.parametrize(
    "response",
    [
        "The request was rejected because it was considered high risk",
        "当前输入图片内容存在敏感信息，请更换图片",
        ('{"main_content":"The request was rejected because it was considered high risk",'
         '"detail_content":"The request was rejected because it was considered high risk"}'),
    ],
)
def test_parse_description_response_strict_classifies_safety_refusal(response):
    with pytest.raises(DescriptionRefusalResponse, match="safety policy") as caught:
        _parse_description_response_strict(response)

    assert caught.value.raw_response == response


def test_build_user_content_without_image():
    content = _build_user_content(_make_input("/nonexistent/path.png"))
    types = [c.get("type", "") for c in content]
    assert "image_url" not in types
    assert "text" in types
    text = next(c["text"] for c in content if c.get("type") == "text")
    assert "资源上下文：\n资源类型: image" in text
    assert "预览策略: static" in text
    assert "format: png" in text
    assert "resolution: 512x512" in text
    assert "描述提示词：" not in text
    assert "可用分类规则" not in text


@pytest.mark.parametrize(
    "file_name, expected_prefix",
    [
        ("preview.png", "data:image/png;base64,"),
        ("preview.JPG", "data:image/jpeg;base64,"),
        ("preview.webp", "data:image/webp;base64,"),
        ("preview.gif", "data:image/gif;base64,"),
        ("preview.bmp", "data:image/bmp;base64,"),
        ("preview.unknown", "data:application/octet-stream;base64,"),
    ],
)
def test_encode_image_data_uri_uses_explicit_mime_mapping(tmp_path, file_name, expected_prefix):
    image_path = tmp_path / file_name
    image_path.write_bytes(b"image-bytes")

    assert _encode_image_data_uri(str(image_path)).startswith(expected_prefix)


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


def test_large_audio_uses_compacted_sample(tmp_path, monkeypatch):
    source = tmp_path / "Track Industrial.wav"
    compacted = tmp_path / "sample.mp3"
    source.write_bytes(b"0" * 128)
    compacted.write_bytes(b"1" * 16)
    monkeypatch.setenv("KSPMAS_MAX_AUDIO_INPUT_BYTES", "32")
    monkeypatch.setenv("KSPMAS_AUDIO_SAMPLE_SECONDS", "30")
    monkeypatch.setattr(
        "ResourceProcessor.description.ksyun_llm_provider._compact_audio_for_llm",
        lambda path, max_bytes: compacted,
    )

    audio_input, note = _prepare_audio_input(str(source))

    assert audio_input is not None
    assert audio_input["format"] == "mp3"
    assert "超过请求体保护阈值" in note
    assert "已截取前 30 秒" in note


def test_large_audio_falls_back_to_text_context(tmp_path, monkeypatch):
    source = tmp_path / "Track Industrial.wav"
    source.write_bytes(b"0" * 128)
    monkeypatch.setenv("KSPMAS_MAX_AUDIO_INPUT_BYTES", "32")
    monkeypatch.setattr(
        "ResourceProcessor.description.ksyun_llm_provider._compact_audio_for_llm",
        lambda path, max_bytes: None,
    )

    content = _build_user_content(
        DescriptionInput(
            preview_path=str(tmp_path / "preview.webp"),
            resource_type="audio_file",
            preview_strategy="static",
            auxiliary_metadata={"format": "wav"},
            llm_input_path=str(source),
            llm_input_type="audio",
            title=source.name,
            resource_path=f"Music/WAV/{source.name}",
        )
    )

    assert [item.get("type") for item in content] == ["text"]
    text = content[0]["text"]
    assert "音频输入处理" in text
    assert "未附加音频本体" in text
    assert "Music/WAV/Track Industrial.wav" in text


def test_build_classification_user_content_is_text_only():
    content = _build_classification_user_content(
        DescriptionInput(
            preview_path="/tmp/preview.webp",
            resource_type="single_image",
            preview_strategy="static",
            auxiliary_metadata={"format": "png"},
            title="door key.png",
            resource_path="items/door_key.png",
        ),
        "像素风格的钥匙图标",
        "适合作为背包或道具栏中的钥匙资源。",
    )

    assert [item.get("type") for item in content] == ["text"]
    text = content[0]["text"]
    assert "只完成用途分类" in text
    assert "已生成资源描述" in text
    assert "像素风格的钥匙图标" in text
    assert "resource_type: single_image" in text
    assert "可用分类规则" in text
    assert "door key.png" not in text
    assert "items/door_key.png" not in text
    assert "预览策略" not in text
    assert "format: png" not in text


def test_build_user_content_with_multiple_audio_inputs(tmp_path):
    audio_a = tmp_path / "click_001.ogg"
    audio_b = tmp_path / "close_001.ogg"
    audio_a.write_bytes(b"OggS")
    audio_b.write_bytes(b"OggS")
    content = _build_user_content(
        DescriptionInput(
            preview_path=str(tmp_path / "preview.webp"),
            resource_type="pack",
            preview_strategy="static",
            auxiliary_metadata={"format": "ogg"},
            llm_input_path=str(audio_a),
            llm_input_paths=[str(audio_a), str(audio_b)],
            llm_input_type="audio",
        )
    )
    audio_blocks = [c for c in content if c.get("type") == "input_audio"]
    assert len(audio_blocks) == 2
    assert all(block["input_audio"]["format"] == "ogg" for block in audio_blocks)


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


def test_provider_uses_timeout_env(monkeypatch):
    monkeypatch.setenv("KSPMAS_LLM_TIMEOUT", "180")

    provider = KsyunLLMProvider(api_key="ks-test")
    explicit = KsyunLLMProvider(api_key="ks-test", timeout=12)

    assert provider._timeout == 180
    assert explicit._timeout == 12


def test_ksyun_registered_in_factory():
    assert "ksyun" in LLMFactory.available_providers()
    assert "kspmas" in LLMFactory.available_providers()


def test_generate_description_success():
    provider = KsyunLLMProvider(api_key="ks-test")
    raw_description = (
        '{"main_content":"一个高质量角色贴图",'
        '"detail_content":"PNG格式，卡通渲染",'
        '"description_quality_score":0.8}'
    )
    raw_classification = (
        '{"space":"2D","category":"角色","subcategories":["人物"],'
        '"reason":"描述说明资源用于角色表现。","suggestion":null}'
    )

    with patch.object(provider, "_call_sync", return_value=raw_description) as desc_call, patch.object(
        provider,
        "_call_classification_sync",
        return_value=raw_classification,
    ) as class_call:
        result = _run(provider.generate_description(_make_input()))

    assert isinstance(result, DescriptionResult)
    assert "角色贴图" in result.main_content
    assert "PNG" in result.detail_content
    assert result.prompt_version == PROMPT_VERSION
    assert result.usage_category == "角色"
    assert result.usage_subcategories == ["人物"]
    desc_call.assert_called_once()
    class_call.assert_called_once()


def test_daemon_thread_bridge_cancels_without_waiting():
    started = threading.Event()
    release = threading.Event()

    def blocked_call():
        started.set()
        release.wait(5)
        return "late-result"

    async def scenario():
        task = asyncio.create_task(_run_in_daemon_thread(blocked_call))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        task.cancel()
        began = time.perf_counter()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.perf_counter() - began < 0.2
        release.set()

    _run(scenario())


def test_generate_via_convenience_function():
    raw_description = (
        '{"main_content":"一个3D模型","detail_content":"FBX格式",'
        '"description_quality_score":0.7}'
    )
    raw_classification = (
        '{"space":"3D","category":"物件","subcategories":["摆件"],'
        '"reason":"描述说明资源是可放入场景的三维对象。","suggestion":null}'
    )
    with patch(
        "ResourceProcessor.description.ksyun_llm_provider.KsyunLLMProvider._call_sync",
        return_value=raw_description,
    ), patch(
        "ResourceProcessor.description.ksyun_llm_provider.KsyunLLMProvider._call_classification_sync",
        return_value=raw_classification,
    ):
        result = _run(generate_resource_description(_make_input(), provider_name="ksyun"))

    assert result.main_content == "一个3D模型"
    assert result.detail_content == "FBX格式"
    assert result.usage_space == "3D"
    assert result.usage_category == "物件"
