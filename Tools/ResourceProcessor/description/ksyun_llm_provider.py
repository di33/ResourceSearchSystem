"""Ksyun StarFlow (OpenAI-compatible) multimodal LLM provider.

Requires:
    pip install requests
    export KSPMAS_API_KEY=xxxxxxxx
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, TypeVar

import requests

from ResourceProcessor.description.description_generator import (
    BaseMultiModalLLMProvider,
    DescriptionInput,
    DescriptionResult,
    LLMFactory,
    build_description_result,
    resolve_prompt_version,
)
from ResourceProcessor.description.prompt_config import (
    get_classification_user_prompt,
    get_system_prompt,
)
from ResourceProcessor.description.description_request import build_description_request
from ResourceProcessor.description.usage_classification import (
    UsageClassification,
    build_classification_response_schema,
    build_response_schema,
    parse_classification_response,
    parse_description_response,
)

PROMPT_VERSION = "ksyun_v2_split"
_DEFAULT_BASE_URL = "https://kspmas.ksyun.com/v1"
_DEFAULT_MAX_AUDIO_INPUT_BYTES = 4 * 1024 * 1024
_DEFAULT_AUDIO_SAMPLE_SECONDS = 30
_DEFAULT_AUDIO_SAMPLE_BITRATE = "48k"
_AUDIO_SAMPLE_RATE = "16000"
_AUDIO_SAMPLE_CHANNELS = "1"

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


async def _run_in_daemon_thread(func: Callable[..., _T], *args: Any) -> _T:
    """Run blocking HTTP work without letting Ctrl+C wait for the worker thread."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[_T] = loop.create_future()

    def _target() -> None:
        try:
            result = func(*args)
        except BaseException as exc:
            if not future.cancelled():
                try:
                    loop.call_soon_threadsafe(_set_exception_if_pending, exc)
                except RuntimeError:
                    pass
            return
        if not future.cancelled():
            try:
                loop.call_soon_threadsafe(_set_result_if_pending, result)
            except RuntimeError:
                pass

    def _set_exception_if_pending(exc: BaseException) -> None:
        if not future.cancelled() and not future.done():
            future.set_exception(exc)

    def _set_result_if_pending(result: _T) -> None:
        if not future.cancelled() and not future.done():
            future.set_result(result)

    thread = threading.Thread(target=_target, name="ksyun-llm-call", daemon=True)
    thread.start()
    return await future


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


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _audio_size_label(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _audio_limit_bytes() -> int:
    return _env_int("KSPMAS_MAX_AUDIO_INPUT_BYTES", _DEFAULT_MAX_AUDIO_INPUT_BYTES)


def _audio_sample_seconds() -> int:
    return _env_int("KSPMAS_AUDIO_SAMPLE_SECONDS", _DEFAULT_AUDIO_SAMPLE_SECONDS)


def _audio_sample_bitrate() -> str:
    return os.environ.get("KSPMAS_AUDIO_SAMPLE_BITRATE", _DEFAULT_AUDIO_SAMPLE_BITRATE).strip() or _DEFAULT_AUDIO_SAMPLE_BITRATE


def _encode_audio_input(path: str | Path) -> dict[str, str] | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    data = p.read_bytes()
    return {
        "format": p.suffix.lower().lstrip(".") or "audio",
        "data": (
            f"data:{mimetypes.guess_type(p.name)[0] or 'application/octet-stream'};base64,"
            f"{base64.b64encode(data).decode('utf-8')}"
        ),
    }


def _audio_sample_dir() -> Path:
    configured = os.environ.get("KSPMAS_AUDIO_SAMPLE_DIR", "").strip()
    base = Path(configured) if configured else Path(tempfile.gettempdir()) / "resource_upload_audio_samples"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _audio_sample_key(path: Path, size: int) -> str:
    stat = path.stat()
    material = "|".join(
        [
            str(path.resolve()),
            str(size),
            str(int(stat.st_mtime)),
            str(_audio_sample_seconds()),
            _audio_sample_bitrate(),
        ]
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


def _compact_audio_for_llm(path: Path, max_bytes: int) -> Path | None:
    ffmpeg = os.environ.get("FFMPEG_BINARY", "").strip() or shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    output_path = _audio_sample_dir() / f"{_audio_sample_key(path, path.stat().st_size)}.mp3"
    if output_path.is_file() and output_path.stat().st_size <= max_bytes:
        return output_path

    attempts = [
        (_audio_sample_seconds(), _audio_sample_bitrate()),
        (min(_audio_sample_seconds(), 15), "32k"),
        (min(_audio_sample_seconds(), 8), "24k"),
    ]
    for seconds, bitrate in attempts:
        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-t",
            str(seconds),
            "-i",
            str(path),
            "-vn",
            "-ac",
            _AUDIO_SAMPLE_CHANNELS,
            "-ar",
            _AUDIO_SAMPLE_RATE,
            "-b:a",
            bitrate,
            str(output_path),
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            logger.warning("Failed to compact audio for LLM input: %s", path, exc_info=True)
            return None
        if output_path.is_file() and output_path.stat().st_size <= max_bytes:
            return output_path

    return None


def _prepare_audio_input(path: str) -> tuple[dict[str, str] | None, str | None]:
    if not path:
        return None, None
    source_path = Path(path)
    if not source_path.is_file():
        return None, f"音频输入文件不存在，已仅使用元数据：{path}"

    size = source_path.stat().st_size
    max_bytes = _audio_limit_bytes()
    if size <= max_bytes:
        return _encode_audio_input(source_path), None

    compacted_path = _compact_audio_for_llm(source_path, max_bytes)
    if compacted_path:
        note = (
            f"音频文件 {source_path.name} 原始大小 {_audio_size_label(size)}，"
            f"超过请求体保护阈值 {_audio_size_label(max_bytes)}；"
            f"已截取前 {_audio_sample_seconds()} 秒并压缩为 {compacted_path.suffix.lstrip('.')} 样本供模型听辨。"
        )
        return _encode_audio_input(compacted_path), note

    note = (
        f"音频文件 {source_path.name} 原始大小 {_audio_size_label(size)}，"
        f"超过请求体保护阈值 {_audio_size_label(max_bytes)}；"
        "未附加音频本体，请仅根据标题、路径、格式和元数据生成描述。"
    )
    return None, note


def _description_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "main_content": {"type": "string", "description": "主体描述，不包含'主体：'前缀。"},
            "detail_content": {"type": "string", "description": "细节描述，不包含'细节：'前缀。"},
            "description_quality_score": {
                "type": ["number", "null"],
                "minimum": 0,
                "maximum": 1,
            },
        },
        "required": ["main_content", "detail_content", "description_quality_score"],
    }


def _build_user_content(input_data: DescriptionInput) -> list[dict[str, Any]]:
    return _build_description_user_content(input_data)


def _build_description_user_content(input_data: DescriptionInput) -> list[dict[str, Any]]:
    request = build_description_request(input_data)
    content: list[dict[str, Any]] = []
    audio_notes: list[str] = []
    if request.llm_input_type == "audio":
        for media_path in request.llm_input_paths:
            audio_input, audio_note = _prepare_audio_input(media_path)
            if audio_input:
                content.append({"type": "input_audio", "input_audio": audio_input})
            if audio_note:
                audio_notes.append(audio_note)
    else:
        for image_path in request.llm_input_paths:
            data_uri = _encode_image_data_uri(image_path)
            if data_uri:
                content.append({"type": "image_url", "image_url": {"url": data_uri}})

    context = request.context
    if audio_notes:
        context = f"{context}\n音频输入处理: {'；'.join(audio_notes)}"
        request = build_description_request(
            replace(input_data, prompt_context_override=context)
        )
    content.append({"type": "text", "text": request.user_prompt})
    return content


def _classification_context(input_data: DescriptionInput, main_content: str, detail_content: str) -> str:
    return (
        "已生成资源描述：\n"
        f"main_content: {main_content}\n"
        f"detail_content: {detail_content}\n\n"
        "资源类型：\n"
        f"resource_type: {input_data.resource_type}"
    )


def _build_classification_user_content(
    input_data: DescriptionInput,
    main_content: str,
    detail_content: str,
) -> list[dict[str, Any]]:
    context = _classification_context(input_data, main_content, detail_content)
    return [{"type": "text", "text": get_classification_user_prompt(context)}]


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


def _compact_text(value: str, max_chars: int = 80) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= max_chars:
        return text
    return "..." + text[-(max_chars - 3):]


def _compact_path(path: str, max_chars: int = 96) -> str:
    text = str(path or "").replace("/", "\\").strip()
    if len(text) <= max_chars:
        return text
    parts = [p for p in re.split(r"[\\/]+", text) if p]
    if parts:
        for count in range(min(4, len(parts)), 0, -1):
            tail = "\\".join(parts[-count:])
            if len(tail) <= max_chars - 4:
                return f"...\\{tail}"
        basename = parts[-1]
        if len(basename) <= max_chars - 4:
            return f"...\\{basename}"
        return "..." + basename[-(max_chars - 3):]
    return "..." + text[-(max_chars - 3):]


def _format_llm_log(input_data: DescriptionInput, model: str, stage: str = "description") -> str:
    fields = [
        f"model={model}",
        f"stage={stage}",
        f"type={input_data.resolved_llm_input_type}",
        f"resource={input_data.resource_type}",
    ]
    if input_data.title:
        fields.append(f'title="{_compact_text(input_data.title, 64)}"')
    if input_data.resource_path and input_data.resource_path != input_data.title:
        fields.append(f'resource_path="{_compact_path(input_data.resource_path, 96)}"')
    media_path = input_data.resolved_llm_input_path
    if media_path:
        fields.append(f'input="{_compact_path(media_path, 96)}"')
    return "  [LLM] " + " ".join(fields)


def _parse_response(text: str) -> tuple[str, str]:
    main, detail, _score, _classification = parse_description_response(text)
    return main, detail


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _response_format(schema_name: str, schema: dict[str, Any]) -> dict[str, Any] | None:
    mode = os.environ.get("KSPMAS_RESPONSE_FORMAT", "json_schema").strip().lower()
    if mode in {"", "none", "off", "false", "disabled"}:
        return None
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema,
            },
        }
    raise ValueError(
        "KSPMAS_RESPONSE_FORMAT must be json_schema, json_object, or none"
    )


class KsyunLLMProvider(BaseMultiModalLLMProvider):
    """Ksyun OpenAI-compatible chat.completions provider."""

    def __init__(
        self,
        model: str = "",
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
    ):
        self._model = model if model else os.environ.get("KSPMAS_LLM_MODEL", "glm-4.7")
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
        self._timeout = timeout if timeout is not None else _env_int("KSPMAS_LLM_TIMEOUT", 60)

    def _call_sync(self, input_data: DescriptionInput) -> str:
        return self._chat_completion_sync(
            input_data,
            stage="description",
            user_content=_build_description_user_content(input_data),
            schema_name="resource_description_result",
            schema=_description_response_schema(),
        )

    def _call_classification_sync(
        self,
        input_data: DescriptionInput,
        main_content: str,
        detail_content: str,
    ) -> str:
        return self._chat_completion_sync(
            input_data,
            stage="classification",
            user_content=_build_classification_user_content(
                input_data,
                main_content,
                detail_content,
            ),
            schema_name="resource_usage_classification",
            schema=build_classification_response_schema(),
        )

    def _chat_completion_sync(
        self,
        input_data: DescriptionInput,
        *,
        stage: str,
        user_content: list[dict[str, Any]],
        schema_name: str,
        schema: dict[str, Any],
    ) -> str:
        payload = {
            "model": self._model,
            "temperature": _env_float("KSPMAS_TEMPERATURE", 0.0),
            "messages": [
                {"role": "system", "content": get_system_prompt()},
                {"role": "user", "content": user_content},
            ],
        }
        response_format = _response_format(schema_name, schema)
        if response_format is not None:
            payload["response_format"] = response_format
        import sys
        print(_format_llm_log(input_data, self._model, stage), file=sys.stderr)
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

    async def classify_usage(
        self,
        input_data: DescriptionInput,
        main_content: str,
        detail_content: str,
    ) -> UsageClassification:
        raw_text = await _run_in_daemon_thread(
            self._call_classification_sync,
            input_data,
            main_content,
            detail_content,
        )
        return parse_classification_response(raw_text)

    async def generate_description_text(
        self, input_data: DescriptionInput
    ) -> DescriptionResult:
        raw_text = await _run_in_daemon_thread(self._call_sync, input_data)
        main, detail, score, _description_classification = parse_description_response(raw_text)
        return build_description_result(
            input_data,
            main_content=main,
            detail_content=detail,
            prompt_version=resolve_prompt_version(PROMPT_VERSION, input_data),
            description_quality_score=score,
        )

    async def generate_description(
        self, input_data: DescriptionInput
    ) -> DescriptionResult:
        raw_text = await _run_in_daemon_thread(self._call_sync, input_data)
        main, detail, score, _description_classification = parse_description_response(raw_text)
        classification = await self.classify_usage(input_data, main, detail)
        return build_description_result(
            input_data,
            main_content=main,
            detail_content=detail,
            prompt_version=resolve_prompt_version(PROMPT_VERSION, input_data),
            description_quality_score=score,
            classification=classification,
        )


LLMFactory.register("ksyun", KsyunLLMProvider)
LLMFactory.register("kspmas", KsyunLLMProvider)
LLMFactory.register("jinshan", KsyunLLMProvider)
