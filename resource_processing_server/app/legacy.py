from __future__ import annotations

import sys
from pathlib import Path

from resource_processing_server.app.config import settings


_READY = False


def ensure_resource_processor_imports() -> None:
    """Expose the shared ResourceProcessor package to this service."""
    global _READY
    scripts_dir = Path(settings.shared_resource_processor_path).resolve()
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    if _READY:
        return

    # Register optional LLM providers if their dependencies are installed.
    for module in (
        "ResourceProcessor.description.dashscope_llm_provider",
        "ResourceProcessor.description.zhipu_llm_provider",
        "ResourceProcessor.description.ksyun_llm_provider",
        "ResourceProcessor.description.codex_exec_provider",
    ):
        try:
            __import__(module)
        except Exception:
            pass
    _READY = True
