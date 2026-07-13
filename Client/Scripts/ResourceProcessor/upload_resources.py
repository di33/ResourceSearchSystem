"""Submit uploaded resource object manifests to the processing server.

The resource processing server receives storage manifests, not local files.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import json
from pathlib import Path
import queue
import threading
import time
import uuid

import requests

from ResourceProcessor.cache.local_cache import LocalCacheStore, compute_resource_fingerprint_for_connection
from ResourceProcessor.core.processing_manifest import (
    build_processing_manifest,
    classification_from_entity,
    description_from_entity,
)
from ResourceProcessor.pipeline_common import Report, env, make_arg_parser, state_ge
from ResourceProcessor.preview_metadata import ProcessState
from ResourceProcessor.submit_processing_manifest import (
    get_processing_job_statuses,
    submit_processing_job,
    wait_processing_job,
)
from resource_contracts.resource_types import PACK_RESOURCE_TYPE, is_search_indexable_resource_type


_WORKER_DONE = object()


@dataclass(frozen=True)
class _SubmitConfig:
    db_path: str
    processing_server: str
    client_id: str
    api_key: str
    no_wait: bool
    poll_interval: float
    wait_timeout: float
    force: bool


@dataclass
class _TaskResult:
    task_id: int = 0
    generated: int = 0
    submitted: int = 0
    failed: int = 0
    skipped_clean: int = 0
    skipped_busy: int = 0
    skipped_missing_manifest: int = 0
    skipped_missing_source: int = 0
    skipped_non_indexable: int = 0
    skipped_not_ready: int = 0
    skipped_missing_preview: int = 0
    skipped_missing_description: int = 0
    failure_step: str = ""
    failure_detail: str = ""


def _split_resource_type_values(values) -> list[str]:
    resource_types: list[str] = []
    for value in values or []:
        for part in str(value or "").replace(";", ",").split(","):
            text = part.strip()
            if text:
                resource_types.append(text)
    return list(dict.fromkeys(resource_types))


def _env_bool(key: str, default: bool = False) -> bool:
    raw = env(key, "")
    if raw == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _object_file_name(ref: dict | None) -> str:
    if not isinstance(ref, dict):
        return "resource"
    if ref.get("file_name"):
        return str(ref.get("file_name") or "")
    key = str(ref.get("object_key") or "").rstrip("/")
    return key.rsplit("/", 1)[-1] if key else "resource"


def _object_file_format(ref: dict | None) -> str:
    if isinstance(ref, dict) and ref.get("file_format"):
        return str(ref.get("file_format") or "").lower().lstrip(".")
    name = _object_file_name(ref)
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _file_ref_as_source_object(item: dict) -> dict:
    source_object = {
        key: item[key]
        for key in (
            "storage_profile_id",
            "object_key",
            "file_name",
            "file_format",
            "content_type",
            "size",
            "checksum",
            "etag",
        )
        if item.get(key) not in (None, "")
    }
    if "size" not in source_object and item.get("file_size") not in (None, ""):
        source_object["size"] = item["file_size"]
    if "checksum" not in source_object:
        checksum = item.get("content_md5") or item.get("content_hash")
        if checksum:
            source_object["checksum"] = checksum
    if "file_name" not in source_object:
        source_object["file_name"] = _object_file_name(source_object)
    if "file_format" not in source_object:
        source_object["file_format"] = _object_file_format(source_object)
    return source_object


def _source_object_from_uploaded_manifest(uploaded_manifest: dict) -> dict:
    source_object = uploaded_manifest.get("source_object")
    if isinstance(source_object, dict) and source_object.get("object_key"):
        return source_object

    source_files = [item for item in uploaded_manifest.get("source_files") or [] if isinstance(item, dict)]
    file_refs = [item for item in source_files if item.get("object_key")]
    if file_refs:
        primary = next((item for item in file_refs if item.get("is_primary")), file_refs[0])
        return _file_ref_as_source_object(primary)

    package_object = uploaded_manifest.get("package_object")
    if isinstance(package_object, dict) and package_object.get("object_key"):
        return package_object

    return source_object if isinstance(source_object, dict) else {}


def _preview_items_from_uploaded_manifest(uploaded_manifest: dict) -> list[dict]:
    for key in ("previews", "provided_previews", "preview_refs"):
        items = uploaded_manifest.get(key)
        if isinstance(items, list) and items:
            return [item for item in items if isinstance(item, dict)]
    return []


def _has_uploaded_preview(uploaded_manifest: dict) -> bool:
    return any(
        isinstance(item, dict) and item.get("object_key")
        for item in _preview_items_from_uploaded_manifest(uploaded_manifest)
    )


def _has_local_description(entity) -> bool:
    return description_from_entity(entity) is not None


def _task_state_ready(task: dict) -> bool:
    return state_ge(str(task.get("process_state") or ""), ProcessState.DESCRIPTION_READY.value)


def _build_manifest_from_uploaded_record(
    entity,
    uploaded_manifest: dict,
    *,
    client_id: str,
) -> dict:
    previews = _preview_items_from_uploaded_manifest(uploaded_manifest)
    description = description_from_entity(entity)
    source_object = _source_object_from_uploaded_manifest(uploaded_manifest)
    return build_processing_manifest(
        entity,
        client_id=client_id,
        source_object=source_object,
        source_files=uploaded_manifest.get("source_files") or [],
        previews=previews,
        description=description,
        classification=classification_from_entity(entity),
        package_object=uploaded_manifest.get("package_object") or None,
    )


def _submission_request_id(
    *,
    object_record: dict,
    manifest: dict,
    resource_fingerprint: str,
    force: bool,
) -> str:
    prior_result = object_record.get("processing_result") or {}
    prior_request_id = str(prior_result.get("request_id") or "").strip()
    prior_fingerprint = str(object_record.get("resource_fingerprint") or "")
    prior_state = str(object_record.get("submit_state") or "")
    if (
        prior_request_id
        and prior_fingerprint == resource_fingerprint
        and prior_state in {"submitting", "submit_failed"}
    ):
        return prior_request_id
    if force:
        return f"force:{uuid.uuid4().hex}"
    return f"upload:{manifest.get('client_resource_id', '')}:{resource_fingerprint}"


def _reconcile_inflight_jobs(
    *,
    cache: LocalCacheStore,
    processing_server: str,
    client_id: str,
    api_key: str,
    resource_types: list[str],
    source: str,
    report: Report,
    limit: int | None = None,
) -> dict[str, int]:
    totals = {"checked": 0, "completed": 0, "active": 0, "failed": 0, "errors": 0}
    batch: list[dict] = []

    def flush(records: list[dict], session: requests.Session) -> None:
        if not records:
            return
        no_job = [record for record in records if not str(record.get("processing_job_id") or "").strip()]
        with_job = [record for record in records if str(record.get("processing_job_id") or "").strip()]
        failed_updates = [
            {
                "task_id": record["task_id"],
                "job_id": "",
                "error": "previous upload was interrupted before processing job id was recorded",
            }
            for record in no_job
        ]
        if no_job:
            applied = cache.apply_processing_job_statuses(failed=failed_updates)
            totals["failed"] += applied["failed"]

        if not with_job:
            totals["checked"] += len(records)
            return

        job_ids = [str(record["processing_job_id"]) for record in with_job]
        try:
            response = get_processing_job_statuses(
                job_ids,
                processing_server=processing_server,
                client_id=client_id,
                api_key=api_key,
                session=session,
            )
        except Exception as exc:
            totals["errors"] += len(with_job)
            report.ok("对账暂缓", f"保留 {len(with_job)} 个 queued 任务，下次继续: {str(exc)[:160]}")
            totals["checked"] += len(records)
            return

        by_job_id = {str(item.get("job_id") or ""): item for item in response.get("jobs") or []}
        missing = set(str(value or "") for value in response.get("missing_job_ids") or [])
        completed_updates: list[dict] = []
        active_updates: list[dict] = []
        failed_updates = []
        for record in with_job:
            job_id = str(record["processing_job_id"])
            status = by_job_id.get(job_id)
            if status is None or job_id in missing:
                failed_updates.append({
                    "task_id": record["task_id"],
                    "job_id": job_id,
                    "error": "processing job not found; it will be submitted again",
                })
                continue
            state = str(status.get("state") or "").lower()
            update = {
                "task_id": record["task_id"],
                "job_id": job_id,
                "resource_fingerprint": str(record.get("resource_fingerprint") or ""),
                "result": {
                    **(record.get("processing_result") or {}),
                    **status,
                },
            }
            if state == "completed":
                completed_updates.append(update)
            elif state in {"failed", "cancelled"}:
                failed_updates.append({
                    **update,
                    "error": str(status.get("error") or state),
                })
            else:
                active_updates.append(update)
        applied = cache.apply_processing_job_statuses(
            completed=completed_updates,
            active=active_updates,
            failed=failed_updates,
        )
        totals["completed"] += applied["completed"]
        totals["active"] += applied["active"]
        totals["failed"] += applied["failed"]
        totals["checked"] += len(records)

    with requests.Session() as session:
        for record in cache.iter_inflight_object_manifests(
            resource_types=resource_types,
            source=source,
            limit=limit,
        ):
            batch.append(record)
            if len(batch) >= 1000:
                flush(batch, session)
                batch = []
        flush(batch, session)
    return totals


def _wait_for_inflight_jobs(
    *,
    cache: LocalCacheStore,
    processing_server: str,
    client_id: str,
    api_key: str,
    resource_types: list[str],
    source: str,
    poll_interval: float,
    report: Report,
) -> dict[str, int]:
    totals = {"completed": 0, "failed": 0, "errors": 0}
    last_remaining = -1
    while True:
        remaining = cache.count_inflight_object_manifests(
            resource_types=resource_types,
            source=source,
        )
        if remaining <= 0:
            return totals
        reconciled = _reconcile_inflight_jobs(
            cache=cache,
            processing_server=processing_server,
            client_id=client_id,
            api_key=api_key,
            resource_types=resource_types,
            source=source,
            report=report,
            limit=min(10000, remaining),
        )
        totals["completed"] += reconciled["completed"]
        totals["failed"] += reconciled["failed"]
        totals["errors"] += reconciled["errors"]
        current = cache.count_inflight_object_manifests(
            resource_types=resource_types,
            source=source,
        )
        if current != last_remaining:
            report.ok(
                "加工进度",
                f"剩余 {current}, 本轮完成 {reconciled['completed']}, 失败 {reconciled['failed']}",
            )
            last_remaining = current
        if current <= 0:
            return totals
        if reconciled["errors"]:
            time.sleep(max(1.0, float(poll_interval)))
        else:
            time.sleep(max(0.1, float(poll_interval)))


def _submit_one_task(
    *,
    cache: LocalCacheStore,
    session: requests.Session,
    task_id: int,
    config: _SubmitConfig,
) -> _TaskResult:
    result = _TaskResult(task_id=task_id)
    task = cache.get_task_by_id(task_id)
    if task is not None and not _task_state_ready(task):
        result.skipped_not_ready = 1
        return result
    entity = cache.rebuild_entity_from_cache(task_id)
    if entity is None:
        return result
    if entity.resource_type == PACK_RESOURCE_TYPE or not is_search_indexable_resource_type(entity.resource_type):
        result.skipped_non_indexable = 1
        return result
    object_record = cache.get_object_manifest(task_id)
    if object_record is None or object_record.get("upload_state") != "uploaded":
        result.skipped_missing_manifest = 1
        return result
    uploaded_manifest = object_record.get("manifest") or {}
    source_object = _source_object_from_uploaded_manifest(uploaded_manifest)
    if not isinstance(source_object, dict) or not source_object.get("object_key"):
        result.skipped_missing_source = 1
        result.failure_step = "跳过"
        result.failure_detail = f"task_id={task_id} 缺少已上传源对象；请先运行 upload_objects_to_storage"
        return result
    if not _has_uploaded_preview(uploaded_manifest):
        result.skipped_missing_preview = 1
        return result
    if not _has_local_description(entity):
        result.skipped_missing_description = 1
        return result

    current_fingerprint, _fingerprint_parts = compute_resource_fingerprint_for_connection(cache._conn, task_id)
    committed_fingerprint = str(object_record.get("committed_fingerprint") or "")
    if not config.force and committed_fingerprint == current_fingerprint:
        result.skipped_clean = 1
        return result
    existing_job_id = str(object_record.get("processing_job_id") or "").strip()
    if str(object_record.get("submit_state") or "") == "submitting" and existing_job_id:
        try:
            created = object_record.get("processing_result") or {"job_id": existing_job_id}
            if config.no_wait:
                processing_result = {**created, "job_id": existing_job_id}
                cache.mark_object_manifest_queued(
                    task_id,
                    processing_result,
                    resource_fingerprint=str(object_record.get("resource_fingerprint") or current_fingerprint),
                )
            else:
                processing_result = {
                    **created,
                    **wait_processing_job(
                        existing_job_id,
                        processing_server=config.processing_server,
                        client_id=config.client_id,
                        api_key=config.api_key,
                        poll_interval=config.poll_interval,
                        timeout_seconds=config.wait_timeout,
                        session=session,
                    ),
                }
                cache.mark_object_manifest_submitted(
                    task_id,
                    processing_result,
                    resource_fingerprint=str(object_record.get("resource_fingerprint") or current_fingerprint),
                )
            cache.add_log(task_id, "processing_job_resumed", json.dumps(processing_result, ensure_ascii=False))
            result.submitted = 1
        except Exception as exc:
            message = str(exc)
            result.failed = 1
            result.failure_step = "提交失败"
            result.failure_detail = f"task_id={task_id}: {message[:160]}"
            cache.mark_object_manifest_submit_failed(task_id, message)
            cache.record_task_error(task_id, "processing_submit_error", message[:1000])
        return result

    manifest = _build_manifest_from_uploaded_record(
        entity,
        uploaded_manifest,
        client_id=config.client_id,
    )
    request_id = _submission_request_id(
        object_record=object_record,
        manifest=manifest,
        resource_fingerprint=current_fingerprint,
        force=config.force,
    )
    manifest["request_id"] = request_id
    result.generated = 1
    if not cache.claim_object_manifest_for_async_submit(
        task_id,
        resource_fingerprint=current_fingerprint,
        request_id=request_id,
        force=config.force,
    ):
        result.skipped_busy = 1
        return result

    try:
        created = submit_processing_job(
            manifest,
            processing_server=config.processing_server,
            client_id=config.client_id,
            api_key=config.api_key,
            session=session,
        )
        created = {"request_id": request_id, **created}
        if config.no_wait:
            processing_result = created
            cache.mark_object_manifest_async_queued(
                task_id,
                processing_result,
                resource_fingerprint=current_fingerprint,
            )
        else:
            cache.mark_object_manifest_submitting_job(
                task_id,
                created,
                resource_fingerprint=current_fingerprint,
            )
            processing_result = {
                **created,
                **wait_processing_job(
                    str(created.get("job_id") or ""),
                    processing_server=config.processing_server,
                    client_id=config.client_id,
                    api_key=config.api_key,
                    poll_interval=config.poll_interval,
                    timeout_seconds=config.wait_timeout,
                    session=session,
                ),
            }
            cache.mark_object_manifest_submitted(
                task_id,
                processing_result,
                resource_fingerprint=current_fingerprint,
            )
            cache.add_log(task_id, "processing_job_submitted", json.dumps(processing_result, ensure_ascii=False))
        result.submitted = 1
    except Exception as exc:
        message = str(exc)
        result.failed = 1
        result.failure_step = "提交失败"
        result.failure_detail = f"task_id={task_id}: {message[:160]}"
        cache.mark_object_manifest_submit_failed(task_id, message)
        cache.record_task_error(task_id, "processing_submit_error", message[:1000])
    return result


def _submit_worker(
    *,
    task_queue: "queue.Queue[int | object]",
    result_queue: "queue.Queue[_TaskResult | object]",
    config: _SubmitConfig,
    stop_event: threading.Event,
) -> None:
    cache = None
    try:
        cache = LocalCacheStore(config.db_path)
        with requests.Session() as session:
            while not stop_event.is_set():
                try:
                    item = task_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    if item is _WORKER_DONE:
                        return
                    try:
                        result = _submit_one_task(
                            cache=cache,
                            session=session,
                            task_id=int(item),
                            config=config,
                        )
                    except Exception as exc:
                        result = _TaskResult(
                            task_id=int(item),
                            failed=1,
                            failure_step="提交失败",
                            failure_detail=f"task_id={int(item)}: {str(exc)[:160]}",
                        )
                    result_queue.put(result)
                finally:
                    task_queue.task_done()
    except Exception as exc:
        result_queue.put(
            _TaskResult(
                failed=1,
                failure_step="worker异常",
                failure_detail=str(exc)[:160],
            )
        )
    finally:
        if cache is not None:
            cache.close()
        result_queue.put(_WORKER_DONE)


def _collect_result(report: Report, result: _TaskResult, totals: dict[str, int]) -> None:
    totals["generated"] += result.generated
    totals["submitted"] += result.submitted
    totals["failed"] += result.failed
    totals["skipped_clean"] += result.skipped_clean
    totals["skipped_busy"] += result.skipped_busy
    totals["skipped_missing_manifest"] += result.skipped_missing_manifest
    totals["skipped_missing_source"] += result.skipped_missing_source
    totals["skipped_non_indexable"] += result.skipped_non_indexable
    totals["skipped_not_ready"] += result.skipped_not_ready
    totals["skipped_missing_preview"] += result.skipped_missing_preview
    totals["skipped_missing_description"] += result.skipped_missing_description
    if result.failure_step:
        report.fail(result.failure_step, result.failure_detail)


def _submit_concurrently(
    *,
    task_ids,
    config: _SubmitConfig,
    concurrency: int,
    report: Report,
) -> dict[str, int]:
    totals = {
        "generated": 0,
        "submitted": 0,
        "failed": 0,
        "skipped_clean": 0,
        "skipped_busy": 0,
        "skipped_missing_manifest": 0,
        "skipped_missing_source": 0,
        "skipped_non_indexable": 0,
        "skipped_not_ready": 0,
        "skipped_missing_preview": 0,
        "skipped_missing_description": 0,
    }
    task_queue: "queue.Queue[int | object]" = queue.Queue(maxsize=max(1, concurrency * 4))
    result_queue: "queue.Queue[_TaskResult | object]" = queue.Queue()
    stop_event = threading.Event()

    workers = [
        threading.Thread(
            target=_submit_worker,
            kwargs={
                "task_queue": task_queue,
                "result_queue": result_queue,
                "config": config,
                "stop_event": stop_event,
            },
            name=f"upload-resources-worker-{index + 1}",
            daemon=True,
        )
        for index in range(concurrency)
    ]
    for worker in workers:
        worker.start()

    def produce() -> None:
        try:
            for task_id in task_ids:
                if stop_event.is_set():
                    break
                while not stop_event.is_set():
                    try:
                        task_queue.put(task_id, timeout=0.5)
                        break
                    except queue.Full:
                        continue
        finally:
            for _ in workers:
                while True:
                    try:
                        task_queue.put(_WORKER_DONE, timeout=0.5)
                        break
                    except queue.Full:
                        if stop_event.is_set():
                            break

    producer = threading.Thread(target=produce, name="upload-resources-producer", daemon=True)
    producer.start()

    try:
        finished_workers = 0
        while finished_workers < len(workers):
            try:
                item = result_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is _WORKER_DONE:
                finished_workers += 1
                continue
            _collect_result(report, item, totals)
    except KeyboardInterrupt:
        stop_event.set()
        report.fail("已中断", "收到 Ctrl+C，正在停止并发提交 worker")
        raise
    finally:
        stop_event.set()
        producer.join(timeout=2)
        for worker in workers:
            worker.join(timeout=2)
    return totals


def main() -> int:
    parser = make_arg_parser(
        "提交已上传对象资源到资源加工服务器",
        extra_args=[
            ("--processing-server", {"default": None, "help": "资源加工服务器地址，默认 RP_PROCESSING_SERVER_URL 或 http://localhost:8100"}),
            ("--client-id", {"default": None, "help": "客户端 ID，会作为资源 ID 命名空间"}),
            ("--api-key", {"default": None, "help": "资源加工服务器 API key，默认 RP_PROCESSING_SERVER_API_KEY/RP_API_KEY"}),
            ("--direct-search-upsert", {"action": "store_true", "default": _env_bool("RP_DIRECT_SEARCH_UPSERT", False), "help": "已废弃：客户端只提交到资源加工服务器"}),
            ("--manifest-out", {"default": "", "help": "可选：导出 JSONL manifest 文件"}),
            ("--dry-run", {"action": "store_true", "help": "只构造待提交 manifest，不提交"}),
            ("--no-wait", {"action": "store_true", "dest": "detach", "default": False, "help": "异步入队后立即退出，不等待 queued/submitting 任务对账完成"}),
            ("--wait-all", {"action": "store_false", "dest": "detach", "help": "全部任务入队后持续批量对账，直到本地没有 queued/submitting 任务（默认行为）"}),
            ("--wait", {"action": "store_true", "dest": "wait_inline", "help": "逐条等待加工完成；仅用于调试"}),
            ("--poll-interval", {"type": float, "default": float(env("RP_PROCESSING_POLL_INTERVAL", "0.2")), "help": "等待加工完成时的轮询间隔秒数，默认 0.2"}),
            ("--wait-timeout", {"type": float, "default": float(env("RP_PROCESSING_JOB_TIMEOUT", "3600")), "help": "等待加工完成的超时秒数；0 表示不超时"}),
            ("--concurrency", {"type": int, "default": int(env("RP_UPLOAD_RESOURCES_CONCURRENCY", "32")), "help": "并发提交 worker 数，默认 32；传 1 使用串行"}),
            ("--force", {"action": "store_true", "help": "强制重新提交匹配资源；不重新上传对象"}),
            ("--resource-types", {"action": "append", "default": [], "help": "只提交指定资源类型；支持逗号分隔或重复传入，例如 atlas,tileset"}),
        ],
    )
    args = parser.parse_args()
    if args.wait_inline and args.detach:
        parser.error("--wait 不能和 --no-wait 同时使用")
    if args.force and args.resume:
        parser.error("--force 不能和 --resume 同时使用")
    if args.direct_search_upsert:
        parser.error("--direct-search-upsert 已废弃；客户端现在只提交到资源加工服务器")
    resource_types = _split_resource_type_values([args.resource_type, *args.resource_types])
    if args.force and not resource_types:
        parser.error("--force 必须配合 --resource-type 或 --resource-types，避免误重提所有资源类型")

    report = Report(label="加工任务提交")
    processing_server = args.processing_server or env("RP_PROCESSING_SERVER_URL", "http://localhost:8100")
    client_id = args.client_id or env("CLIENT_ID", "client")
    api_key = args.api_key or env("RP_PROCESSING_SERVER_API_KEY", env("RP_API_KEY", ""))
    cache = LocalCacheStore(args.db_path)
    config = _SubmitConfig(
        db_path=args.db_path,
        processing_server=processing_server,
        client_id=client_id,
        api_key=api_key,
        no_wait=not args.wait_inline,
        poll_interval=args.poll_interval,
        wait_timeout=args.wait_timeout,
        force=args.force,
    )

    generated = 0
    submitted = 0
    failed = 0
    manifest_handle = None
    try:
        if args.manifest_out:
            manifest_path = Path(args.manifest_out)
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_handle = open(manifest_path, "w", encoding="utf-8")

        skipped_clean = 0
        skipped_busy = 0
        skipped_missing_manifest = 0
        skipped_missing_source = 0
        skipped_non_indexable = 0
        skipped_not_ready = 0
        skipped_missing_preview = 0
        skipped_missing_description = 0
        if not args.dry_run and not args.manifest_out:
            reconciled = _reconcile_inflight_jobs(
                cache=cache,
                processing_server=processing_server,
                client_id=client_id,
                api_key=api_key,
                resource_types=resource_types,
                source=args.source_filter,
                report=report,
            )
            if reconciled["checked"]:
                report.ok(
                    "恢复旧任务",
                    f"检查 {reconciled['checked']}, 完成 {reconciled['completed']}, "
                    f"处理中 {reconciled['active']}, 待重提 {reconciled['failed']}",
                )
        session_context = nullcontext(None) if args.dry_run else requests.Session()
        use_concurrency = not args.dry_run and not args.manifest_out and int(args.concurrency or 1) >= 1
        with session_context as session:
            task_ids = cache.iter_tasks(
                limit=args.limit,
                resource_types=resource_types,
                source=args.source_filter,
            )
            if hasattr(cache, "iter_submit_candidate_task_ids"):
                task_ids = cache.iter_submit_candidate_task_ids(
                    limit=args.limit,
                    resource_types=resource_types,
                    source=args.source_filter,
                    force=args.force,
                )
            if use_concurrency:
                totals = _submit_concurrently(
                    task_ids=task_ids,
                    config=config,
                    concurrency=max(1, int(args.concurrency)),
                    report=report,
                )
                generated = totals["generated"]
                submitted = totals["submitted"]
                failed = totals["failed"]
                skipped_clean = totals["skipped_clean"]
                skipped_busy = totals["skipped_busy"]
                skipped_missing_manifest = totals["skipped_missing_manifest"]
                skipped_missing_source = totals["skipped_missing_source"]
                skipped_non_indexable = totals["skipped_non_indexable"]
                skipped_not_ready = totals["skipped_not_ready"]
                skipped_missing_preview = totals["skipped_missing_preview"]
                skipped_missing_description = totals["skipped_missing_description"]
                task_ids = ()
            for task_id in task_ids:
                task = cache.get_task_by_id(task_id)
                if task is not None and not _task_state_ready(task):
                    skipped_not_ready += 1
                    continue
                entity = cache.rebuild_entity_from_cache(task_id)
                if entity is None:
                    continue
                if entity.resource_type == PACK_RESOURCE_TYPE or not is_search_indexable_resource_type(entity.resource_type):
                    skipped_non_indexable += 1
                    continue
                object_record = cache.get_object_manifest(task_id)
                if object_record is None or object_record.get("upload_state") != "uploaded":
                    skipped_missing_manifest += 1
                    continue
                uploaded_manifest = object_record.get("manifest") or {}
                source_object = _source_object_from_uploaded_manifest(uploaded_manifest)
                if not isinstance(source_object, dict) or not source_object.get("object_key"):
                    skipped_missing_source += 1
                    report.fail("跳过", f"task_id={task_id} 缺少已上传源对象；请先运行 upload_objects_to_storage")
                    continue
                if not _has_uploaded_preview(uploaded_manifest):
                    skipped_missing_preview += 1
                    continue
                if not _has_local_description(entity):
                    skipped_missing_description += 1
                    continue

                current_fingerprint, _fingerprint_parts = compute_resource_fingerprint_for_connection(cache._conn, task_id)
                committed_fingerprint = str(object_record.get("committed_fingerprint") or "")
                if not args.force and committed_fingerprint == current_fingerprint:
                    skipped_clean += 1
                    continue

                manifest = _build_manifest_from_uploaded_record(
                    entity,
                    uploaded_manifest,
                    client_id=client_id,
                )
                generated += 1
                line = json.dumps(manifest, ensure_ascii=False)
                if manifest_handle is not None:
                    manifest_handle.write(line + "\n")
                    manifest_handle.flush()
                elif args.dry_run:
                    print(line)

                if args.dry_run:
                    continue

                if not cache.claim_object_manifest_for_submit(
                    task_id,
                    resource_fingerprint=current_fingerprint,
                    force=args.force,
                ):
                    skipped_busy += 1
                    continue

                try:
                    assert session is not None
                    created = submit_processing_job(
                        manifest,
                        processing_server=processing_server,
                        client_id=client_id,
                        api_key=api_key,
                        session=session,
                    )
                    cache.mark_object_manifest_submitting_job(
                        task_id,
                        created,
                        resource_fingerprint=current_fingerprint,
                    )
                    if config.no_wait:
                        result = created
                        cache.mark_object_manifest_queued(
                            task_id,
                            result,
                            resource_fingerprint=current_fingerprint,
                        )
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
                        cache.mark_object_manifest_submitted(
                            task_id,
                            result,
                            resource_fingerprint=current_fingerprint,
                        )
                    cache.add_log(task_id, "processing_job_submitted", json.dumps(result, ensure_ascii=False))
                    submitted += 1
                except Exception as exc:
                    failed += 1
                    cache.mark_object_manifest_submit_failed(task_id, str(exc))
                    cache.record_task_error(task_id, "processing_submit_error", str(exc)[:1000])
                    report.fail("提交失败", f"task_id={task_id}: {str(exc)[:160]}")
        if skipped_clean:
            report.ok("指纹未变化", f"{skipped_clean} 个资源已提交，跳过")
        if skipped_busy:
            report.ok("正在提交", f"{skipped_busy} 个资源已被其他 worker 或进程认领，跳过")
        if skipped_missing_manifest:
            report.ok("等待对象上传", f"{skipped_missing_manifest} 个资源对象 manifest 未就绪")
        if skipped_missing_source:
            report.ok("缺少源对象", f"{skipped_missing_source} 个资源未提交")
        if skipped_non_indexable:
            report.ok("跳过非索引资源", f"{skipped_non_indexable} 个资源不提交加工服务器")
        if skipped_not_ready:
            report.ok("等待处理完成", f"{skipped_not_ready} 个资源状态未到 description_ready")
        if skipped_missing_preview:
            report.ok("等待预览上传", f"{skipped_missing_preview} 个资源 manifest 缺少已上传预览")
        if skipped_missing_description:
            report.ok("等待描述", f"{skipped_missing_description} 个资源缺少本地描述")
        if not args.dry_run and not args.manifest_out and config.no_wait:
            reconciled = _reconcile_inflight_jobs(
                cache=cache,
                processing_server=processing_server,
                client_id=client_id,
                api_key=api_key,
                resource_types=resource_types,
                source=args.source_filter,
                report=report,
            )
            if reconciled["checked"]:
                report.ok(
                    "异步状态",
                    f"检查 {reconciled['checked']}, 完成 {reconciled['completed']}, "
                    f"处理中 {reconciled['active']}, 待重提 {reconciled['failed']}",
                )
        if (
            not args.dry_run
            and not args.manifest_out
            and config.no_wait
            and not args.detach
        ):
            waited = _wait_for_inflight_jobs(
                cache=cache,
                processing_server=processing_server,
                client_id=client_id,
                api_key=api_key,
                resource_types=resource_types,
                source=args.source_filter,
                poll_interval=args.poll_interval,
                report=report,
            )
            if waited["failed"]:
                report.ok("等待重提", f"{waited['failed']} 个失败任务将在下次运行时自动重提")
    finally:
        if manifest_handle is not None:
            manifest_handle.close()

    cache.close()
    report.ok("完成", f"生成 manifest {generated}, 提交 {submitted}, 失败 {failed}")
    return 0 if report.summary() else 1


if __name__ == "__main__":
    raise SystemExit(main())
