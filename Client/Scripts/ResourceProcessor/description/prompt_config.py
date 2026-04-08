"""Shared prompt configuration — reads from environment variables."""
from __future__ import annotations

import os


def _unescape(s: str) -> str:
    """Replace literal ``\\n`` from env values with real newlines."""
    return s.replace("\\n", "\n").strip()


def get_system_prompt() -> str:
    raw = os.environ.get("LLM_SYSTEM_PROMPT", "")
    return _unescape(raw)


def get_user_prompt(context: str) -> str:
    raw = os.environ.get("LLM_USER_PROMPT", "")
    template = _unescape(raw)
    return template.replace("{context}", context)
