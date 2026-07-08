from __future__ import annotations

import sys
from pathlib import Path

from preview_renderer.app.config import settings


_READY = False


def ensure_resource_processor_imports() -> None:
    global _READY
    tools_dir = Path(settings.shared_resource_processor_path).resolve()
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    if _READY:
        return
    _READY = True
