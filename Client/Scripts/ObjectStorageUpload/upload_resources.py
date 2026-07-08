"""CLI for uploading local resources to object storage and saving manifests."""

from __future__ import annotations

from ObjectStorageUpload.resource_manifest import build_manifests_from_cache, write_manifest_records
from ResourceProcessor.pipeline_common import Report, env, make_arg_parser


def _split_resource_type_values(values) -> list[str]:
    items: list[str] = []
    for value in values or []:
        for part in str(value or "").replace(";", ",").split(","):
            text = part.strip()
            if text:
                items.append(text)
    return list(dict.fromkeys(items))


def main() -> int:
    parser = make_arg_parser(
        "上传资源到对象存储并输出加工 manifest",
        extra_args=[
            ("--client-id", {"default": None, "help": "客户端 ID，会作为资源 ID 命名空间"}),
            ("--storage-profile-id", {"default": None, "help": "对象存储 profile ID，默认 STORAGE_PROFILE_ID/OBJECT_STORAGE_PROFILE_ID"}),
            ("--key-prefix", {"default": "", "help": "可选对象 key 根前缀；默认空，生成 {client_id}/files 和 {client_id}/previews"}),
            ("--no-previews", {"action": "store_true", "help": "不上传本地已有预览"}),
            ("--include-descriptions", {"action": "store_true", "help": "manifest 中携带本地已有描述，服务端将跳过描述生成"}),
            ("--manifest-out", {"default": "", "help": "输出 JSONL manifest 文件；真实上传未传则只写 DB，dry-run 未传则输出到 stdout"}),
            ("--dry-run", {"action": "store_true", "help": "只构造计划 manifest，不上传对象存储"}),
            ("--force", {"action": "store_true", "help": "强制重新上传匹配资源；如已有 manifest，先删除旧源文件对象和旧 manifest"}),
            ("--resource-types", {"action": "append", "default": [], "help": "只上传指定资源类型；支持逗号分隔或重复传入，例如 atlas,tileset"}),
            ("--process-states", {"action": "append", "default": [], "help": "只上传指定任务状态；支持逗号分隔或重复传入，例如 discovered,preview_failed"}),
            ("--min-task-id", {"type": int, "default": None, "help": "只上传 id >= 该值的任务"}),
            ("--max-task-id", {"type": int, "default": None, "help": "只上传 id <= 该值的任务"}),
            ("--preview-created-after", {"default": "", "help": "只处理该时间之后生成过预览的任务，例如 2026-07-06T15:22:06Z"}),
            ("--missing-manifest-only", {"action": "store_true", "help": "只处理尚未保存 uploaded manifest 的任务"}),
            ("--defer-replaced-object-cleanup", {"action": "store_true", "help": "替换 manifest 后把旧对象写入删除队列，稍后统一 flush"}),
            ("--workers", {"type": int, "default": int(env("OBJECT_STORAGE_UPLOAD_WORKERS", "8")), "help": "并发上传 worker 数，默认 8；传 1 使用单线程"}),
        ],
    )
    args = parser.parse_args()
    if args.force and args.resume:
        parser.error("--force 不能和 --resume 同时使用")
    resource_types = _split_resource_type_values([args.resource_type, *args.resource_types])
    if args.force and not resource_types:
        parser.error("--force 必须配合 --resource-type 或 --resource-types，避免误重传所有资源类型")

    report = Report(label="对象存储上传")
    client_id = args.client_id or env("CLIENT_ID", "client")
    records = (
        manifest
        for _, manifest in build_manifests_from_cache(
            db_path=args.db_path,
            client_id=client_id,
            storage_profile_id=args.storage_profile_id or "",
            include_previews=not args.no_previews,
            key_prefix=args.key_prefix,
            include_descriptions=args.include_descriptions,
            dry_run=args.dry_run,
            resume=not args.force,
            force=args.force,
            workers=args.workers,
            limit=args.limit,
            resource_types=resource_types,
            process_states=_split_resource_type_values(args.process_states),
            min_task_id=args.min_task_id,
            max_task_id=args.max_task_id,
            preview_created_after=args.preview_created_after,
            source_filter=args.source_filter,
            missing_manifest_only=args.missing_manifest_only,
            defer_replaced_object_cleanup=args.defer_replaced_object_cleanup,
            report=report,
        )
    )
    if args.manifest_out or args.dry_run:
        count = write_manifest_records(records, args.manifest_out or None)
    else:
        count = sum(1 for _ in records)
    report.ok("完成", f"输出 {count} 条 manifest")
    return 0 if report.summary() else 1


if __name__ == "__main__":
    raise SystemExit(main())
