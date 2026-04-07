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

    def to_prompt_context(self) -> str:
        """将输入转为可嵌入 Prompt 的上下文文本。"""
        parts = [f"资源类型: {self.resource_type}"]
        parts.append(f"预览策略: {self.preview_strategy}")
        for k, v in self.auxiliary_metadata.items():
            parts.append(f"{k}: {v}")
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
        main = (
            f"这是一个{input_data.resource_type}类型的数字资源，"
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
