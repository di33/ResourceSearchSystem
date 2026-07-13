"""One-command refresh from ResourceCrawler state to processing upload.

The command intentionally orchestrates the existing split pipeline commands
instead of re-implementing their internals, so each step keeps its normal
resume and failure semantics.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_CLIENT_SCRIPTS = Path(__file__).resolve().parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_TOOLS_ROOT = _REPO_ROOT / "Tools"


def _prepend_paths(paths: tuple[Path, ...]) -> None:
    for path in reversed(paths):
        text = str(path)
        while text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)


_prepend_paths((_CLIENT_SCRIPTS, _REPO_ROOT, _TOOLS_ROOT))

from ResourceProcessor.crawler.catalog_loader import (  # noqa: E402
    DEFAULT_CRAWLER_OUTPUT,
    DEFAULT_CRAWLER_STATE_DB,
)
from ResourceProcessor.pipeline_common import env  # noqa: E402


@dataclass(frozen=True)
class Step:
    name: str
    module: str
    args: list[str]

    def command(self, python_exe: str) -> list[str]:
        return [python_exe, "-m", self.module, *self.args]


def _append_value(command: list[str], flag: str, value: object | None) -> None:
    if value in (None, ""):
        return
    command.extend([flag, str(value)])


def _append_flag(command: list[str], flag: str, enabled: bool) -> None:
    if enabled:
        command.append(flag)


def _append_common_filters(command: list[str], args: argparse.Namespace) -> None:
    _append_value(command, "--limit", args.limit)
    _append_value(command, "--resource-type", args.resource_type)
    _append_value(command, "--source-filter", args.source_filter)


def build_steps(args: argparse.Namespace) -> list[Step]:
    steps: list[Step] = []

    if not args.skip_sync:
        sync_args = [
            "--crawler-state-db",
            args.crawler_state_db,
            "--crawler-output",
            args.crawler_output,
            "--db-path",
            args.db_path,
        ]
        _append_flag(sync_args, "--no-backup", args.no_backup)
        _append_flag(sync_args, "--keep-preview-files", args.keep_preview_files)
        _append_flag(sync_args, "--no-object-delete-jobs", args.no_object_delete_jobs)
        for preview_dir in args.preview_dir:
            sync_args.extend(["--preview-dir", preview_dir])
        _append_value(sync_args, "--commit-every", args.sync_commit_every)
        _append_value(sync_args, "--asset-batch-size", args.asset_batch_size)
        steps.append(
            Step(
                name="sync_pipeline_from_crawler_state",
                module="ResourceProcessor.tools.sync_pipeline_from_crawler_state",
                args=sync_args,
            )
        )

    if not args.skip_object_upload:
        upload_args = ["--db-path", args.db_path, "--client-id", args.client_id]
        _append_common_filters(upload_args, args)
        _append_value(upload_args, "--storage-profile-id", args.storage_profile_id)
        _append_value(upload_args, "--key-prefix", args.key_prefix)
        _append_value(upload_args, "--workers", args.object_upload_workers)
        _append_flag(upload_args, "--missing-manifest-only", args.missing_manifest_only)
        steps.append(
            Step(
                name="upload_objects_to_storage",
                module="ResourceProcessor.upload_objects_to_storage",
                args=upload_args,
            )
        )

    if not args.skip_object_delete_flush:
        steps.append(_flush_step(args, name="flush_object_delete_jobs"))

    if not args.skip_previews:
        preview_args = [
            "--db-path",
            args.db_path,
            "--work-dir",
            args.work_dir,
            "--resume",
            "--preview-mode",
            args.preview_mode,
            "--client-id",
            args.client_id,
            "--skip-missing-object-manifest",
        ]
        _append_common_filters(preview_args, args)
        _append_value(preview_args, "--preview-renderer", args.preview_renderer)
        _append_value(preview_args, "--api-key", args.preview_api_key)
        _append_value(preview_args, "--storage-profile-id", args.storage_profile_id)
        _append_value(preview_args, "--key-prefix", args.key_prefix)
        _append_value(preview_args, "--progress-every", args.preview_progress_every)
        _append_value(preview_args, "--status-file", args.preview_status_file)
        steps.append(
            Step(
                name="generate_previews",
                module="ResourceProcessor.generate_previews",
                args=preview_args,
            )
        )

    if args.flush_object_deletes_after_previews and not args.skip_object_delete_flush:
        steps.append(_flush_step(args, name="flush_object_delete_jobs_after_previews"))

    if not args.skip_descriptions:
        description_args = ["--db-path", args.db_path, "--resume"]
        _append_common_filters(description_args, args)
        _append_value(description_args, "--llm-provider", args.llm_provider)
        _append_value(description_args, "--audio-llm-provider", args.audio_llm_provider)
        _append_value(description_args, "--concurrency", args.description_concurrency)
        _append_flag(description_args, "--retry-failed", args.retry_failed_descriptions)
        steps.append(
            Step(
                name="generate_descriptions",
                module="ResourceProcessor.generate_descriptions",
                args=description_args,
            )
        )

    if not args.skip_upload_resources:
        submit_args = [
            "--db-path",
            args.db_path,
            "--client-id",
            args.client_id,
            "--processing-server",
            args.processing_server,
        ]
        _append_common_filters(submit_args, args)
        _append_value(submit_args, "--api-key", args.processing_api_key)
        _append_value(submit_args, "--manifest-out", args.manifest_out)
        _append_value(submit_args, "--concurrency", args.upload_resources_concurrency)
        _append_value(submit_args, "--poll-interval", args.poll_interval)
        _append_value(submit_args, "--wait-timeout", args.wait_timeout)
        _append_flag(submit_args, "--no-wait", args.no_wait)
        _append_flag(submit_args, "--wait", args.wait_inline)
        steps.append(
            Step(
                name="upload_resources",
                module="ResourceProcessor.upload_resources",
                args=submit_args,
            )
        )

    return steps


def _flush_step(args: argparse.Namespace, *, name: str) -> Step:
    flush_args = ["--db-path", args.db_path]
    _append_value(flush_args, "--limit", args.object_delete_limit)
    _append_value(flush_args, "--max-attempts", args.object_delete_max_attempts)
    _append_value(flush_args, "--batch-size", args.object_delete_batch_size)
    _append_value(flush_args, "--progress-every", args.object_delete_progress_every)
    return Step(
        name=name,
        module="ResourceProcessor.tools.flush_object_delete_jobs",
        args=flush_args,
    )


def _command_text(command: list[str]) -> str:
    redacted = list(command)
    secret_flags = {"--api-key", "--preview-api-key", "--processing-api-key"}
    for index, item in enumerate(redacted[:-1]):
        if item in secret_flags:
            redacted[index + 1] = "***"
    if os.name == "nt":
        return subprocess.list2cmdline(redacted)
    return shlex.join(redacted)


def _subprocess_env() -> dict[str, str]:
    env_vars = os.environ.copy()
    paths = [str(_CLIENT_SCRIPTS), str(_TOOLS_ROOT), str(_REPO_ROOT)]
    existing = env_vars.get("PYTHONPATH", "")
    if existing:
        paths.append(existing)
    env_vars["PYTHONPATH"] = os.pathsep.join(paths)
    return env_vars


def _run_step(step: Step, *, python_exe: str, cwd: Path, env_vars: dict[str, str], print_only: bool) -> int:
    command = step.command(python_exe)
    print("\n" + "=" * 72)
    print(f"[refresh] {step.name}")
    print(_command_text(command))
    if print_only:
        return 0
    started = time.time()
    completed = subprocess.run(command, cwd=str(cwd), env=env_vars)
    elapsed = time.time() - started
    if completed.returncode == 0:
        print(f"[refresh] {step.name} OK ({elapsed:.1f}s)")
    else:
        print(f"[refresh] {step.name} FAILED rc={completed.returncode} ({elapsed:.1f}s)")
    return int(completed.returncode)


def _parser() -> argparse.ArgumentParser:
    default_db = _REPO_ROOT / "data" / "databases" / "pipeline.db"
    default_work_dir = _REPO_ROOT / "data"

    parser = argparse.ArgumentParser(
        description=(
            "一键刷新 ResourceCrawler 数据：sync -> upload objects -> flush old objects "
            "-> generate previews -> generate descriptions -> upload resources"
        )
    )
    parser.add_argument("--crawler-state-db", default=DEFAULT_CRAWLER_STATE_DB, help="ResourceCrawler crawler_state.db 路径")
    parser.add_argument("--crawler-output", default=DEFAULT_CRAWLER_OUTPUT, help="ResourceCrawler output 根目录")
    parser.add_argument("--db-path", default=str(default_db), help="目标 pipeline SQLite 路径")
    parser.add_argument("--work-dir", default=str(default_work_dir), help="预览工作目录，默认仓库 data")
    parser.add_argument("--client-id", default=env("CLIENT_ID", "resource-crawler"), help="客户端命名空间")
    parser.add_argument("--limit", type=int, default=None, help="传给支持该参数的子步骤；每步最多处理多少个资源")
    parser.add_argument("--resource-type", default="", help="传给支持该参数的子步骤；只处理指定资源类型")
    parser.add_argument("--source-filter", default="", help="传给支持该参数的子步骤；只处理指定来源站点")
    parser.add_argument("--python", default=sys.executable, help="用于执行子命令的 Python 解释器")
    parser.add_argument("--print-only", action="store_true", help="只打印将执行的子命令，不真正运行")

    parser.add_argument("--skip-sync", action="store_true", help="跳过 crawler_state 同步")
    parser.add_argument("--skip-object-upload", action="store_true", help="跳过对象存储上传")
    parser.add_argument("--skip-object-delete-flush", action="store_true", help="跳过对象删除队列清理")
    parser.add_argument("--skip-previews", action="store_true", help="跳过预览生成")
    parser.add_argument("--skip-descriptions", action="store_true", help="跳过描述生成")
    parser.add_argument("--skip-upload-resources", action="store_true", help="跳过提交加工服务器")

    parser.add_argument("--no-backup", action="store_true", help="传给 sync：同步前不备份目标 DB")
    parser.add_argument("--keep-preview-files", action="store_true", help="传给 sync：只清 DB 预览记录，不删磁盘预览")
    parser.add_argument("--no-object-delete-jobs", action="store_true", help="传给 sync：不记录对象删除队列")
    parser.add_argument("--preview-dir", action="append", default=[], help="传给 sync：允许删除预览文件的目录，可重复")
    parser.add_argument("--sync-commit-every", type=int, default=1000, help="传给 sync：每 N 条提交一次")
    parser.add_argument("--asset-batch-size", type=int, default=10000, help="传给 sync：asset_index 批大小")

    parser.add_argument("--storage-profile-id", default="", help="对象存储 profile ID")
    parser.add_argument("--key-prefix", default="", help="对象 key 根前缀")
    parser.add_argument("--object-upload-workers", type=int, default=int(env("OBJECT_STORAGE_UPLOAD_WORKERS", "8")), help="对象上传并发数")
    parser.add_argument("--missing-manifest-only", action="store_true", help="传给 upload_objects_to_storage：只处理缺失 manifest 的任务")

    parser.add_argument("--object-delete-limit", type=int, default=None, help="对象删除队列最多处理多少个任务")
    parser.add_argument("--object-delete-max-attempts", type=int, default=10, help="对象删除队列最大重试次数")
    parser.add_argument("--object-delete-batch-size", type=int, default=1000, help="对象删除队列每批最多对象 key 数")
    parser.add_argument("--object-delete-progress-every", type=int, default=1000, help="对象删除队列进度打印间隔")
    parser.add_argument(
        "--flush-object-deletes-after-previews",
        action="store_true",
        help="预览生成可能入队旧预览对象清理；开启后在预览后再清一次队列",
    )

    parser.add_argument("--preview-mode", choices=["local", "renderer"], default="renderer", help="预览生成方式")
    parser.add_argument("--preview-renderer", default=env("PREVIEW_RENDERER_URL", "http://localhost:8200"), help="preview-renderer 地址")
    parser.add_argument("--preview-api-key", default="", help="preview-renderer API key；默认由子命令读取 PR_PREVIEW_RENDERER_API_KEY/PR_API_KEY")
    parser.add_argument("--preview-progress-every", type=int, default=25, help="预览生成进度打印间隔")
    parser.add_argument("--preview-status-file", default="", help="预览生成状态日志文件")

    parser.add_argument("--llm-provider", default="", help="描述生成 LLM provider")
    parser.add_argument("--audio-llm-provider", default="", help="音频描述 LLM provider")
    parser.add_argument("--description-concurrency", type=int, default=None, help="描述生成并发数")
    parser.add_argument("--retry-failed-descriptions", action="store_true", help="描述阶段重试失败任务")

    parser.add_argument("--processing-server", default=env("RP_PROCESSING_SERVER_URL", "http://localhost:8100"), help="资源加工服务器地址")
    parser.add_argument("--processing-api-key", default="", help="资源加工服务器 API key；默认由子命令读取 RP_PROCESSING_SERVER_API_KEY/RP_API_KEY")
    parser.add_argument("--manifest-out", default="", help="提交阶段额外导出 JSONL manifest")
    parser.add_argument("--upload-resources-concurrency", type=int, default=None, help="提交加工服务器并发数")
    parser.add_argument("--poll-interval", type=float, default=None, help="提交阶段轮询间隔")
    parser.add_argument("--wait-timeout", type=float, default=None, help="提交阶段等待超时")
    parser.add_argument("--no-wait", action="store_true", help="传给 upload_resources：异步入队后不等待全部完成")
    parser.add_argument("--wait", action="store_true", dest="wait_inline", help="传给 upload_resources：逐条等待，仅用于调试")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.wait_inline and args.no_wait:
        parser.error("--wait 不能和 --no-wait 同时使用")

    steps = build_steps(args)
    if not steps:
        print("[refresh] 没有需要执行的步骤")
        return 0

    env_vars = _subprocess_env()
    print("=" * 72)
    print("ResourceCrawler 一键刷新")
    print(f"DB:        {args.db_path}")
    print(f"Client ID: {args.client_id}")
    print(f"Steps:     {' -> '.join(step.name for step in steps)}")
    print("=" * 72)

    for step in steps:
        rc = _run_step(
            step,
            python_exe=args.python,
            cwd=_REPO_ROOT,
            env_vars=env_vars,
            print_only=args.print_only,
        )
        if rc != 0:
            return rc

    print("\n[refresh] 全部步骤完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
