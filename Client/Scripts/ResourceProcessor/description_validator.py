"""Description validation, retry logic, and primary/fallback provider switching."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from ResourceProcessor.description_generator import (
    BaseMultiModalLLMProvider,
    DescriptionInput,
    DescriptionResult,
    LLMFactory,
)

# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    passed: bool
    error_code: str = ""
    error_message: str = ""


# ---------------------------------------------------------------------------
# Individual validators
# ---------------------------------------------------------------------------


def validate_description_format(result: DescriptionResult) -> ValidationResult:
    """校验 full_description 是否严格匹配两行结构：'主体：...' 和 '细节：...'"""
    lines = result.full_description.strip().split("\n")
    if len(lines) != 2:
        return ValidationResult(False, "FORMAT_ERROR", f"期望2行，实际{len(lines)}行")
    if not lines[0].startswith("主体："):
        return ValidationResult(False, "FORMAT_ERROR", "第一行必须以'主体：'开头")
    if not lines[1].startswith("细节："):
        return ValidationResult(False, "FORMAT_ERROR", "第二行必须以'细节：'开头")
    return ValidationResult(True)


def validate_description_length(
    result: DescriptionResult, min_chars: int = 100, max_chars: int = 220
) -> ValidationResult:
    """校验 full_description 总字数是否在容忍范围内。"""
    text = (
        result.full_description.replace("主体：", "")
        .replace("细节：", "")
        .replace("\n", "")
    )
    length = len(text)
    if length < min_chars:
        return ValidationResult(
            False, "TOO_SHORT", f"描述字数 {length} 不足最低 {min_chars}"
        )
    if length > max_chars:
        return ValidationResult(
            False, "TOO_LONG", f"描述字数 {length} 超过上限 {max_chars}"
        )
    return ValidationResult(True)


def validate_description_keywords(
    result: DescriptionResult, resource_type: str
) -> ValidationResult:
    """校验描述是否包含资源类型或等价词。"""
    type_keywords = {
        "image": ["图片", "图像", "image", "照片", "插画"],
        "3d_model": ["模型", "3D", "3d", "model", "mesh"],
        "design_file": ["设计", "design", "源文件", "画板"],
        "audio": ["音频", "audio", "音乐", "声音", "sound"],
        "font": ["字体", "font", "笔刷", "brush"],
        "other": [],
    }
    keywords = type_keywords.get(resource_type, [])
    if not keywords:
        return ValidationResult(True)
    text = result.full_description
    for kw in keywords:
        if kw in text:
            return ValidationResult(True)
    return ValidationResult(
        False, "MISSING_KEYWORD", f"描述中缺少资源类型关键词，期望包含 {keywords} 之一"
    )


# ---------------------------------------------------------------------------
# Combined validator
# ---------------------------------------------------------------------------


def validate_description(
    result: DescriptionResult, resource_type: str
) -> ValidationResult:
    """组合校验：格式 -> 字数 -> 关键词。返回第一个失败的结果，或全部通过。"""
    for validator in [
        lambda: validate_description_format(result),
        lambda: validate_description_length(result),
        lambda: validate_description_keywords(result, resource_type),
    ]:
        v = validator()
        if not v.passed:
            return v
    return ValidationResult(True)


# ---------------------------------------------------------------------------
# Retry + fallback generation
# ---------------------------------------------------------------------------


async def generate_description_with_retry(
    input_data: DescriptionInput,
    primary_provider: str = "mock",
    fallback_provider: Optional[str] = None,
    max_retries: int = 2,
    **provider_kwargs,
) -> Tuple[Optional[DescriptionResult], ValidationResult]:
    """带校验和重试的描述生成。

    Returns:
        (result, validation) 二元组。
        - 若成功：result 是通过校验的 DescriptionResult，validation.passed=True。
        - 若全部失败：result 为 None，validation 包含最后一次失败原因。
    """
    providers_to_try = [primary_provider]
    if fallback_provider and fallback_provider != primary_provider:
        providers_to_try.append(fallback_provider)

    last_validation = ValidationResult(False, "NO_ATTEMPT", "未执行任何生成尝试")

    for provider_name in providers_to_try:
        provider = LLMFactory.create(provider_name, **provider_kwargs)
        for attempt in range(max_retries + 1):
            try:
                result = await provider.generate_description(input_data)
                validation = validate_description(result, input_data.resource_type)
                if validation.passed:
                    return result, validation
                last_validation = validation
                logging.warning(
                    "描述校验失败 (provider=%s, attempt=%d): %s",
                    provider_name,
                    attempt,
                    validation.error_message,
                )
            except Exception as exc:
                last_validation = ValidationResult(
                    False, "PROVIDER_ERROR", str(exc)
                )
                logging.warning(
                    "描述生成异常 (provider=%s, attempt=%d): %s",
                    provider_name,
                    attempt,
                    exc,
                )
        logging.warning("Provider %s 耗尽重试次数", provider_name)

    return None, last_validation
