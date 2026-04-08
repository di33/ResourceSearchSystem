"""DashScope (通义千问) multimodal LLM provider for resource description generation.

Requires:
    pip install dashscope
    export DASHSCOPE_API_KEY=sk-xxxxxxxx   # 阿里云百炼 API Key
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from pathlib import Path

from ResourceProcessor.description.description_generator import (
    BaseMultiModalLLMProvider,
    DescriptionInput,
    DescriptionResult,
    LLMFactory,
)
from ResourceProcessor.description.prompt_config import get_system_prompt, get_user_prompt

logger = logging.getLogger(__name__)

PROMPT_VERSION = "dashscope_v1"


def _build_user_content(input_data: DescriptionInput) -> list[dict]:
    """Construct the multimodal message content list."""
    content: list[dict] = []

    preview = Path(input_data.preview_path) if input_data.preview_path else None
    if preview is not None and preview.is_file():
        abs_path = str(preview.resolve()).replace("\\", "/")
        content.append({"image": f"file://{abs_path}"})

    context = input_data.to_prompt_context()
    content.append({"text": get_user_prompt(context)})
    return content


def _parse_response(text: str) -> tuple[str, str]:
    """Extract main and detail from the model output."""
    main, detail = "", ""
    for line in text.strip().splitlines():
        line = line.strip()
        if line.startswith("主体：") or line.startswith("主体:"):
            main = re.sub(r"^主体[：:]", "", line).strip()
        elif line.startswith("细节：") or line.startswith("细节:"):
            detail = re.sub(r"^细节[：:]", "", line).strip()
    if not main:
        main = text.strip().splitlines()[0] if text.strip() else ""
    if not detail:
        lines = text.strip().splitlines()
        detail = lines[1] if len(lines) > 1 else main
    return main, detail


class DashScopeLLMProvider(BaseMultiModalLLMProvider):
    """通义千问 qwen-vl 多模态描述生成。

    Parameters
    ----------
    model : str
        模型名称，默认 ``"qwen-vl-max"``。可选 ``"qwen-vl-plus"``（更快更便宜）。
    api_key : str | None
        DashScope API Key。为 None 时从环境变量 ``DASHSCOPE_API_KEY`` 读取。
    """

    def __init__(
        self,
        model: str = "qwen-vl-max",
        api_key: str | None = None,
    ):
        self._model = model
        self._api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "DashScope API Key 未设置。请设置环境变量 DASHSCOPE_API_KEY "
                "或在构造时传入 api_key 参数。"
            )

    def _call_sync(self, input_data: DescriptionInput) -> str:
        from http import HTTPStatus

        import dashscope
        from dashscope import MultiModalConversation

        dashscope.api_key = self._api_key

        messages = [
            {"role": "system", "content": [{"text": get_system_prompt()}]},
            {"role": "user", "content": _build_user_content(input_data)},
        ]

        response = MultiModalConversation.call(model=self._model, messages=messages)

        if response.status_code != HTTPStatus.OK:
            raise RuntimeError(
                f"DashScope API 调用失败: code={response.status_code}, "
                f"message={response.message}"
            )

        return response.output.choices[0].message.content[0]["text"]

    async def generate_description(
        self, input_data: DescriptionInput
    ) -> DescriptionResult:
        raw_text = await asyncio.to_thread(self._call_sync, input_data)
        main, detail = _parse_response(raw_text)
        full = f"主体：{main}\n细节：{detail}"
        return DescriptionResult(
            main_content=main,
            detail_content=detail,
            full_description=full,
            prompt_version=PROMPT_VERSION,
        )


LLMFactory.register("dashscope", DashScopeLLMProvider)
LLMFactory.register("qwen-vl", DashScopeLLMProvider)
