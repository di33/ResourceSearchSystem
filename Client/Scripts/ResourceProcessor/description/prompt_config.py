"""Shared prompt configuration — reads from environment variables."""
from __future__ import annotations

import os

from ResourceProcessor.description.usage_classification import CLASSIFICATION_RUNTIME_PROMPT


def _unescape(s: str) -> str:
    """Replace literal ``\\n`` from env values with real newlines."""
    return s.replace("\\n", "\n").strip()


SYSTEM_PROMPT = "你是一个用于数字资源检索的结构化描述与分类助手。"

DEFAULT_DESCRIPTION_PROMPT = (
    "请直接描述这个资源本身，不要描述任务说明或上下文文本。"
)

OUTPUT_FORMAT_PROMPT = (
    "输出格式要求：\n"
    "1. 最终只输出一个 JSON 对象，不要输出 Markdown、解释或代码块。\n"
    "2. 顶层字段固定为 main_content、detail_content、description_quality_score、classification。\n"
    "3. classification 字段固定包含 space、category、subcategories、reason、suggestion。\n"
    "4. 字段内容与取值规则分别遵循下面的“描述提示词”和“类型提示词”。"
)

CLASSIFICATION_OUTPUT_FORMAT_PROMPT = (
    "输出格式要求：\n"
    "1. 最终只输出一个 JSON 对象，不要输出 Markdown、解释或代码块。\n"
    "2. 顶层字段固定为 space、category、subcategories、reason、suggestion。\n"
    "3. 本步骤只做用途分类，不要改写或重新生成资源描述。"
)


def get_system_prompt() -> str:
    return SYSTEM_PROMPT


def get_description_user_prompt(context: str) -> str:
    description_prompt = _unescape(os.environ.get("LLM_DESCRIPTION_PROMPT", "")) or DEFAULT_DESCRIPTION_PROMPT
    return (
        f"{description_prompt}\n\n"
        f"资源上下文：\n{context}"
    )


def get_classification_user_prompt(context: str) -> str:
    type_prompt = _unescape(os.environ.get("LLM_TYPE_PROMPT", ""))
    if type_prompt:
        type_prompt = f"{type_prompt}\n\n可用分类规则：\n{CLASSIFICATION_RUNTIME_PROMPT}"
    else:
        type_prompt = CLASSIFICATION_RUNTIME_PROMPT
    return (
        "请根据已生成的资源描述和 resource_type，只完成用途分类。\n"
        "分类只表达资源导入游戏后的空间形态与直接用途；不要重新描述资源，"
        "不要把对象名称、文件名状态词、风格或格式当作分类。\n\n"
        f"{CLASSIFICATION_OUTPUT_FORMAT_PROMPT}\n\n"
        f"类型提示词：\n{type_prompt}\n\n"
        f"分类输入：\n{context}"
    )


def get_user_prompt(context: str) -> str:
    description_prompt = _unescape(os.environ.get("LLM_DESCRIPTION_PROMPT", "")) or DEFAULT_DESCRIPTION_PROMPT
    type_prompt = _unescape(os.environ.get("LLM_TYPE_PROMPT", ""))
    if type_prompt:
        type_prompt = f"{type_prompt}\n\n可用分类规则：\n{CLASSIFICATION_RUNTIME_PROMPT}"
    else:
        type_prompt = CLASSIFICATION_RUNTIME_PROMPT
    return (
        "请先理解资源输入载体和元数据，然后在一次响应中同时完成两件事：\n"
        "1. 按“描述提示词”生成资源描述，写入 main_content 和 detail_content。\n"
        "2. 按“类型提示词”完成用途分类，写入 classification。\n"
        "两个结果都必须输出；不要只输出描述，也不要只输出类型；不要把分类规则本身当作资源内容。\n\n"
        f"{OUTPUT_FORMAT_PROMPT}\n\n"
        f"描述提示词：\n{description_prompt}\n\n"
        f"类型提示词：\n{type_prompt}\n\n"
        f"资源上下文：\n{context}"
    )
