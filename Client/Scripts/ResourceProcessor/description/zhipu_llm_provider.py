"""智谱 AI (ZhipuAI) LLM provider for resource description generation.

Supports multimodal models (glm-4v-plus, glm-4v-flash) and text-only
models (glm-5.1, glm-5, glm-4-plus) through a single provider class.

Requires:
    pip install zhipuai
    export ZHIPUAI_API_KEY=xxxxxxxx   # 智谱开放平台 API Key
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

PROMPT_VERSION = "zhipu_v1"

_VISION_MODELS = {"glm-4v-plus", "glm-4v-flash", "glm-4v"}


def _encode_image_base64(path: str) -> str | None:
    """Read a local image file and return its base64 string."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    data = p.read_bytes()
    return base64.b64encode(data).decode("utf-8")


def _build_user_content_vision(input_data: DescriptionInput) -> list[dict]:
    """Build multimodal content (image + text) for vision models."""
    content: list[dict] = []

    b64 = _encode_image_base64(input_data.preview_path)
    if b64:
        content.append({"type": "image_url", "image_url": {"url": b64}})

    context = input_data.to_prompt_context()
    content.append({"type": "text", "text": get_user_prompt(context)})
    return content


def _build_user_content_text(input_data: DescriptionInput) -> str:
    """Build text-only content for non-vision models like GLM-5.1."""
    context = input_data.to_prompt_context()
    return get_user_prompt(context)


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


class ZhipuLLMProvider(BaseMultiModalLLMProvider):
    """智谱 AI 描述生成 Provider。

    Parameters
    ----------
    model : str
        模型名称。多模态（含图片理解）用 ``"glm-4v-plus"``；
        纯文本（更强推理）用 ``"glm-5.1"``（默认）。
    api_key : str | None
        智谱 API Key。为 None 时从环境变量 ``ZHIPUAI_API_KEY`` 读取。
    """

    def __init__(
        self,
        model: str = "glm-5.1",
        api_key: str | None = None,
    ):
        self._model = model
        self._api_key = api_key or os.environ.get("ZHIPUAI_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "智谱 API Key 未设置。请设置环境变量 ZHIPUAI_API_KEY "
                "或在构造时传入 api_key 参数。"
            )

    @property
    def is_vision_model(self) -> bool:
        return self._model in _VISION_MODELS

    def _call_sync(self, input_data: DescriptionInput) -> str:
        from zhipuai import ZhipuAI

        client = ZhipuAI(api_key=self._api_key)

        if self.is_vision_model:
            user_content = _build_user_content_vision(input_data)
        else:
            user_content = _build_user_content_text(input_data)

        response = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": get_system_prompt()},
                {"role": "user", "content": user_content},
            ],
        )

        return response.choices[0].message.content

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


LLMFactory.register("zhipu", ZhipuLLMProvider)
LLMFactory.register("glm", ZhipuLLMProvider)
