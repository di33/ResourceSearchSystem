"""Shared prompt configuration — reads from environment variables."""
from __future__ import annotations

import os


def _unescape(s: str) -> str:
    """Replace literal ``\\n`` from env values with real newlines."""
    return s.replace("\\n", "\n").strip()


def get_system_prompt() -> str:
    raw = os.environ.get("LLM_SYSTEM_PROMPT", "")
    if raw.strip():
        return _unescape(raw)
    return (
        "你是一个用于数字资源检索的描述生成助手。"
        "请结合预览图与结构化上下文，输出准确、简洁、可检索的资源描述。"
        "不要直接照抄来源描述；应保留关键信息并统一表述风格。"
    )


def get_user_prompt(context: str) -> str:
    raw = os.environ.get("LLM_USER_PROMPT", "")
    if raw.strip():
        template = _unescape(raw)
    else:
        template = (
            "请根据下面的资源上下文生成两段式中文描述。\n"
            "要求：\n"
            "1. 输出格式固定为两行，分别以“主体：”和“细节：”开头。\n"
            "2. 主体描述说明资源是什么、适用于什么场景。\n"
            "3. 细节描述补充风格、构成、用途或关键元素。\n"
            "4. 若预览是 metadata_only/fallback，应优先依赖上下文字段，避免凭空猜测。\n\n"
            "{context}"
        )
    return template.replace("{context}", context)
