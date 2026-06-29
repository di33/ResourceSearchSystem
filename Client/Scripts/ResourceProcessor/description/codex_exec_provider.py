"""Codex CLI-backed image description provider.

This provider keeps the existing API-key providers intact and runs Codex in
non-interactive mode for image-like resources. Audio should continue to use an
audio-capable API provider configured through AUDIO_LLM_PROVIDER/AUDIO_LLM_MODEL.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageSequence

from ResourceProcessor.description.description_generator import (
    BaseMultiModalLLMProvider,
    DescriptionInput,
    DescriptionResult,
    LLMFactory,
    build_description_result,
)
from ResourceProcessor.description.prompt_config import get_system_prompt, get_user_prompt
from ResourceProcessor.description.usage_classification import (
    build_response_schema,
    parse_description_response,
)

PROMPT_VERSION = "codex_exec_v1"

_SCHEMA: dict[str, Any] = build_response_schema()


def _env_flag(name: str, default: str = "") -> bool:
    value = os.environ.get(name, default).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_response(text: str) -> tuple[str, str, float | None]:
    """Parse Codex output into main/detail text.

    The normal path is schema-constrained JSON. The loose-text fallback keeps the
    provider usable if an older Codex CLI ignores --output-schema.
    """
    main, detail, score, _classification = parse_description_response(text)
    return main, detail, score


def _prepare_image_for_codex(source_path: str, work_dir: Path, output_name: str = "codex_input.png") -> str | None:
    """Return a PNG/JPEG path suitable for Codex image input.

    Many pipeline previews are WebP or GIF. Convert them to a temporary PNG so
    Codex CLI gets a conservative image format while leaving source files intact.
    """
    if not source_path:
        return None
    source = Path(source_path)
    if not source.is_file():
        return None
    if source.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        return str(source)

    out_path = work_dir / output_name
    if source.suffix.lower() == ".svg":
        try:
            import cairosvg
            cairosvg.svg2png(url=str(source), write_to=str(out_path))
        except Exception as exc:
            raise RuntimeError(
                "Codex provider needs cairosvg and a working Cairo runtime "
                "to convert SVG previews to PNG."
            ) from exc
        return str(out_path)

    with Image.open(source) as image:
        if getattr(image, "is_animated", False):
            image = next(ImageSequence.Iterator(image))
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA")
        if image.mode == "RGBA":
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            background.alpha_composite(image)
            image = background.convert("RGB")
            image.save(out_path, format="PNG")
    return str(out_path)


def _prepare_images_for_codex(source_paths: list[str], work_dir: Path) -> list[str]:
    paths: list[str] = []
    for index, source_path in enumerate(source_paths):
        output_name = f"codex_input_{index + 1:02d}.png"
        prepared = _prepare_image_for_codex(source_path, work_dir, output_name)
        if prepared:
            paths.append(prepared)
    return paths


def _build_prompt(input_data: DescriptionInput) -> str:
    context = input_data.to_prompt_context()
    return (
        f"{get_system_prompt()}\n\n"
        f"{get_user_prompt(context)}\n\n"
        "请只生成资源描述，不要修改文件、不要运行命令。"
        "最终响应必须严格匹配 JSON schema，包含 main_content、detail_content、"
        "description_quality_score、classification。main_content/detail_content 不要包含前缀。"
    )


class CodexExecProvider(BaseMultiModalLLMProvider):
    """Run Codex CLI in the background for image descriptions."""

    def __init__(
        self,
        model: str = "",
        codex_bin: str | None = None,
        timeout: int | None = None,
        profile: str | None = None,
        sandbox: str | None = None,
        cwd: str | None = None,
        skip_git_repo_check: bool | None = None,
        ignore_rules: bool | None = None,
        ignore_user_config: bool | None = None,
        disable_plugins: bool | None = None,
        disable_workspace_dependencies: bool | None = None,
    ):
        self._model = model or os.environ.get("CODEX_MODEL", "")
        self._codex_bin = codex_bin or os.environ.get("CODEX_BIN", "codex")
        self._timeout = int(timeout or os.environ.get("CODEX_TIMEOUT_SECONDS", "300"))
        self._profile = profile if profile is not None else os.environ.get("CODEX_PROFILE", "")
        self._sandbox = sandbox or os.environ.get("CODEX_SANDBOX", "read-only")
        self._cwd = cwd or os.environ.get("CODEX_WORKDIR", os.getcwd())
        self._skip_git_repo_check = (
            _env_flag("CODEX_SKIP_GIT_REPO_CHECK")
            if skip_git_repo_check is None
            else skip_git_repo_check
        )
        self._ignore_rules = (
            _env_flag("CODEX_IGNORE_RULES") if ignore_rules is None else ignore_rules
        )
        self._ignore_user_config = (
            _env_flag("CODEX_IGNORE_USER_CONFIG")
            if ignore_user_config is None
            else ignore_user_config
        )
        self._disable_plugins = (
            _env_flag("CODEX_DISABLE_PLUGINS")
            if disable_plugins is None
            else disable_plugins
        )
        self._disable_workspace_dependencies = (
            _env_flag("CODEX_DISABLE_WORKSPACE_DEPENDENCIES")
            if disable_workspace_dependencies is None
            else disable_workspace_dependencies
        )

    def _call_sync(self, input_data: DescriptionInput) -> str:
        if input_data.resolved_llm_input_type == "audio":
            raise ValueError(
                "Codex provider is configured for image descriptions only; "
                "use AUDIO_LLM_PROVIDER/AUDIO_LLM_MODEL for audio resources."
            )

        with tempfile.TemporaryDirectory(prefix="resource_codex_") as tmp:
            tmp_path = Path(tmp)
            schema_path = tmp_path / "description_schema.json"
            output_path = tmp_path / "codex_result.json"
            schema_path.write_text(json.dumps(_SCHEMA, ensure_ascii=False), encoding="utf-8")

            image_paths = _prepare_images_for_codex(input_data.resolved_llm_input_paths, tmp_path)
            prompt = _build_prompt(input_data)

            cmd = [
                self._codex_bin,
                "exec",
                "--ephemeral",
                "--sandbox",
                self._sandbox,
                "--cd",
                self._cwd,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            ]
            if self._skip_git_repo_check:
                cmd.append("--skip-git-repo-check")
            if self._ignore_rules:
                cmd.append("--ignore-rules")
            if self._ignore_user_config:
                cmd.append("--ignore-user-config")
            if self._disable_plugins:
                cmd.extend(["--disable", "plugins"])
            if self._disable_workspace_dependencies:
                cmd.extend(["--disable", "workspace_dependencies"])
            if self._model:
                cmd.extend(["--model", self._model])
            if self._profile:
                cmd.extend(["--profile", self._profile])
            for image_path in image_paths:
                cmd.extend(["--image", image_path])
            cmd.append("-")

            completed = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._timeout,
                cwd=self._cwd,
                check=False,
            )
            if completed.returncode != 0:
                detail = "\n".join(
                    part
                    for part in (
                        "STDERR:\n" + (completed.stderr or "").strip(),
                        "STDOUT:\n" + (completed.stdout or "").strip(),
                    )
                    if part.strip()
                )
                if len(detail) > 8000:
                    detail = detail[:1500] + "\n...[truncated]...\n" + detail[-6500:]
                raise RuntimeError(f"Codex exec failed ({completed.returncode}): {detail}")

            if output_path.is_file():
                result_text = output_path.read_text(encoding="utf-8").strip()
                if result_text:
                    return result_text
            return (completed.stdout or "").strip()

    async def generate_description(
        self, input_data: DescriptionInput
    ) -> DescriptionResult:
        raw_text = await asyncio.to_thread(self._call_sync, input_data)
        main, detail, score, classification = parse_description_response(raw_text)
        return build_description_result(
            input_data,
            main_content=main,
            detail_content=detail,
            prompt_version=PROMPT_VERSION,
            description_quality_score=score,
            classification=classification,
        )


LLMFactory.register("codex", CodexExecProvider)
LLMFactory.register("codex-exec", CodexExecProvider)
