from __future__ import annotations

import re
from pathlib import Path

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_RESERVED_RE = re.compile(r'[<>:"/\\|?*]')


def safe_path_part(value: str, fallback: str = "resource") -> str:
    text = str(value or "").strip()
    text = text.replace("/", "_").replace("\\", "_")
    text = _CONTROL_RE.sub("_", text)
    text = _RESERVED_RE.sub("_", text)
    text = text.strip(" .")
    return text if text and text not in {".", ".."} else fallback


def safe_file_name(value: str, fallback: str = "file") -> str:
    text = str(value or "").replace("\\", "/").strip()
    name = text.rsplit("/", 1)[-1].strip()
    return safe_path_part(name, fallback=fallback)


def safe_join_under(root: str | Path, *parts: str, fallback: str = "resource") -> Path:
    root_path = Path(root).resolve()
    candidate = root_path
    for part in parts:
        candidate = candidate / safe_path_part(part, fallback=fallback)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise ValueError(f"path escapes work root: {candidate}") from exc
    return resolved


def ensure_under(root: str | Path, target: str | Path) -> Path:
    root_path = Path(root).resolve()
    resolved = Path(target).resolve()
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise ValueError(f"path escapes work root: {target}") from exc
    return resolved
