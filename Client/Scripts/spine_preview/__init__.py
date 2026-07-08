"""Client-side spine preview maintenance package."""

from __future__ import annotations

from pathlib import Path

_TOOLS_SPINE_PREVIEW = Path(__file__).resolve().parents[3] / "Tools" / "spine_preview"

if _TOOLS_SPINE_PREVIEW.is_dir():
    __path__.append(str(_TOOLS_SPINE_PREVIEW))  # type: ignore[name-defined]
