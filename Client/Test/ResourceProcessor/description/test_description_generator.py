"""Tests for ResourceProcessor.description_generator module."""

import pytest

from ResourceProcessor.description.description_generator import (
    BaseMultiModalLLMProvider,
    DescriptionInput,
    DescriptionResult,
    LLMFactory,
    MockLLMProvider,
    build_description_result,
    generate_resource_description,
    resolve_prompt_version,
)
from ResourceProcessor.description.description_request import build_description_request
from ResourceProcessor.description.prompt_config import (
    get_classification_user_prompt,
    get_description_user_prompt,
    get_user_prompt,
)
from ResourceProcessor.description.usage_classification import (
    UsageClassification,
    build_classification_response_schema,
    build_response_schema,
    parse_classification_response,
    parse_description_response,
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
        usage_space="2D",
        usage_category="界面",
        usage_subcategories=["图标"],
        usage_classification_reason="作为小型视觉符号直接用于界面。",
        usage_classification_version="game_visual_usage_v1.0",
    )
    d = original.to_dict()
    restored = DescriptionResult.from_dict(d)
    assert restored == original
    assert isinstance(d, dict)
    assert d["main_content"] == "主体描述"
    assert d["description_quality_score"] == 0.95
    assert d["usage_category"] == "界面"


def test_parse_json_response_with_usage_classification():
    raw = {
        "main_content": "一个技能图标资源",
        "detail_content": "适合放在技能栏中表示火焰攻击",
        "description_quality_score": 0.9,
        "classification": {
            "space": "2D",
            "category": "界面",
            "subcategories": ["图标"],
            "reason": "资源直接用途是屏幕空间中的小型功能符号。",
            "suggestion": None,
        },
    }
    import json

    main, detail, score, classification = parse_description_response(json.dumps(raw, ensure_ascii=False))
    assert main == "一个技能图标资源"
    assert "技能栏" in detail
    assert score == 0.9
    assert classification.space == "2D"
    assert classification.category == "界面"
    assert classification.subcategories == ["图标"]


def test_parse_json_response_embedded_in_text():
    raw = """好的，结果如下：
    {"main_content":"一个技能图标资源","detail_content":"火焰攻击图标","description_quality_score":0.8,"classification":{"space":"2D","category":"界面","subcategories":["图标"],"reason":"作为技能栏符号使用。","suggestion":null}}
    """

    main, detail, score, classification = parse_description_response(raw)
    assert main == "一个技能图标资源"
    assert detail == "火焰攻击图标"
    assert score == 0.8
    assert classification.category == "界面"


def test_parse_placeholder_auxiliary_classification():
    raw = {
        "main_content": "透明占位图，无可见像素内容",
        "detail_content": "该图片可作为 None 选项、空图层或资源组合占位使用。",
        "description_quality_score": 1.0,
        "classification": {
            "space": "2D",
            "category": "其他",
            "subcategories": ["占位/辅助资源"],
            "reason": "图片没有可见像素，直接用途是作为透明占位或空图层。",
            "suggestion": None,
        },
    }
    import json

    _main, _detail, _score, classification = parse_description_response(json.dumps(raw, ensure_ascii=False))

    assert classification.category == "其他"
    assert classification.subcategories == ["占位/辅助资源"]
    assert classification.suggestion is None


def test_parse_classification_removes_other_when_specific_subcategory_exists():
    raw = {
        "main_content": "角色部件动画帧",
        "detail_content": "用于角色动作表现。",
        "classification": {
            "space": "2D",
            "category": "角色",
            "subcategories": ["角色部件", "动作", "其他"],
            "reason": "角色部件和动作已能表达用途。",
            "suggestion": None,
        },
    }
    import json

    _main, _detail, _score, classification = parse_description_response(json.dumps(raw, ensure_ascii=False))

    assert classification.subcategories == ["角色部件", "动作"]


def test_parse_classification_only_response():
    raw = {
        "space": "2D",
        "category": "物件",
        "subcategories": ["道具", "其他"],
        "reason": "瓶子是可放入游戏世界的独立道具。",
        "suggestion": None,
    }
    import json

    classification = parse_classification_response(json.dumps(raw, ensure_ascii=False))

    assert classification.space == "2D"
    assert classification.category == "物件"
    assert classification.subcategories == ["道具"]


def test_response_schema_allows_placeholder_auxiliary_subcategory():
    schema = build_response_schema()
    classification = schema["properties"]["classification"]
    assert "其他" in classification["properties"]["category"]["enum"]
    assert "占位/辅助资源" in classification["properties"]["subcategories"]["items"]["enum"]


def test_classification_response_schema_is_classification_only():
    schema = build_classification_response_schema()

    assert "main_content" not in schema["properties"]
    assert schema["required"] == ["space", "category", "subcategories", "reason", "suggestion"]
    assert "占位/辅助资源" in schema["properties"]["subcategories"]["items"]["enum"]


def test_build_description_result_keeps_missing_classification_empty():
    inp = DescriptionInput(
        preview_path="",
        resource_type="audio_file",
        preview_strategy="static",
        auxiliary_metadata={"format": "wav"},
        llm_input_path="/tmp/Laser Skill Release.wav",
        llm_input_type="audio",
        title="Laser Skill Release.wav",
        resource_path="sfx/skill/Laser Skill Release.wav",
    )
    _main, _detail, _score, classification = parse_description_response(
        "主体：一段激光技能释放音效\n细节：适合战斗技能触发。"
    )

    result = build_description_result(
        inp,
        main_content=_main,
        detail_content=_detail,
        prompt_version="test",
        classification=classification,
    )

    assert result.usage_space == "不确定"
    assert result.usage_category == ""
    assert result.usage_subcategories == []
    assert "模型未返回" in result.usage_classification_reason


def test_build_description_result_cleans_redundant_other_subcategory():
    inp = DescriptionInput(
        preview_path="",
        resource_type="single_image",
        preview_strategy="static",
        auxiliary_metadata={},
    )

    result = build_description_result(
        inp,
        main_content="角色部件动画帧",
        detail_content="用于角色动作表现。",
        prompt_version="test",
        classification=UsageClassification(
            space="2D",
            category="角色",
            subcategories=["角色部件", "动作", "其他"],
            reason="角色部件和动作已能表达用途。",
        ),
    )

    assert result.usage_subcategories == ["角色部件", "动作"]


def test_build_description_result_rejects_empty_description():
    inp = DescriptionInput(
        preview_path="",
        resource_type="audio_file",
        preview_strategy="static",
        auxiliary_metadata={},
    )

    with pytest.raises(ValueError, match="empty description"):
        build_description_result(
            inp,
            main_content="",
            detail_content="",
            prompt_version="test",
        )


def test_user_prompt_keeps_description_and_type_prompts_separate(monkeypatch):
    monkeypatch.setenv("LLM_DESCRIPTION_PROMPT", "描述段：只写描述规则")
    monkeypatch.setenv("LLM_TYPE_PROMPT", "类型段：只能使用固定类型")

    prompt = get_user_prompt("资源上下文")

    assert "同时完成两件事" in prompt
    assert "输出格式要求" in prompt
    assert "描述提示词" in prompt
    assert "描述段：只写描述规则" in prompt
    assert "类型提示词" in prompt
    assert "类型段：只能使用固定类型" in prompt
    assert "可用分类规则" in prompt
    assert "游戏资源用途分类规则" in prompt
    assert "`角色`" in prompt
    assert "不要同时选择 `其他`" in prompt
    assert "陷阱、尖刺、开关、门控、传送器" in prompt
    assert "`物件-机关`" in prompt
    assert "`right`、`left`、`appear`" in prompt
    assert "不要仅凭这些词把资源归为 `角色-动作`" in prompt
    assert "数字、徽章、笔记、标记、圆形符号、状态符号" in prompt
    assert "独立物件的打开、关闭、出现、消失、数量变化或状态帧" in prompt
    assert "只有画面主体主要是粒子、光效、爆炸、拖尾、冲击" in prompt
    assert "`其他-占位/辅助资源`" in prompt
    assert "其他颜色的纯色图块交给模型" in prompt
    assert "{context}" not in prompt
    assert prompt.rstrip().endswith("资源上下文")


def test_split_prompts_keep_description_and_classification_contexts_separate(monkeypatch):
    monkeypatch.setenv("LLM_DESCRIPTION_PROMPT", "描述段：只写描述规则")
    monkeypatch.setenv("LLM_TYPE_PROMPT", "类型段：只能使用固定类型")

    description_prompt = get_description_user_prompt("资源上下文")
    classification_prompt = get_classification_user_prompt("描述和 resource_type")

    assert description_prompt == "描述段：只写描述规则\n\n资源上下文：\n资源上下文"
    assert "描述提示词：" not in description_prompt
    assert "类型段：只能使用固定类型" not in description_prompt
    assert "可用分类规则" not in description_prompt

    assert "只完成用途分类" in classification_prompt
    assert "不要改写或重新生成资源描述" in classification_prompt
    assert "space、category、subcategories、reason、suggestion" in classification_prompt
    assert "资源描述和 resource_type" in classification_prompt
    assert "资源元数据" not in classification_prompt
    assert "描述段：只写描述规则" not in classification_prompt
    assert "类型段：只能使用固定类型" in classification_prompt
    assert "可用分类规则" in classification_prompt


def test_description_request_uses_pack_prompt_and_disables_media(monkeypatch):
    monkeypatch.setenv("LLM_PACK_DESCRIPTION_PROMPT", "包描述规则：总结子资源")
    inp = DescriptionInput(
        preview_path="/tmp/preview.webp",
        resource_type="pack",
        preview_strategy="contact_sheet",
        auxiliary_metadata={"format": "png"},
        prompt_context_override="包上下文",
        description_prompt_env="LLM_PACK_DESCRIPTION_PROMPT",
        attach_llm_media=False,
        prompt_version_tag="pack_child_embedding_summary_v1",
    )

    request = build_description_request(inp)

    assert request.context == "包上下文"
    assert request.llm_input_paths == []
    assert request.user_prompt == "包描述规则：总结子资源\n\n资源上下文：\n包上下文"
    assert resolve_prompt_version("ksyun_v2_split", inp) == (
        "ksyun_v2_split+pack_child_embedding_summary_v1"
    )


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


@pytest.mark.asyncio
async def test_transparent_image_uses_programmatic_description(tmp_path):
    from PIL import Image

    image = tmp_path / "none.png"
    Image.new("RGBA", (16, 16), (0, 0, 0, 0)).save(image)
    inp = DescriptionInput(
        preview_path=str(image),
        source_file_path=str(image),
        resource_type="single_image",
        preview_strategy="static",
        auxiliary_metadata={"format": "png"},
        title="_None.png",
        resource_path="characters/15Background/_None.png",
    )

    result = await generate_resource_description(inp, provider_name="missing_provider")

    assert result.prompt_version == "programmatic_low_information_v1"
    assert "透明占位图" in result.main_content
    assert result.usage_category == "其他"
    assert result.usage_subcategories == ["占位/辅助资源"]
    assert result.usage_classification_suggestion is None


@pytest.mark.asyncio
async def test_black_image_uses_programmatic_description(tmp_path):
    from PIL import Image

    image = tmp_path / "black.png"
    Image.new("RGBA", (16, 16), (0, 0, 0, 255)).save(image)
    inp = DescriptionInput(
        preview_path=str(image),
        source_file_path=str(image),
        resource_type="single_image",
        preview_strategy="static",
        auxiliary_metadata={"format": "png"},
        title="black.png",
    )

    result = await generate_resource_description(inp, provider_name="missing_provider")

    assert "近似全黑图块" in result.main_content
    assert "#000000" in result.main_content
    assert result.usage_category == "其他"
    assert result.usage_subcategories == ["占位/辅助资源"]


@pytest.mark.asyncio
async def test_white_tileset_uses_environment_terrain(tmp_path):
    from PIL import Image

    image = tmp_path / "white_tileset.png"
    Image.new("RGBA", (16, 16), (255, 255, 255, 255)).save(image)
    inp = DescriptionInput(
        preview_path=str(image),
        source_file_path=str(image),
        resource_type="tileset",
        preview_strategy="static",
        auxiliary_metadata={"format": "png"},
        title="white_tileset.png",
    )

    result = await generate_resource_description(inp, provider_name="missing_provider")

    assert "近似全白瓦片集" in result.main_content
    assert result.usage_category == "环境"
    assert result.usage_subcategories == ["地形"]


@pytest.mark.asyncio
async def test_red_image_still_uses_provider(tmp_path):
    from PIL import Image

    image = tmp_path / "red.png"
    Image.new("RGBA", (16, 16), (255, 0, 0, 255)).save(image)
    inp = DescriptionInput(
        preview_path=str(image),
        source_file_path=str(image),
        resource_type="single_image",
        preview_strategy="static",
        auxiliary_metadata={"format": "png"},
    )

    result = await generate_resource_description(inp, provider_name="mock")

    assert result.prompt_version == "prompt_v1"


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
