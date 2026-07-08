"""Client upload command: save object manifests and submit processing jobs.

The resource processing server receives storage manifests, not local files.
"""

from __future__ import annotations

import json

import requests

from ResourceProcessor.cache.local_cache import LocalCacheStore
from ResourceProcessor.pipeline_common import Report, env, make_arg_parser
from ResourceProcessor.submit_processing_manifest import submit_processing_job, wait_processing_job
from ObjectStorageUpload.resource_manifest import build_manifests_from_cache, write_manifest_records


def _split_resource_type_values(values) -> list[str]:
    resource_types: list[str] = []
    for value in values or []:
        for part in str(value or "").replace(";", ",").split(","):
            text = part.strip()
            if text:
                resource_types.append(text)
    return list(dict.fromkeys(resource_types))


def main() -> int:
    parser = make_arg_parser(
        "上传资源到资源加工服务器",
        extra_args=[
            ("--processing-server", {"default": None, "help": "资源加工服务器地址，默认 RP_PROCESSING_SERVER_URL 或 http://localhost:8100"}),
            ("--client-id", {"default": None, "help": "客户端 ID，会作为资源 ID 命名空间"}),
            ("--api-key", {"default": None, "help": "资源加工服务器 API key，默认 RP_PROCESSING_SERVER_API_KEY/RP_API_KEY"}),
            ("--storage-profile-id", {"default": None, "help": "对象存储 profile ID，默认 STORAGE_PROFILE_ID/OBJECT_STORAGE_PROFILE_ID"}),
            ("--key-prefix", {"default": "", "help": "可选对象 key 根前缀；默认空，生成 {client_id}/files 和 {client_id}/previews"}),
            ("--no-previews", {"action": "store_true", "help": "不上传本地已有预览"}),
            ("--include-descriptions", {"action": "store_true", "help": "manifest 中携带本地已有描述，服务端将跳过描述生成"}),
            ("--manifest-out", {"default": "", "help": "可选：导出 JSONL manifest 文件"}),
            ("--dry-run", {"action": "store_true", "help": "只构造 manifest，不上传、不提交"}),
            ("--no-wait", {"action": "store_true", "help": "只提交到加工服务器并记录 queued，不等待加工完成"}),
            ("--poll-interval", {"type": float, "default": 2.0, "help": "等待加工完成时的轮询间隔秒数，默认 2"}),
            ("--wait-timeout", {"type": float, "default": float(env("RP_PROCESSING_JOB_TIMEOUT", "3600")), "help": "等待加工完成的超时秒数；0 表示不超时"}),
            ("--force", {"action": "store_true", "help": "强制重新上传匹配资源；如已有 manifest，先删除旧源文件对象和旧 manifest"}),
            ("--resource-types", {"action": "append", "default": [], "help": "只上传指定资源类型；支持逗号分隔或重复传入，例如 atlas,tileset"}),
            ("--workers", {"type": int, "default": int(env("OBJECT_STORAGE_UPLOAD_WORKERS", "8")), "help": "并发上传 worker 数，默认 8；传 1 使用单线程"}),
        ],
    )
    args = parser.parse_args()
    if args.force and args.resume:
        parser.error("--force 不能和 --resume 同时使用")
    resource_types = _split_resource_type_values([args.resource_type, *args.resource_types])
    if args.force and not resource_types:
        parser.error("--force 必须配合 --resource-type 或 --resource-types，避免误重传所有资源类型")

    report = Report(label="对象存储上传并提交加工")
    processing_server = args.processing_server or env("RP_PROCESSING_SERVER_URL", "http://localhost:8100")
    client_id = args.client_id or env("CLIENT_ID", "client")
    api_key = args.api_key or env("RP_PROCESSING_SERVER_API_KEY", env("RP_API_KEY", ""))
    cache = LocalCacheStore(args.db_path)

    uploaded: list[tuple[int, dict]] = []
    for task_id, manifest in build_manifests_from_cache(
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
        source_filter=args.source_filter,
        report=report,
    ):
        uploaded.append((task_id, manifest))

    if args.manifest_out:
        write_manifest_records((manifest for _, manifest in uploaded), args.manifest_out)

    submitted = 0
    failed = 0
    if args.dry_run:
        for _, manifest in uploaded:
            print(json.dumps(manifest, ensure_ascii=False))
    else:
        with requests.Session() as session:
            for task_id, manifest in uploaded:
                try:
                    created = submit_processing_job(
                        manifest,
                        processing_server=processing_server,
                        client_id=client_id,
                        api_key=api_key,
                        session=session,
                    )
                    if args.no_wait:
                        result = created
                        cache.mark_object_manifest_queued(task_id, result)
                    else:
                        result = {
                            **created,
                            **wait_processing_job(
                                str(created.get("job_id") or ""),
                                processing_server=processing_server,
                                client_id=client_id,
                                api_key=api_key,
                                poll_interval=args.poll_interval,
                                timeout_seconds=args.wait_timeout,
                                session=session,
                            ),
                        }
                        cache.mark_object_manifest_submitted(task_id, result)
                    cache.add_log(task_id, "processing_job_submitted", json.dumps(result, ensure_ascii=False))
                    submitted += 1
                except Exception as exc:
                    failed += 1
                    cache.mark_object_manifest_submit_failed(task_id, str(exc))
                    cache.record_task_error(task_id, "processing_submit_error", str(exc)[:1000])
                    report.fail("提交失败", f"task_id={task_id}: {str(exc)[:160]}")

    cache.close()
    report.ok("完成", f"生成 manifest {len(uploaded)}, 提交 {submitted}, 失败 {failed}")
    return 0 if report.summary() else 1


if __name__ == "__main__":
    raise SystemExit(main())
