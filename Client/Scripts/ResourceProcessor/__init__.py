"""Client ResourceProcessor package.

Client-owned, stateful pipeline modules live in this package. Pure preview and
description helpers are resolved from Tools/ResourceProcessor as a shared
fallback via ``__path__``.
"""

from __future__ import annotations

from pathlib import Path

_SHARED_PACKAGE = Path(__file__).resolve().parents[3] / "Tools" / "ResourceProcessor"

if _SHARED_PACKAGE.is_dir():
    __path__.append(str(_SHARED_PACKAGE))  # type: ignore[name-defined]
