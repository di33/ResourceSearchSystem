"""Preview generation step of the split pipeline.

Usage:
    python -m ResourceProcessor.generate_previews \
        --db-path pipeline.db --limit 100
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from ResourceProcessor.pipeline_common import (
    Report,
    env,
    make_arg_parser,
    print_progress,
    state_ge,
)
from resource_contracts.resource_types import (
    ANIMATION_SEQUENCE_RESOURCE_TYPE,
    PACK_RESOURCE_TYPE,
    SINGLE_IMAGE_RESOURCE_TYPE,
    is_search_indexable_resource_type,
)


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"}
PREVIEW_FILE_EXTS = IMAGE_EXTS | {".avif"}


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _delete_old_preview_files(old_previews: list[dict], keep_paths: set[str]) -> tuple[int, int]:
    deleted = 0
    skipped = 0
    keep_keys = {_path_key(path) for path in keep_paths if path}
    seen: set[str] = set()
    for preview in old_previews:
        raw_path = preview.get("path") if isinstance(preview, dict) else ""
        if not raw_path:
            skipped += 1
            continue
        path = Path(raw_path)
        key = _path_key(path)
        if key in seen or key in keep_keys:
            skipped += 1
            continue
        seen.add(key)
        if path.suffix.lower() not in PREVIEW_FILE_EXTS or not path.is_file():
            skipped += 1
            continue
        try:
            path.unlink()
            deleted += 1
        except OSError:
            skipped += 1
    return deleted, skipped


def _append_status(status_file: str, message: str) -> None:
    if not status_file:
        return
    path = Path(status_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")


def _preview_strategy(value: str):
    from ResourceProcessor.preview_metadata import PreviewStrategy

    try:
        return PreviewStrategy(str(value or "static"))
    except ValueError:
        return PreviewStrategy.STATIC


def _remote_preview_infos(result: dict) -> list:
    from ResourceProcessor.preview_metadata import PreviewInfo

    previews = []
    for item in result.get("previews") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if not path:
            continue
        preview_path = Path(path)
        previews.append(
            PreviewInfo(
                strategy=_preview_strategy(str(item.get("strategy") or "static")),
                role=str(item.get("role") or "primary"),
                path=str(preview_path),
                format=str(item.get("format") or preview_path.suffix.lstrip(".") or ""),
                mode=str(item.get("mode") or "direct"),
                confidence=str(item.get("confidence") or "high"),
                width=item.get("width"),
                height=item.get("height"),
                size=item.get("size") or (preview_path.stat().st_size if preview_path.is_file() else None),
                renderer=str(item.get("renderer") or "preview-renderer"),
                used_placeholder=bool(item.get("used_placeholder") or False),
                fail_reason=str(item.get("fail_reason") or ""),
            )
        )
    if not previews:
        raise RuntimeError("preview-renderer returned no preview files")
    return previews


def _render_previews_with_remote_renderer(
    manifest: dict,
    *,
    preview_renderer: str,
    client_id: str,
    previews_dir: str | Path,
    api_key: str = "",
    session=None,
) -> list:
    from ResourceProcessor.render_previews_via_renderer import render_preview_manifest

    result = render_preview_manifest(
        manifest,
        preview_renderer=preview_renderer,
        client_id=client_id,
        output_root=Path(previews_dir),
        api_key=api_key,
        primary_only=False,
        session=session,
    )
    return _remote_preview_infos(result)


def _dedupe_pack_child_resources(records: list[dict]) -> list[dict]:
    records.sort(
        key=lambda item: (
            item["priority"],
            -item["coverage_count"],
            _natural_key(item.get("resource_path") or item.get("title") or ""),
        )
    )
    selected: list[dict] = []
    seen_resources: set[str] = set()
    covered_by_selected: set[str] = set()

    for record in records:
        resource_key = str(record.get("source_resource_id") or record.get("task_id") or "")
        if resource_key and resource_key in seen_resources:
            continue
        if resource_key:
            seen_resources.add(resource_key)

        file_keys = record.pop("_file_keys", set())
        coverage = record.pop("_coverage_keys", set())
        if record["resource_type"] == SINGLE_IMAGE_RESOURCE_TYPE and file_keys & covered_by_selected:
            continue
        if coverage and any(
            _pack_child_coverage_can_dedupe(selected_record, record)
            and coverage <= selected_record.get("_coverage_keys", set())
            and selected_record["priority"] <= record["priority"]
            for selected_record in selected
        ):
            continue

        record["_coverage_keys"] = coverage
        selected.append(record)
        covered_by_selected.update(coverage)

    cleaned: list[dict] = []
    for record in selected:
        record = dict(record)
        record.pop("_coverage_keys", None)
        record.pop("_file_keys", None)
        cleaned.append(record)
    return cleaned


def _pack_child_coverage_can_dedupe(selected_record: dict, record: dict) -> bool:
    if (
        selected_record.get("resource_type") == ANIMATION_SEQUENCE_RESOURCE_TYPE
        and record.get("resource_type") == ANIMATION_SEQUENCE_RESOURCE_TYPE
    ):
        return False
    return True


def _natural_key(value: str) -> list[tuple[int, object]]:
    parts: list[tuple[int, object]] = []
    chunk = ""
    is_digit = False
    for ch in Path(str(value)).name.lower():
        if ch.isdigit():
            if chunk and not is_digit:
                parts.append((1, chunk))
                chunk = ""
            chunk += ch
            is_digit = True
        else:
            if chunk and is_digit:
                parts.append((0, int(chunk)))
                chunk = ""
            chunk += ch
            is_digit = False
    if chunk:
        parts.append((0, int(chunk)) if is_digit else (1, chunk))
    return parts


def _cached_task_ids(
    cache,
    resource_type: str = "",
    source_filter: str = "",
    limit: int | None = None,
    task_ids: list[int] | None = None,
    min_task_id: int | None = None,
    max_task_id: int | None = None,
    phase: str = "non-pack",
    marker: str = "",
    worker_count: int = 1,
    worker_index: int = 0,
) -> list[int]:
    sql = "SELECT id FROM resource_task WHERE 1 = 1"
    params: list = []
    if task_ids:
        placeholders = ",".join(["?"] * len(task_ids))
        sql += f" AND id IN ({placeholders})"
        params.extend(task_ids)
    if min_task_id is not None:
        sql += " AND id >= ?"
        params.append(min_task_id)
    if max_task_id is not None:
        sql += " AND id <= ?"
        params.append(max_task_id)
    if resource_type:
        sql += " AND resource_type = ?"
        params.append(resource_type)
    sql += " AND resource_type <> ?"
    params.append(PACK_RESOURCE_TYPE)
    if source_filter:
        sql += " AND source = ?"
        params.append(source_filter)
    if marker:
        sql += """
            AND NOT EXISTS (
                SELECT 1 FROM resource_preview rp
                WHERE rp.task_id = resource_task.id
                  AND rp.created_at >= ?
            )
        """
        params.append(marker)
    if worker_count > 1:
        sql += " AND (id % ?) = ?"
        params.extend([worker_count, worker_index])
    sql += " ORDER BY id"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [int(row["id"]) for row in cache._conn.execute(sql, params).fetchall()]


def main() -> int:
    parser = make_arg_parser(
        "生成资源预览并写入 SQLite",
        extra_args=[
            ("--work-dir", {"default": None, "help": "工作目录；默认仓库根目录 data，预览写入 <work-dir>/previews/<resource_type>/"}),
            ("--force", {"action": "store_true", "help": "强制重新生成匹配资源的预览；成功后清除旧预览记录和旧预览文件"}),
            ("--task-id", {"action": "append", "type": int, "help": "只处理指定 task id；可重复传入"}),
            ("--min-task-id", {"type": int, "default": None, "help": "只处理 id >= 该值的 task"}),
            ("--max-task-id", {"type": int, "default": None, "help": "只处理 id <= 该值的 task"}),
            ("--preview-mode", {"choices": ["local", "renderer"], "default": "renderer", "help": "预览生成方式：renderer=调用 preview-renderer 服务，local=本地调试生成"}),
            ("--preview-renderer", {"default": None, "help": "preview-renderer 地址；renderer 模式默认 PREVIEW_RENDERER_URL 或 http://localhost:8200"}),
            ("--client-id", {"default": None, "help": "renderer 模式写入 X-Client-Id，默认 CLIENT_ID"}),
            ("--api-key", {"default": None, "help": "preview-renderer API key，默认 PR_PREVIEW_RENDERER_API_KEY/PR_API_KEY"}),
            ("--phase", {"choices": ["non-pack", "pack", "all"], "default": "non-pack", "help": "兼容旧刷新 worker 参数；pack 资源始终跳过预览生成"}),
            ("--marker", {"default": "", "help": "只处理 marker 时间之后没有新预览的任务，例如 2026-06-23T13:51:27Z"}),
            ("--worker-count", {"type": int, "default": 1, "help": "并行 worker 总数；按 task id 取模分片"}),
            ("--worker-index", {"type": int, "default": 0, "help": "当前 worker 下标，范围 [0, worker-count)"}),
            ("--progress-every", {"type": int, "default": 25, "help": "每处理 N 个任务打印一次进度"}),
            ("--status-file", {"default": "", "help": "可选状态日志文件，兼容旧刷新 worker"}),
            ("--skip-missing-object-manifest", {"action": "store_true", "help": "renderer 模式下跳过没有对象存储 manifest 的任务"}),
        ],
    )
    args = parser.parse_args()
    if args.worker_count < 1:
        parser.error("--worker-count must be >= 1")
    if not 0 <= args.worker_index < args.worker_count:
        parser.error("--worker-index must be in [0, worker-count)")

    from ResourceProcessor.cache.local_cache import LocalCacheStore
    from ResourceProcessor.preview.crawler_thumbnail_policy import CrawlerThumbnailPolicy
    from ResourceProcessor.preview_metadata import ProcessState

    db_path = os.path.abspath(args.db_path)
    cache = LocalCacheStore(db_path)

    project_root = Path(__file__).resolve().parents[3]
    work_dir = os.path.abspath(args.work_dir) if args.work_dir else str(project_root / "data")
    previews_dir = os.path.join(work_dir, "previews")
    os.makedirs(previews_dir, exist_ok=True)
    preview_mode = "renderer" if args.preview_renderer else args.preview_mode
    preview_renderer = (args.preview_renderer or env("PREVIEW_RENDERER_URL", "http://localhost:8200")).strip().rstrip("/")
    client_id = (args.client_id or env("CLIENT_ID", "client")).strip()
    api_key = args.api_key or env("PR_PREVIEW_RENDERER_API_KEY", env("PR_API_KEY", ""))

    report = Report(label="预览生成")
    print("=" * 60)
    print("  预览生成 (generate_previews)")
    print("  数据源:         DB-only")
    print(f"  数据库:         {db_path}")
    print(f"  预览目录:       {previews_dir}")
    if args.limit:
        print(f"  限制:           {args.limit}")
    if args.resource_type:
        print(f"  资源类型:       {args.resource_type}")
    if args.task_id:
        print(f"  Task过滤:       {', '.join(str(item) for item in args.task_id)}")
    if args.min_task_id is not None or args.max_task_id is not None:
        print(f"  Task范围:       {args.min_task_id or '-inf'}..{args.max_task_id or '+inf'}")
    if args.force:
        print("  强制刷新:       enabled")
    print(f"  生成方式:       {preview_mode}")
    if preview_mode == "renderer":
        print(f"  Renderer:       {preview_renderer}")
        print(f"  Client ID:      {client_id}")
    if args.phase != "non-pack":
        print(f"  阶段:           {args.phase}")
    if args.marker:
        print(f"  Marker:         {args.marker}")
    if args.worker_count > 1:
        print(f"  Worker:         {args.worker_index}/{args.worker_count}")
    print("=" * 60)
    _append_status(
        args.status_file,
        (
            f"start mode={preview_mode} renderer={preview_renderer if preview_mode == 'renderer' else ''} "
            f"db={db_path} phase={args.phase} worker={args.worker_index}/{args.worker_count}"
        ),
    )

    policy = CrawlerThumbnailPolicy(previews_dir)
    remote_session = None
    if preview_mode == "renderer":
        import requests

        remote_session = requests.Session()

    state_counts = cache.count_tasks_by_state()
    report.ok("当前状态统计", ", ".join(f"{k}={v}" for k, v in state_counts.items()) or "(空)")

    processed = 0
    skipped = 0
    preview_count = 0
    deleted_preview_count = 0
    deleted_preview_file_count = 0
    skipped_preview_file_count = 0
    failed = 0

    def process_entity(task_id: int, entity, *, success_state: ProcessState | None) -> bool:
        nonlocal preview_count, deleted_preview_count, deleted_preview_file_count, skipped_preview_file_count, failed
        try:
            old_previews = cache.get_previews_by_task(task_id)
            if preview_mode == "renderer":
                object_manifest = cache.get_object_manifest(task_id)
                if not object_manifest:
                    raise RuntimeError("missing object-storage manifest; run upload_objects_to_storage first")
                renderer_kwargs = {
                    "preview_renderer": preview_renderer,
                    "client_id": client_id,
                    "previews_dir": previews_dir,
                    "session": remote_session,
                }
                if api_key:
                    renderer_kwargs["api_key"] = api_key
                previews = _render_previews_with_remote_renderer(
                    object_manifest["manifest"],
                    **renderer_kwargs,
                )
            else:
                previews = asyncio.run(policy.generate_previews(entity))
            entity.previews = previews
            deleted_preview_count += cache.delete_previews_by_task(task_id)
            for preview in previews:
                if preview.path:
                    cache.insert_preview(task_id, preview)
                    preview_count += 1
            deleted_files, skipped_files = _delete_old_preview_files(
                old_previews,
                {preview.path for preview in previews if preview.path},
            )
            deleted_preview_file_count += deleted_files
            skipped_preview_file_count += skipped_files
            if success_state is not None:
                cache.update_task_state(task_id, success_state)
        except Exception as exc:
            failed += 1
            if success_state == ProcessState.PREVIEW_READY:
                cache.update_task_state(
                    task_id, ProcessState.PREVIEW_FAILED,
                    error_code="preview_error",
                    error_message=str(exc)[:500],
                )
            else:
                cache.record_task_error(task_id, "preview_error", str(exc)[:500])
            report.fail(
                f"预览 [{entity.title or entity.resource_path or entity.content_md5[:12]}]",
                str(exc)[:120],
            )
            return False

        return True

    for task_id in _cached_task_ids(
        cache,
        args.resource_type,
        args.source_filter,
        args.limit,
        args.task_id,
        args.min_task_id,
        args.max_task_id,
        phase=args.phase,
        marker=args.marker,
        worker_count=args.worker_count,
        worker_index=args.worker_index,
    ):
        task = cache.get_task_by_id(task_id)
        if task is None:
            failed += 1
            continue
        if not is_search_indexable_resource_type(task["resource_type"]):
            skipped += 1
            continue
        current_state = ProcessState(task["process_state"])
        already_previewed = state_ge(current_state.value, ProcessState.PREVIEW_READY.value)
        if already_previewed and not args.force:
            skipped += 1
            continue
        if preview_mode == "renderer" and args.skip_missing_object_manifest and not cache.get_object_manifest(task_id):
            skipped += 1
            continue
        entity = cache.rebuild_entity_from_cache(task_id)
        if entity is None:
            failed += 1
            continue
        success_state = ProcessState.PREVIEW_READY
        if process_entity(task_id, entity, success_state=success_state):
            processed += 1
        if args.progress_every and processed % args.progress_every == 0 and processed:
            print_progress(processed, processed + skipped, f"预览累计 {preview_count}, 跳过 {skipped}")
            _append_status(
                args.status_file,
                f"progress processed={processed} skipped={skipped} failed={failed} previews={preview_count}",
            )

    report.ok(
        "预览生成",
        (
            f"处理 {processed}, 跳过 {skipped}, 失败 {failed}, 清旧记录 {deleted_preview_count}, "
            f"删旧文件 {deleted_preview_file_count}, 文件跳过 {skipped_preview_file_count}, 预览图片 {preview_count} 张"
        ),
    )
    report.ok("最终状态统计", ", ".join(f"{k}={v}" for k, v in cache.count_tasks_by_state().items()))

    if remote_session is not None:
        remote_session.close()
    cache.close()
    ok = report.summary()
    _append_status(
        args.status_file,
        f"done ok={ok} processed={processed} skipped={skipped} failed={failed} previews={preview_count}",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
