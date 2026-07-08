from __future__ import annotations

import os
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
CLIENT_ROOT = SCRIPT_DIR.parent
REPO_ROOT = CLIENT_ROOT.parent
TOOLS_ROOT = REPO_ROOT / "Tools"


def ensure_paths() -> None:
    for path in (REPO_ROOT, TOOLS_ROOT, SCRIPT_DIR):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def init_client_env() -> None:
    ensure_paths()
    load_dotenv(CLIENT_ROOT / ".env")
    load_dotenv(CLIENT_ROOT / ".env.local")
