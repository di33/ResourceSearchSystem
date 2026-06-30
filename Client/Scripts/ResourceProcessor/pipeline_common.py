"""Shared utilities for the split pipeline scripts (generate_previews, generate_descriptions, upload_resources)."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Environment setup
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


def _load_dotenv(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    if not os.path.isfile(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    return env


def _load_dotenv_files(*paths: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for path in paths:
        env.update(_load_dotenv(str(path)))
    return env


def init_env() -> dict[str, str]:
    client_root = Path(_SCRIPT_DIR).resolve().parents[1]
    dotenv = _load_dotenv_files(client_root / ".env", client_root / ".env.local")
    for key, value in dotenv.items():
        if value and key not in os.environ:
            os.environ[key] = value
    return dotenv


_DOTENV = init_env()


def env(key: str, fallback: str = "") -> str:
    return os.environ.get(key, _DOTENV.get(key, fallback))


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------

_CLIENT_ROOT = Path(_SCRIPT_DIR).resolve().parents[1]
_REPO_ROOT = _CLIENT_ROOT.parent
_DATA_ROOT = _REPO_ROOT / "data"
_DEFAULT_CRAWLER_OUTPUT = env("CRAWLER_OUTPUT", r"K:\ResourceCrawler\output")
_DEFAULT_CRAWLER_STATE_DB = env("CRAWLER_STATE_DB", r"G:\ResourceCrawler\data\crawler_state.db")


def make_arg_parser(
    description: str,
    extra_args: list[tuple] | None = None,
    *,
    include_crawler_args: bool = False,
    crawler_output_required: bool = False,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--db-path",
        default=str(_DATA_ROOT / "databases" / "pipeline.db"),
        help="SQLite 数据库路径 (默认 data/databases/pipeline.db)",
    )
    if include_crawler_args:
        crawler_output_default = _DEFAULT_CRAWLER_OUTPUT if crawler_output_required else ""
        parser.add_argument(
            "--crawler-state-db",
            default=_DEFAULT_CRAWLER_STATE_DB,
            help="ResourceCrawler crawler_state.db 路径",
        )
        parser.add_argument(
            "--crawler-output",
            required=crawler_output_required and not crawler_output_default,
            default=crawler_output_default,
            help="ResourceCrawler output 根目录，仅用于定位 assets/metadata",
        )
    parser.add_argument("--limit", type=int, default=None, help="最多处理多少个资源")
    parser.add_argument("--resource-type", default="", help="只处理指定资源类型")
    parser.add_argument("--source-filter", default="", help="只处理指定来源站点")
    parser.add_argument("--resume", action="store_true", help="跳过已完成的资源 (断点续传)")
    if extra_args:
        for flags_or_kwargs in extra_args:
            if isinstance(flags_or_kwargs, tuple):
                flag = flags_or_kwargs[0]
                kwargs = flags_or_kwargs[1] if len(flags_or_kwargs) > 1 else {}
                parser.add_argument(flag, **kwargs)
            else:
                parser.add_argument(flags_or_kwargs)
    return parser


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class Report:
    def __init__(self, label: str = ""):
        self.label = label
        self.steps: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.t0 = time.time()

    def ok(self, step: str, detail: str = ""):
        self.steps.append({"step": step, "status": "OK", "detail": detail})
        self._print("OK", step, detail)

    def fail(self, step: str, detail: str = ""):
        self.steps.append({"step": step, "status": "FAIL", "detail": detail})
        self.errors.append(f"{step}: {detail}")
        self._print("FAIL", step, detail)

    def _print(self, status: str, step: str, detail: str):
        color = "\033[92m" if status == "OK" else "\033[91m"
        reset = "\033[0m"
        msg = f"  {color}[{status}]{reset} {step}"
        if detail:
            msg += f"  ({detail})"
        print(msg)

    def summary(self) -> bool:
        elapsed = time.time() - self.t0
        print("\n" + "=" * 60)
        label = self.label or "流程"
        print(f"  {label}完成  耗时 {elapsed:.1f}s")
        print(f"  通过: {sum(1 for s in self.steps if s['status'] == 'OK')}  失败: {len(self.errors)}")
        if self.errors:
            print("  失败详情：")
            for error in self.errors:
                print(f"    - {error}")
        print("=" * 60)
        return not self.errors


# ---------------------------------------------------------------------------
# ProcessState ordering (alphabetical string comparison is wrong: 'd' < 'p')
# ---------------------------------------------------------------------------

# Pipeline order: earlier states < later states
_STATE_ORDINAL: dict[str, int] = {
    "discovered": 0,
    "preview_failed": 1,
    "preview_ready": 2,
    "description_failed": 3,
    "description_ready": 4,
    "classify_ready": 5,
    "package_ready": 6,
    "registered": 7,
    "uploaded": 8,
    "committed": 9,
    "synced": 10,
}


def state_ge(state_a: str, state_b: str) -> bool:
    """Return True if state_a is >= state_b in pipeline order."""
    return _STATE_ORDINAL.get(state_a, -1) >= _STATE_ORDINAL.get(state_b, -1)


def state_lt(state_a: str, state_b: str) -> bool:
    """Return True if state_a is < state_b in pipeline order."""
    return _STATE_ORDINAL.get(state_a, -1) < _STATE_ORDINAL.get(state_b, -1)


def merge_cached_entity_state(entity, cached_entity):
    """Keep fresh crawler metadata while carrying DB-generated state/results."""
    if cached_entity is None:
        return entity
    if not getattr(entity, "files", None) and getattr(cached_entity, "files", None):
        entity.files = cached_entity.files
    for attr in (
        "process_state",
        "previews",
        "resource_id",
        "download_object_key",
        "download_file_name",
        "download_content_type",
        "download_file_size",
        "description_main",
        "description_detail",
        "description_full",
        "prompt_version",
        "description_quality_score",
        "usage_space",
        "usage_category",
        "usage_subcategories",
        "usage_classification_reason",
        "usage_classification_suggestion",
        "usage_classification_version",
        "retry_count",
        "last_error_code",
        "last_error_message",
        "updated_at",
    ):
        if hasattr(cached_entity, attr):
            setattr(entity, attr, getattr(cached_entity, attr))
    return entity


# ---------------------------------------------------------------------------
# Progress helper
# ---------------------------------------------------------------------------


def print_progress(current: int, total: int, label: str = "") -> None:
    suffix = f" | {label}" if label else ""
    print(f"    进度: {current}/{total}{suffix}")
