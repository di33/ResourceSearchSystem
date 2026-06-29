"""Tests for the Codex CLI-backed description provider."""

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from ResourceProcessor.description.codex_exec_provider import (
    CodexExecProvider,
    LLMFactory,
    PROMPT_VERSION,
    _parse_response,
    _prepare_image_for_codex,
)
from ResourceProcessor.description.description_generator import (
    DescriptionInput,
    DescriptionResult,
    generate_resource_description,
)


def _make_input(preview_path: str = "fake.png") -> DescriptionInput:
    return DescriptionInput(
        preview_path=preview_path,
        resource_type="image",
        preview_strategy="static",
        auxiliary_metadata={"format": "png", "resolution": "512x512"},
        title="test image",
    )


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_codex_registered_in_factory():
    assert "codex" in LLMFactory.available_providers()
    assert "codex-exec" in LLMFactory.available_providers()


def test_parse_schema_json_response():
    raw = json.dumps(
        {
            "main_content": "一个像素风角色图标资源",
            "detail_content": "PNG格式，适合作为游戏头像或UI素材",
            "description_quality_score": 0.9,
        },
        ensure_ascii=False,
    )
    main, detail, score = _parse_response(raw)
    assert main == "一个像素风角色图标资源"
    assert detail.startswith("PNG格式")
    assert score == 0.9


def test_parse_two_line_fallback():
    main, detail, score = _parse_response("主体：一个按钮图标\n细节：蓝色描边风格")
    assert main == "一个按钮图标"
    assert detail == "蓝色描边风格"
    assert score is None


def test_prepare_svg_preview_as_png(tmp_path):
    svg = tmp_path / "icon.svg"
    svg.write_text(
        "<svg xmlns='http://www.w3.org/2000/svg' width='16' height='16'>"
        "<rect width='16' height='16' fill='red'/></svg>",
        encoding="utf-8",
    )
    try:
        prepared = _prepare_image_for_codex(str(svg), tmp_path)
    except RuntimeError as exc:
        assert "SVG previews" in str(exc)
    else:
        assert prepared is not None
        assert Path(prepared).suffix == ".png"
        assert Path(prepared).is_file()


def test_generate_description_runs_codex_with_image(tmp_path):
    image = tmp_path / "preview.png"
    Image.new("RGB", (16, 16), "red").save(image)
    provider = CodexExecProvider(
        codex_bin="codex-test",
        model="gpt-test",
        cwd=str(Path.cwd()),
        timeout=5,
    )
    raw = {
        "main_content": "一张红色测试图片资源，适用于游戏开发中的占位或调试场景",
        "detail_content": "图片采用PNG格式，画面简洁，适合作为UI原型、资源管线测试或视觉占位素材",
        "description_quality_score": 0.8,
    }

    def fake_run(cmd, **kwargs):
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        assert cmd[:2] == ["codex-test", "exec"]
        assert "--image" in cmd
        assert str(image) in cmd
        assert "--output-schema" in cmd
        assert kwargs["input"]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("ResourceProcessor.description.codex_exec_provider.subprocess.run", fake_run):
        result = _run(provider.generate_description(_make_input(str(image))))

    assert isinstance(result, DescriptionResult)
    assert result.main_content == raw["main_content"]
    assert result.detail_content == raw["detail_content"]
    assert result.prompt_version == PROMPT_VERSION
    assert result.description_quality_score == 0.8


def test_generate_via_convenience_function(tmp_path):
    image = tmp_path / "preview.png"
    Image.new("RGB", (8, 8), "blue").save(image)
    raw = {
        "main_content": "一张蓝色图像资源",
        "detail_content": "适合作为UI占位图片",
        "description_quality_score": None,
    }

    def fake_run(cmd, **kwargs):
        output_path = Path(cmd[cmd.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("ResourceProcessor.description.codex_exec_provider.subprocess.run", fake_run):
        result = _run(
            generate_resource_description(
                _make_input(str(image)),
                provider_name="codex",
                codex_bin="codex-test",
                cwd=str(Path.cwd()),
            )
        )

    assert result.main_content == "一张蓝色图像资源"
    assert result.full_description.startswith("主体：")


def test_audio_input_is_rejected(tmp_path):
    audio = tmp_path / "coin.ogg"
    audio.write_bytes(b"OggS")
    provider = CodexExecProvider(codex_bin="codex-test", cwd=str(Path.cwd()))
    inp = DescriptionInput(
        preview_path="",
        resource_type="audio_file",
        preview_strategy="static",
        auxiliary_metadata={"format": "ogg"},
        llm_input_path=str(audio),
        llm_input_type="audio",
    )
    with pytest.raises(ValueError, match="image descriptions only"):
        _run(provider.generate_description(inp))
