"""Tests for ResourceProcessor.description_validator module."""

import pytest

from ResourceProcessor.description.description_generator import (
    BaseMultiModalLLMProvider,
    DescriptionInput,
    DescriptionResult,
    LLMFactory,
)
from ResourceProcessor.description.description_validator import (
    ValidationResult,
    generate_description_with_retry,
    validate_description,
    validate_description_format,
    validate_description_keywords,
    validate_description_length,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_MAIN = "这是一张高清图片素材，展现了丰富的色彩层次和精细的纹理表现，非常适合游戏开发和数字内容创作场景使用"
VALID_DETAIL = "该资源采用主流图像格式，支持标准化预览载体和语义检索，便于资源管理系统集成，可满足专业级数字内容生产流水线的品质要求"
VALID_FULL = f"主体：{VALID_MAIN}\n细节：{VALID_DETAIL}"


def _make_valid_result() -> DescriptionResult:
    return DescriptionResult(
        main_content=VALID_MAIN,
        detail_content=VALID_DETAIL,
        full_description=VALID_FULL,
        prompt_version="v_test",
    )


def _make_input(resource_type: str = "image") -> DescriptionInput:
    return DescriptionInput(
        preview_path="/tmp/test.webp",
        resource_type=resource_type,
        preview_strategy="static",
        auxiliary_metadata={"format": "webp"},
    )


# ---------------------------------------------------------------------------
# Custom providers for retry / fallback tests (12-14)
# ---------------------------------------------------------------------------


class ValidMockProvider(BaseMultiModalLLMProvider):
    def __init__(self, **kwargs):
        pass

    async def generate_description(self, input_data):
        return _make_valid_result()


class FailThenSucceedProvider(BaseMultiModalLLMProvider):
    """Returns an invalid description for the first *n_failures* calls,
    then returns a valid one."""

    def __init__(self, n_failures=1, **kwargs):
        self.n_failures = n_failures
        self.call_count = 0

    async def generate_description(self, input_data):
        self.call_count += 1
        if self.call_count <= self.n_failures:
            return DescriptionResult(
                main_content="bad",
                detail_content="",
                full_description="不合格的单行描述",
                prompt_version="v_test",
            )
        return _make_valid_result()


class AlwaysFailProvider(BaseMultiModalLLMProvider):
    """Always returns a description that fails format validation."""

    def __init__(self, **kwargs):
        pass

    async def generate_description(self, input_data):
        return DescriptionResult(
            main_content="bad",
            detail_content="",
            full_description="不合格的单行描述",
            prompt_version="v_test",
        )


@pytest.fixture(autouse=True)
def _register_test_providers():
    LLMFactory.register("_test_valid", ValidMockProvider)
    LLMFactory.register("_test_fail_then_succeed", FailThenSucceedProvider)
    LLMFactory.register("_test_always_fail", AlwaysFailProvider)
    yield
    for name in ("_test_valid", "_test_fail_then_succeed", "_test_always_fail"):
        LLMFactory._registry.pop(name, None)


# ---------------------------------------------------------------------------
# 1-3: Format validation
# ---------------------------------------------------------------------------


def test_validate_format_passes_correct_two_lines():
    result = _make_valid_result()
    v = validate_description_format(result)
    assert v.passed is True


def test_validate_format_fails_single_line():
    result = DescriptionResult(
        main_content="内容",
        detail_content="",
        full_description="主体：只有一行",
        prompt_version="v1",
    )
    v = validate_description_format(result)
    assert v.passed is False
    assert v.error_code == "FORMAT_ERROR"


def test_validate_format_fails_missing_prefix():
    result = DescriptionResult(
        main_content="内容",
        detail_content="细节",
        full_description="内容：第一行\n细节：第二行",
        prompt_version="v1",
    )
    v = validate_description_format(result)
    assert v.passed is False
    assert v.error_code == "FORMAT_ERROR"


# ---------------------------------------------------------------------------
# 4-6: Length validation
# ---------------------------------------------------------------------------


def test_validate_length_passes_in_range():
    result = _make_valid_result()
    v = validate_description_length(result)
    assert v.passed is True


def test_validate_length_fails_too_short():
    result = DescriptionResult(
        main_content="短",
        detail_content="短",
        full_description="主体：短描述\n细节：太短",
        prompt_version="v1",
    )
    v = validate_description_length(result)
    assert v.passed is False
    assert v.error_code == "TOO_SHORT"


def test_validate_length_fails_too_long():
    padding = "长" * 200
    result = DescriptionResult(
        main_content=padding,
        detail_content=padding,
        full_description=f"主体：{padding}\n细节：{padding}",
        prompt_version="v1",
    )
    v = validate_description_length(result)
    assert v.passed is False
    assert v.error_code == "TOO_LONG"


# ---------------------------------------------------------------------------
# 7-9: Keyword validation
# ---------------------------------------------------------------------------


def test_validate_keywords_passes_with_match():
    result = _make_valid_result()
    v = validate_description_keywords(result, "image")
    assert v.passed is True


def test_validate_keywords_fails_without_match():
    result = DescriptionResult(
        main_content="通用",
        detail_content="通用",
        full_description="主体：这是一个数字资源\n细节：适合多种场景使用",
        prompt_version="v1",
    )
    v = validate_description_keywords(result, "image")
    assert v.passed is False
    assert v.error_code == "MISSING_KEYWORD"


def test_validate_keywords_passes_for_other_type():
    result = DescriptionResult(
        main_content="通用",
        detail_content="通用",
        full_description="主体：通用描述内容\n细节：无特定关键词",
        prompt_version="v1",
    )
    v = validate_description_keywords(result, "other")
    assert v.passed is True


# ---------------------------------------------------------------------------
# 10: Combined validation
# ---------------------------------------------------------------------------


def test_validate_description_combined():
    result = _make_valid_result()
    v = validate_description(result, "image")
    assert v.passed is True


# ---------------------------------------------------------------------------
# 11-14: Retry and fallback (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_with_retry_succeeds_on_first_attempt():
    input_data = _make_input("image")
    result, validation = await generate_description_with_retry(
        input_data,
        primary_provider="_test_valid",
        max_retries=2,
    )
    assert result is not None
    assert validation.passed is True


@pytest.mark.asyncio
async def test_generate_with_retry_retries_on_validation_failure():
    input_data = _make_input("image")
    result, validation = await generate_description_with_retry(
        input_data,
        primary_provider="_test_fail_then_succeed",
        max_retries=2,
        n_failures=1,
    )
    assert result is not None
    assert validation.passed is True


@pytest.mark.asyncio
async def test_generate_with_retry_falls_back_to_secondary():
    input_data = _make_input("image")
    result, validation = await generate_description_with_retry(
        input_data,
        primary_provider="_test_always_fail",
        fallback_provider="_test_valid",
        max_retries=1,
    )
    assert result is not None
    assert validation.passed is True


@pytest.mark.asyncio
async def test_generate_with_retry_returns_none_after_exhaustion():
    input_data = _make_input("image")
    result, validation = await generate_description_with_retry(
        input_data,
        primary_provider="_test_always_fail",
        max_retries=2,
    )
    assert result is None
    assert validation.passed is False
