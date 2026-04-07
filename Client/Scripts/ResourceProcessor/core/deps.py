"""
运行时按需安装 requirements.txt 中的第三方依赖（当前主要为 Pillow）。
仅在显式调用 ensure_requirements() 时执行，避免 import 包副作用。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# deps.py 位于 <repo>/Scripts/ResourceProcessor/，requirements.txt 在仓库根目录
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_REQUIREMENTS = _REPO_ROOT / "requirements.txt"


def ensure_requirements() -> None:
    """若缺少 Pillow，则使用当前解释器执行 pip install -r requirements.txt。"""
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
