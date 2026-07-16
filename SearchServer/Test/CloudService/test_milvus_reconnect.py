from __future__ import annotations

import sys
from pathlib import Path


_SERVER_DIR = Path(__file__).resolve().parents[2]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.deps import _is_connection_error


def test_should_create_connection_first_is_reconnectable() -> None:
    error = RuntimeError("MilvusException: should create connection first")

    assert _is_connection_error(error)
