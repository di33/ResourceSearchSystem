"""Client-side utilities for ResourceProcessor pipeline commands."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any


_SCRIPT_DIR = Path(__file__).resolve().parent
_CLIENT_SCRIPTS_ROOT = _SCRIPT_DIR.parent
_CLIENT_ROOT = _CLIENT_SCRIPTS_ROOT.parent
_REPO_ROOT = _CLIENT_ROOT.parent
_TOOLS_ROOT = _REPO_ROOT / "Tools"
_DATA_ROOT = _REPO_ROOT / "data"

for path in (_CLIENT_SCRIPTS_ROOT, _REPO_ROOT, _TOOLS_ROOT):
    text = str(path)
    if text not in sys.path:
        sys.path.append(text)


def _load_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip("'\"")
    return env


def _load_dotenv_files(*paths: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for path in paths:
        env.update(_load_dotenv(path))
    return env


def init_env() -> dict[str, str]:
    dotenv = _load_dotenv_files(
        _TOOLS_ROOT / ".env",
        _CLIENT_ROOT / ".env",
        _CLIENT_ROOT / ".env.local",
    )
    for key, value in dotenv.items():
        if value and key not in os.environ:
            os.environ[key] = value
    return dotenv


_DOTENV = init_env()


def env(key: str, fallback: str = "") -> str:
    return os.environ.get(key, _DOTENV.get(key, fallback))


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
        print(f"  步骤通过: {sum(1 for s in self.steps if s['status'] == 'OK')}  失败: {len(self.errors)}")
        if self.errors:
            print("  失败详情：")
            for error in self.errors:
                print(f"    - {error}")
        print("=" * 60)
        return not self.errors


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
    return _STATE_ORDINAL.get(state_a, -1) >= _STATE_ORDINAL.get(state_b, -1)


def state_lt(state_a: str, state_b: str) -> bool:
    return _STATE_ORDINAL.get(state_a, -1) < _STATE_ORDINAL.get(state_b, -1)


def merge_cached_entity_state(entity, cached_entity):
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


def print_progress(current: int, total: int, label: str = "") -> None:
    suffix = f" | {label}" if label else ""
    print(f"    进度: {current}/{total}{suffix}")
