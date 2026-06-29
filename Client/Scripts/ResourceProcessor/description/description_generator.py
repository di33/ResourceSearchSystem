"""LLM-based description generation: provider interface, factory, and mock."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ResourceProcessor.description.usage_classification import (
    CLASSIFICATION_RULE_VERSION,
    UsageClassification,
    ensure_usage_classification,
)


MAX_DESCRIPTION_IMAGE_INPUTS = 6
MAX_DESCRIPTION_AUDIO_INPUTS = 8
LOW_INFORMATION_PROMPT_VERSION = "programmatic_low_information_v1"
_VISIBLE_ALPHA_THRESHOLD = 8
_LOW_INFORMATION_RATIO = 0.98
_BLACK_RGB_MAX = 8
_WHITE_RGB_MIN = 247
_LOW_INFORMATION_SAMPLE_SIZE = 512
_TILESET_TYPES = {"tileset", "tiled_tileset"}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DescriptionResult:
    main_content: str
    detail_content: str
    full_description: str
    prompt_version: str
    description_quality_score: Optional[float] = None
    usage_space: str = ""
    usage_category: str = ""
    usage_subcategories: list[str] = field(default_factory=list)
    usage_classification_reason: str = ""
    usage_classification_suggestion: dict | None = None
    usage_classification_version: str = CLASSIFICATION_RULE_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> DescriptionResult:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class DescriptionInput:
    preview_path: str
    resource_type: str
    preview_strategy: str
    auxiliary_metadata: dict
    preview_paths: list[str] = field(default_factory=list)
    llm_input_path: str = ""
    llm_input_paths: list[str] = field(default_factory=list)
    llm_input_type: str = "image"
    title: str = ""
    pack_name: str = ""
    resource_path: str = ""
    source: str = ""
    source_tags: Optional[list[str]] = None
    source_description: str = ""
    category: str = ""
    member_count: int = 0
    asset_formats: Optional[list[str]] = None
    source_file_path: str = ""
    preview_mode: str = ""
    preview_confidence: str = ""
    missing_file_ratio: float = 0.0

    @property
    def resolved_llm_input_path(self) -> str:
        return self.llm_input_path or self.preview_path

    @property
    def resolved_llm_input_paths(self) -> list[str]:
        primary = self.resolved_llm_input_path
        if self.resolved_llm_input_type == "audio":
            paths: list[str] = []
            for path in [primary, *self.llm_input_paths]:
                if not path or path in paths:
                    continue
                paths.append(path)
                if len(paths) >= MAX_DESCRIPTION_AUDIO_INPUTS:
                    break
            return paths

        paths: list[str] = []
        for path in [primary, *self.preview_paths]:
            if not path or path in paths:
                continue
            paths.append(path)
            if len(paths) >= MAX_DESCRIPTION_IMAGE_INPUTS:
                break
        return paths

    @property
    def resolved_llm_input_type(self) -> str:
        return (self.llm_input_type or "image").strip().lower()

    @staticmethod
    def _stringify(value) -> str:
        if isinstance(value, (list, tuple, set)):
            return ", ".join(str(v) for v in value if v not in ("", None))
        return str(value)

    def to_prompt_context(self) -> str:
        """将输入转为可嵌入 Prompt 的上下文文本。"""
        parts = [f"资源类型: {self.resource_type}"]
        if self.resolved_llm_input_type and self.resolved_llm_input_path:
            parts.append(f"LLM输入模态: {self.resolved_llm_input_type}")
        if self.title:
            parts.append(f"资源标题: {self.title}")
        if self.pack_name:
            parts.append(f"资源包: {self.pack_name}")
        if self.resource_path:
            parts.append(f"资源路径: {self.resource_path}")
        if self.source:
            parts.append(f"来源站点: {self.source}")
        if self.category:
            parts.append(f"来源分类: {self.category}")
        if self.source_tags:
            parts.append(f"来源标签: {self._stringify(self.source_tags)}")
        if self.source_description:
            parts.append(f"来源描述: {self.source_description}")
        if self.member_count:
            parts.append(f"成员文件数: {self.member_count}")
        if self.asset_formats:
            parts.append(f"文件格式分布: {self._stringify(self.asset_formats)}")
        parts.append(f"预览策略: {self.preview_strategy}")
        if self.preview_mode:
            parts.append(f"预览模式: {self.preview_mode}")
        if self.preview_confidence:
            parts.append(f"预览置信度: {self.preview_confidence}")
        if self.preview_paths:
            parts.append(f"预览图数量: {len(self.preview_paths)}")
            if len(self.preview_paths) > MAX_DESCRIPTION_IMAGE_INPUTS:
                parts.append(f"描述输入图数量上限: {MAX_DESCRIPTION_IMAGE_INPUTS}")
        if self.resolved_llm_input_type == "audio" and self.resolved_llm_input_paths:
            parts.append(f"音频输入数量: {len(self.resolved_llm_input_paths)}")
            if len(self.llm_input_paths) > MAX_DESCRIPTION_AUDIO_INPUTS:
                parts.append(f"描述输入音频数量上限: {MAX_DESCRIPTION_AUDIO_INPUTS}")
        if self.missing_file_ratio:
            parts.append(f"缺失文件比例: {self.missing_file_ratio:.2f}")
        for k, v in self.auxiliary_metadata.items():
            if v in ("", None, [], {}):
                continue
            parts.append(f"{k}: {self._stringify(v)}")
        return "\n".join(parts)


def _clean_description_part(value: str, label: str) -> str:
    text = str(value or "").strip()
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = text.removeprefix(f"{label}：").removeprefix(f"{label}:").strip()
    return text


def build_description_result(
    input_data: DescriptionInput,
    *,
    main_content: str,
    detail_content: str,
    prompt_version: str,
    description_quality_score: Optional[float] = None,
    classification: UsageClassification | None = None,
) -> DescriptionResult:
    """Normalize provider output, validate text, and keep model classification."""
    main = _clean_description_part(main_content, "主体")
    detail = _clean_description_part(detail_content, "细节")
    if main and not detail:
        detail = main
    elif detail and not main:
        main = detail
    if not main or not detail:
        label = input_data.title or input_data.resource_path or input_data.resource_type
        raise ValueError(f"LLM returned empty description for resource: {label}")

    final_classification = ensure_usage_classification(
        classification or UsageClassification(),
        resource_type=input_data.resource_type,
        llm_input_type=input_data.resolved_llm_input_type,
        title=input_data.title,
        resource_path=input_data.resource_path,
        source_category=input_data.category,
        source_tags=input_data.source_tags,
        source_description=input_data.source_description,
        main_content=main,
        detail_content=detail,
        auxiliary_metadata=input_data.auxiliary_metadata,
    )

    return DescriptionResult(
        main_content=main,
        detail_content=detail,
        full_description=f"主体：{main}\n细节：{detail}",
        prompt_version=prompt_version,
        description_quality_score=description_quality_score,
        **final_classification.to_result_kwargs(),
    )


def _hex_color(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _mean_rgb(pixels: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    if not pixels:
        return (0, 0, 0)
    total_r = sum(pixel[0] for pixel in pixels)
    total_g = sum(pixel[1] for pixel in pixels)
    total_b = sum(pixel[2] for pixel in pixels)
    count = len(pixels)
    return (
        round(total_r / count),
        round(total_g / count),
        round(total_b / count),
    )


def _classify_low_information_pixels(path: str) -> tuple[str, str] | None:
    if not path or not Path(path).is_file():
        return None
    try:
        from PIL import Image
    except Exception:
        return None

    try:
        with Image.open(path) as image:
            image = image.convert("RGBA")
            image.thumbnail(
                (_LOW_INFORMATION_SAMPLE_SIZE, _LOW_INFORMATION_SAMPLE_SIZE),
                Image.Resampling.NEAREST,
            )
            pixel_data = getattr(image, "get_flattened_data", image.getdata)()
            pixels = list(pixel_data)
    except Exception:
        return None

    visible = [(r, g, b) for r, g, b, a in pixels if a > _VISIBLE_ALPHA_THRESHOLD]
    if not visible:
        return ("transparent", "#000000")

    black = [pixel for pixel in visible if max(pixel) <= _BLACK_RGB_MAX]
    if len(black) / len(visible) >= _LOW_INFORMATION_RATIO:
        return ("black", _hex_color(_mean_rgb(visible)))

    white = [pixel for pixel in visible if min(pixel) >= _WHITE_RGB_MIN]
    if len(white) / len(visible) >= _LOW_INFORMATION_RATIO:
        return ("white", _hex_color(_mean_rgb(visible)))

    return None


def _low_information_candidate_paths(input_data: DescriptionInput) -> list[str]:
    paths: list[str] = []
    for path in [
        input_data.source_file_path,
        input_data.resolved_llm_input_path,
        input_data.preview_path,
        *input_data.resolved_llm_input_paths,
    ]:
        if path and path not in paths:
            paths.append(path)
    return paths


def _low_information_classification(
    input_data: DescriptionInput,
    kind: str,
) -> UsageClassification:
    if kind in {"black", "white"} and input_data.resource_type in _TILESET_TYPES:
        return UsageClassification(
            space="2D",
            category="环境",
            subcategories=["地形"],
            reason="资源已识别为瓦片集，且图像为低信息量纯色瓦片，按地图或地形填充用途处理。",
        )

    label = {
        "transparent": "无可见像素",
        "black": "近似全黑",
        "white": "近似全白",
    }.get(kind, "低信息量")
    return UsageClassification(
        space="2D",
        category="其他",
        subcategories=["占位/辅助资源"],
        reason=f"图片为{label}低信息量资源，主要用途是占位、遮罩、填充或资源管线辅助。",
    )


def _programmatic_low_information_result(
    input_data: DescriptionInput,
) -> DescriptionResult | None:
    if input_data.resolved_llm_input_type == "audio":
        return None

    classification: tuple[str, str] | None = None
    for path in _low_information_candidate_paths(input_data):
        classification = _classify_low_information_pixels(path)
        if classification is not None:
            break
    if classification is None:
        return None

    kind, color_hex = classification
    title = input_data.title or Path(input_data.resource_path).name or input_data.resource_type
    if kind == "transparent":
        main = f"{title} 是透明占位图，无可见像素内容。"
        detail = "图片有效像素为空，通常用于 None 选项、空图层、透明占位或资源组合辅助，不应按角色、环境或物件内容检索。"
    elif kind == "black":
        if input_data.resource_type in _TILESET_TYPES:
            main = f"{title} 是近似全黑瓦片集，主色约为 {color_hex}。"
            detail = "图片有效像素几乎全部为黑色，无可辨认图案；因资源已识别为瓦片集，可作为地图地形、暗色填充或遮罩类基础瓦片使用。"
        else:
            main = f"{title} 是近似全黑图块，主色约为 {color_hex}。"
            detail = "图片有效像素几乎全部为黑色，没有可辨认图案、角色、物件或场景结构；适合作为遮罩、暗色占位、填充或资源管线辅助素材。"
    elif kind == "white":
        if input_data.resource_type in _TILESET_TYPES:
            main = f"{title} 是近似全白瓦片集，主色约为 {color_hex}。"
            detail = "图片有效像素几乎全部为白色，无可辨认图案；因资源已识别为瓦片集，可作为地图地形、亮色填充或遮罩类基础瓦片使用。"
        else:
            main = f"{title} 是近似全白图块，主色约为 {color_hex}。"
            detail = "图片有效像素几乎全部为白色，没有可辨认图案、角色、物件或场景结构；适合作为遮罩、亮色占位、填充或资源管线辅助素材。"
    else:
        return None

    return build_description_result(
        input_data,
        main_content=main,
        detail_content=detail,
        prompt_version=LOW_INFORMATION_PROMPT_VERSION,
        description_quality_score=1.0,
        classification=_low_information_classification(input_data, kind),
    )


# ---------------------------------------------------------------------------
# Provider base class
# ---------------------------------------------------------------------------


class BaseMultiModalLLMProvider(ABC):
    """多模态 LLM Provider 基类。"""

    @abstractmethod
    async def generate_description(
        self, input_data: DescriptionInput
    ) -> DescriptionResult:
        """根据预览载体和辅助元数据生成标准描述。"""
        ...

    async def generate_description_text(
        self, input_data: DescriptionInput
    ) -> DescriptionResult:
        """Generate description text only; subclasses may override to avoid classification."""
        result = await self.generate_description(input_data)
        return build_description_result(
            input_data,
            main_content=result.main_content,
            detail_content=result.detail_content,
            prompt_version=result.prompt_version,
            description_quality_score=result.description_quality_score,
        )

    async def classify_usage(
        self,
        input_data: DescriptionInput,
        main_content: str,
        detail_content: str,
    ) -> UsageClassification:
        raise NotImplementedError(f"{type(self).__name__} does not support usage classification")


# ---------------------------------------------------------------------------
# Mock provider (testing & development)
# ---------------------------------------------------------------------------


class MockLLMProvider(BaseMultiModalLLMProvider):
    """用于测试的 Mock Provider，返回固定格式的描述。"""

    PROMPT_VERSION = "prompt_v1"

    async def generate_description(
        self, input_data: DescriptionInput
    ) -> DescriptionResult:
        label = input_data.title or input_data.resource_type
        main = (
            f"这是一个{label}数字资源，类型为{input_data.resource_type}，"
            "适用于游戏开发和数字内容创作场景，可作为项目素材直接使用。"
        )
        detail = (
            f"该资源为{input_data.auxiliary_metadata.get('format', '未知')}格式，"
            f"预览方式为{input_data.preview_strategy}，"
            "具备标准化预览载体，可用于语义检索和资源管理。"
        )
        return build_description_result(
            input_data,
            main_content=main,
            detail_content=detail,
            prompt_version=self.PROMPT_VERSION,
        )

    async def generate_description_text(
        self, input_data: DescriptionInput
    ) -> DescriptionResult:
        return await self.generate_description(input_data)

    async def classify_usage(
        self,
        input_data: DescriptionInput,
        main_content: str,
        detail_content: str,
    ) -> UsageClassification:
        if input_data.resolved_llm_input_type == "audio" or input_data.resource_type == "audio_file":
            return UsageClassification(
                space="通用",
                category="音频",
                subcategories=["音效"],
                reason="Mock provider 根据资源类型返回音频用途分类。",
            )
        return UsageClassification(
            space="2D",
            category="物件",
            subcategories=["道具"],
            reason="Mock provider 返回固定用途分类，用于测试分类流程。",
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class LLMFactory:
    """按配置实例化 LLM Provider。"""

    _registry: dict[str, type] = {}

    @classmethod
    def register(cls, name: str, provider_class: type):
        cls._registry[name] = provider_class

    @classmethod
    def create(cls, name: str, **kwargs) -> BaseMultiModalLLMProvider:
        if name not in cls._registry:
            raise ValueError(
                f"Unknown LLM provider: {name}. "
                f"Available: {list(cls._registry.keys())}"
            )
        return cls._registry[name](**kwargs)

    @classmethod
    def available_providers(cls) -> list[str]:
        return list(cls._registry.keys())


LLMFactory.register("mock", MockLLMProvider)


# ---------------------------------------------------------------------------
# Convenience entry-point
# ---------------------------------------------------------------------------


async def generate_resource_description(
    input_data: DescriptionInput,
    provider_name: str = "mock",
    model: str = "",
    **provider_kwargs,
) -> DescriptionResult:
    """便捷入口：根据 provider 名称创建实例并生成描述。"""
    programmatic_result = _programmatic_low_information_result(input_data)
    if programmatic_result is not None:
        return programmatic_result

    if model:
        provider_kwargs["model"] = model
    provider = LLMFactory.create(provider_name, **provider_kwargs)
    return await provider.generate_description(input_data)


async def generate_resource_description_text(
    input_data: DescriptionInput,
    provider_name: str = "mock",
    model: str = "",
    **provider_kwargs,
) -> DescriptionResult:
    """Generate only description text for the split description command."""
    programmatic_result = _programmatic_low_information_result(input_data)
    if programmatic_result is not None:
        return build_description_result(
            input_data,
            main_content=programmatic_result.main_content,
            detail_content=programmatic_result.detail_content,
            prompt_version=programmatic_result.prompt_version,
            description_quality_score=programmatic_result.description_quality_score,
        )

    if model:
        provider_kwargs["model"] = model
    provider = LLMFactory.create(provider_name, **provider_kwargs)
    return await provider.generate_description_text(input_data)


async def classify_resource_usage(
    input_data: DescriptionInput,
    *,
    main_content: str,
    detail_content: str,
    provider_name: str = "mock",
    model: str = "",
    **provider_kwargs,
) -> UsageClassification:
    """Classify an already-described resource."""
    programmatic_result = _programmatic_low_information_result(input_data)
    if programmatic_result is not None:
        return UsageClassification(
            space=programmatic_result.usage_space,
            category=programmatic_result.usage_category,
            subcategories=programmatic_result.usage_subcategories,
            reason=programmatic_result.usage_classification_reason,
            suggestion=programmatic_result.usage_classification_suggestion,
            version=programmatic_result.usage_classification_version,
        )

    if model:
        provider_kwargs["model"] = model
    provider = LLMFactory.create(provider_name, **provider_kwargs)
    return await provider.classify_usage(input_data, main_content, detail_content)
