from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]
_TOOLS_ROOT = _REPO_ROOT / "Tools"


def _prepend_paths(paths: tuple[Path, ...]) -> None:
    for path in reversed(paths):
        text = str(path)
        while text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)


_prepend_paths((_SCRIPT_DIR, _REPO_ROOT, _TOOLS_ROOT))

from ResourceProcessor.tools.refresh_from_crawler_state import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
