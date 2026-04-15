"""Tests for ResourceProcessor.description_generator module."""

import pytest

from ResourceProcessor.description.description_generator import (
    BaseMultiModalLLMProvider,
    DescriptionInput,
    DescriptionResult,
    LLMFactory,
    MockLLMProvider,
    generate_resource_description,
)
from ResourceProcessor.preview_metadata import PreviewInfo, PreviewStrategy


# ---------------------------------------------------------------------------
# DescriptionResult
# ---------------------------------------------------------------------------


def test_description_result_to_dict_roundtrip():
    original = DescriptionResult(
        main_content="主体描述",
        detail_content="细节描述",
        full_description="主体：主体描述\n细节：细节描述",
        prompt_version="prompt_v1",
        description_quality_score=0.95,
    )
    d = original.to_dict()
    restored = DescriptionResult.from_dict(d)
    assert restored == original
    assert isinstance(d, dict)
    assert d["main_content"] == "主体描述"
    assert d["description_quality_score"] == 0.95


# ---------------------------------------------------------------------------
# DescriptionInput
# ---------------------------------------------------------------------------


def test_description_input_to_prompt_context():
    inp = DescriptionInput(
        preview_path="/tmp/preview.webp",
        resource_type="image",
        preview_strategy="static",
        auxiliary_metadata={"width": 512, "height": 256, "format": "webp"},
    )
    ctx = inp.to_prompt_context()
    assert "image" in ctx
    assert "static" in ctx
    assert "512" in ctx
    assert "webp" in ctx


def test_description_input_resolves_audio_llm_fields():
    inp = DescriptionInput(
        preview_path="/tmp/preview.webp",
        resource_type="audio_file",
        preview_strategy="static",
        auxiliary_metadata={"format": "ogg"},
        llm_input_path="/tmp/coin.ogg",
        llm_input_type="audio",
    )
    assert inp.resolved_llm_input_path == "/tmp/coin.ogg"
    assert inp.resolved_llm_input_type == "audio"
    assert "LLM输入模态: audio" in inp.to_prompt_context()


# ---------------------------------------------------------------------------
# MockLLMProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mock_provider_returns_valid_result():
    provider = MockLLMProvider()
    inp = DescriptionInput(
        preview_path="/tmp/preview.webp",
        resource_type="image",
        preview_strategy="static",
        auxiliary_metadata={"format": "webp"},
    )
    result = await provider.generate_description(inp)
    assert isinstance(result, DescriptionResult)
    assert result.main_content != ""
    assert result.detail_content != ""
    assert result.full_description != ""
    assert result.prompt_version != ""


@pytest.mark.asyncio
async def test_mock_provider_output_format():
    provider = MockLLMProvider()
    inp = DescriptionInput(
        preview_path="/tmp/preview.webp",
        resource_type="3d_model",
        preview_strategy="gif",
        auxiliary_metadata={"format": "fbx"},
    )
    result = await provider.generate_description(inp)
    assert "主体：" in result.full_description
    assert "细节：" in result.full_description


@pytest.mark.asyncio
async def test_mock_provider_prompt_version():
    provider = MockLLMProvider()
    inp = DescriptionInput(
        preview_path="/tmp/preview.webp",
        resource_type="image",
        preview_strategy="static",
        auxiliary_metadata={},
    )
    result = await provider.generate_description(inp)
    assert result.prompt_version == "prompt_v1"


# ---------------------------------------------------------------------------
# LLMFactory
# ---------------------------------------------------------------------------


def test_llm_factory_register_and_create():
    class DummyProvider(BaseMultiModalLLMProvider):
        async def generate_description(self, input_data):
            return DescriptionResult(
                main_content="d",
                detail_content="d",
                full_description="d",
                prompt_version="v0",
            )

    LLMFactory.register("dummy_test", DummyProvider)
    provider = LLMFactory.create("dummy_test")
    assert isinstance(provider, DummyProvider)


def test_llm_factory_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        LLMFactory.create("nonexistent_provider_xyz")


def test_llm_factory_mock_registered_by_default():
    assert "mock" in LLMFactory.available_providers()
    provider = LLMFactory.create("mock")
    assert isinstance(provider, MockLLMProvider)


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_resource_description_convenience():
    inp = DescriptionInput(
        preview_path="/tmp/preview.webp",
        resource_type="image",
        preview_strategy="static",
        auxiliary_metadata={"format": "webp"},
    )
    result = await generate_resource_description(inp, provider_name="mock")
    assert isinstance(result, DescriptionResult)
    assert result.prompt_version == "prompt_v1"
    assert "image" in result.main_content


# ---------------------------------------------------------------------------
# DescriptionInput from PreviewInfo
# ---------------------------------------------------------------------------


def test_description_input_with_preview_info():
    preview = PreviewInfo(
        strategy=PreviewStrategy.STATIC,
        path="/tmp/preview.webp",
        format="webp",
        width=512,
        height=256,
        size=12345,
        renderer="pillow",
    )
    metadata = {}
    if preview.width is not None:
        metadata["width"] = preview.width
    if preview.height is not None:
        metadata["height"] = preview.height
    if preview.format is not None:
        metadata["format"] = preview.format
    if preview.size is not None:
        metadata["size"] = preview.size

    inp = DescriptionInput(
        preview_path=preview.path,
        resource_type="image",
        preview_strategy=preview.strategy.value,
        auxiliary_metadata=metadata,
    )
    assert inp.preview_path == "/tmp/preview.webp"
    assert inp.preview_strategy == "static"
    assert inp.auxiliary_metadata["width"] == 512
    assert inp.auxiliary_metadata["height"] == 256
    assert inp.auxiliary_metadata["format"] == "webp"
    ctx = inp.to_prompt_context()
    assert "512" in ctx
    assert "256" in ctx
