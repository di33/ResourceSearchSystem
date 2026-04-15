"""Ksyun StarFlow (OpenAI-compatible) multimodal LLM provider.

Requires:
    pip install requests
    export KSPMAS_API_KEY=xxxxxxxx
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

import requests

from ResourceProcessor.description.description_generator import (
    BaseMultiModalLLMProvider,
    DescriptionInput,
    DescriptionResult,
    LLMFactory,
)
from ResourceProcessor.description.prompt_config import get_system_prompt, get_user_prompt

PROMPT_VERSION = "ksyun_v1"
_DEFAULT_BASE_URL = "https://kspmas.ksyun.com/v1"


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _encode_image_data_uri(path: str) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    data = p.read_bytes()
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(data).decode("utf-8")
    return f"data:{mime};base64,{encoded}"


def _encode_audio_input(path: str) -> dict[str, str] | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    data = p.read_bytes()
    fmt = (p.suffix.lstrip(".").lower() or "wav")
    if fmt == "oga":
        fmt = "ogg"
    return {
        "data": base64.b64encode(data).decode("utf-8"),
        "format": fmt,
    }


def _build_user_content(input_data: DescriptionInput) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    media_path = input_data.resolved_llm_input_path
    if input_data.resolved_llm_input_type == "audio":
        audio_input = _encode_audio_input(media_path)
        if audio_input:
            content.append({"type": "input_audio", "input_audio": audio_input})
    else:
        data_uri = _encode_image_data_uri(media_path)
        if data_uri:
            content.append({"type": "image_url", "image_url": {"url": data_uri}})

    context = input_data.to_prompt_context()
    content.append({"type": "text", "text": get_user_prompt(context)})
    return content


def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts).strip()
    return str(content)


def _parse_response(text: str) -> tuple[str, str]:
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


class KsyunLLMProvider(BaseMultiModalLLMProvider):
    """Ksyun OpenAI-compatible chat.completions provider."""

    def __init__(
        self,
        model: str = "glm-4.7",
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int = 60,
    ):
        self._model = os.environ.get("KSPMAS_LLM_MODEL", model)
        self._api_key = (
            api_key
            or os.environ.get("KSPMAS_API_KEY", "")
            or os.environ.get("KSC_API_KEY", "")
        )
        if not self._api_key:
            raise ValueError(
                "Ksyun API Key 未设置。请设置 KSPMAS_API_KEY（或 KSC_API_KEY）"
                "或在构造时传入 api_key。"
            )
        self._base_url = _normalize_base_url(
            base_url
            or os.environ.get("CLIENT_LLM_BASE_URL", "")
            or os.environ.get("KSPMAS_BASE_URL", "")
            or _DEFAULT_BASE_URL
        )
        self._timeout = timeout

    def _call_sync(self, input_data: DescriptionInput) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": get_system_prompt()},
                {"role": "user", "content": _build_user_content(input_data)},
            ],
        }
        resp = requests.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self._timeout,
        )
        if not resp.ok:
            raise RuntimeError(
                f"Ksyun chat.completions 调用失败: code={resp.status_code}, "
                f"body={resp.text[:300]}"
            )

        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("Ksyun 返回缺少 choices 字段")

        message = choices[0].get("message", {})
        raw_content = message.get("content", "")
        return _extract_message_text(raw_content)

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


LLMFactory.register("ksyun", KsyunLLMProvider)
LLMFactory.register("kspmas", KsyunLLMProvider)
LLMFactory.register("jinshan", KsyunLLMProvider)
