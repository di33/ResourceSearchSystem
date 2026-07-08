"""Runtime dependency helper for CLI scripts.

Only runs when ensure_requirements() is called, avoiding package import side
effects.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# deps.py 位于 <repo>/Tools/ResourceProcessor/core/，依赖声明在 Tools 目录
_REPO_ROOT = Path(__file__).resolve().parents[3]
_REQUIREMENTS = _REPO_ROOT / "Tools" / "requirements.txt"


def ensure_requirements() -> None:
    """若缺少 Pillow，则使用当前解释器执行 pip install -r Tools/requirements.txt。"""
    try:
        import PIL  # noqa: F401
        return
    except ImportError:
        pass

    if not _REQUIREMENTS.is_file():
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-q", "Pillow"],
        )
        return

    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-q", "-r", str(_REQUIREMENTS)],
    )
