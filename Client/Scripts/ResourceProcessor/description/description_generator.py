"""LLM-based description generation: provider interface, factory, and mock."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Optional


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
    llm_input_path: str = ""
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
    preview_mode: str = ""
    preview_confidence: str = ""
    missing_file_ratio: float = 0.0

    @property
    def resolved_llm_input_path(self) -> str:
        return self.llm_input_path or self.preview_path

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
        if self.missing_file_ratio:
            parts.append(f"缺失文件比例: {self.missing_file_ratio:.2f}")
        for k, v in self.auxiliary_metadata.items():
            if v in ("", None, [], {}):
                continue
            parts.append(f"{k}: {self._stringify(v)}")
        return "\n".join(parts)


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
        full = f"主体：{main}\n细节：{detail}"
        return DescriptionResult(
            main_content=main,
            detail_content=detail,
            full_description=full,
            prompt_version=self.PROMPT_VERSION,
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
    **provider_kwargs,
) -> DescriptionResult:
    """便捷入口：根据 provider 名称创建实例并生成描述。"""
    provider = LLMFactory.create(provider_name, **provider_kwargs)
    return await provider.generate_description(input_data)
