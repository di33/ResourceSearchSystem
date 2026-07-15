"""Submit existing object-storage manifests to the resource processing server."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

import requests
from requests import Response

from ResourceProcessor.pipeline_common import Report, env, make_arg_parser


_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
_DEFAULT_HTTP_RETRIES = 3
_DEFAULT_HTTP_RETRY_BACKOFF = 0.5


def _request_with_retries(
    http: requests.Session,
    method: str,
    url: str,
    *,
    retries: int = _DEFAULT_HTTP_RETRIES,
    retry_backoff: float = _DEFAULT_HTTP_RETRY_BACKOFF,
    **kwargs,
) -> Response:
    attempts = max(1, int(retries or 1))
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            if hasattr(http, "request"):
                response = http.request(method, url, **kwargs)
            else:
                response = getattr(http, method.lower())(url, **kwargs)
            status_code = getattr(response, "status_code", 200)
            if status_code not in _RETRYABLE_STATUS_CODES or attempt >= attempts:
                return response
            last_exc = requests.HTTPError(f"retryable status {status_code} for {method} {url}")
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt >= attempts:
                raise
        time.sleep(max(0.0, float(retry_backoff)) * attempt)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"request failed without response: {method} {url}")


def submit_processing_job(
    manifest: dict[str, Any],
    *,
    processing_server: str,
    client_id: str,
    api_key: str = "",
    session: requests.Session | None = None,
) -> dict[str, Any]:
    http = session or requests.Session()
    headers = {"X-Client-Id": client_id}
    if api_key:
        headers["X-API-Key"] = api_key
    response = _request_with_retries(
        http,
        "POST",
        f"{processing_server.rstrip('/')}/processing-jobs",
        json=manifest,
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def get_processing_job(
    job_id: str,
    *,
    processing_server: str,
    client_id: str,
    api_key: str = "",
    session: requests.Session | None = None,
) -> dict[str, Any]:
    http = session or requests.Session()
    headers = {"X-Client-Id": client_id}
    if api_key:
        headers["X-API-Key"] = api_key
    response = _request_with_retries(
        http,
        "GET",
        f"{processing_server.rstrip('/')}/processing-jobs/{job_id}",
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def get_processing_job_statuses(
    job_ids: list[str],
    *,
    processing_server: str,
    client_id: str,
    api_key: str = "",
    session: requests.Session | None = None,
) -> dict[str, Any]:
    normalized = list(dict.fromkeys(str(job_id or "").strip() for job_id in job_ids if str(job_id or "").strip()))
    if not normalized:
        return {"jobs": [], "missing_job_ids": []}
    if len(normalized) > 1000:
        raise ValueError("at most 1000 processing job ids can be queried at once")
    http = session or requests.Session()
    headers = {"X-Client-Id": client_id}
    if api_key:
        headers["X-API-Key"] = api_key
    response = _request_with_retries(
        http,
        "POST",
        f"{processing_server.rstrip('/')}/processing-jobs/status",
        json={"job_ids": normalized},
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def wait_processing_job(
    job_id: str,
    *,
    processing_server: str,
    client_id: str,
    api_key: str = "",
    poll_interval: float = 2.0,
    timeout_seconds: float = 3600.0,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    if not str(job_id or "").strip():
        raise RuntimeError("processing job id is required")
    deadline = time.monotonic() + timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
    while True:
        job = get_processing_job(
            job_id,
            processing_server=processing_server,
            client_id=client_id,
            api_key=api_key,
            session=session,
        )
        state = str(job.get("state") or "").lower()
        if state == "completed":
            return job
        if state in {"failed", "cancelled"}:
            error = str(job.get("error") or state)
            raise RuntimeError(f"processing job {job_id} {state}: {error}")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"processing job {job_id} did not complete within {timeout_seconds:g}s")
        time.sleep(max(0.1, float(poll_interval or 0.1)))


def submit_and_wait_processing_job(
    manifest: dict[str, Any],
    *,
    processing_server: str,
    client_id: str,
    api_key: str = "",
    poll_interval: float = 2.0,
    timeout_seconds: float = 3600.0,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    created = submit_processing_job(
        manifest,
        processing_server=processing_server,
        client_id=client_id,
        api_key=api_key,
        session=session,
    )
    job_id = str(created.get("job_id") or "")
    if not job_id:
        raise RuntimeError(f"processing server did not return job_id: {created}")
    completed = wait_processing_job(
        job_id,
        processing_server=processing_server,
        client_id=client_id,
        api_key=api_key,
        poll_interval=poll_interval,
        timeout_seconds=timeout_seconds,
        session=session,
    )
    return {**created, **completed}


def submit_processing_batch(
    manifests: list[dict[str, Any]],
    *,
    processing_server: str,
    client_id: str,
    api_key: str = "",
    session: requests.Session | None = None,
) -> dict[str, Any]:
    http = session or requests.Session()
    headers = {"X-Client-Id": client_id}
    if api_key:
        headers["X-API-Key"] = api_key
    response = _request_with_retries(
        http,
        "POST",
        f"{processing_server.rstrip('/')}/processing-jobs/batch",
        json={"manifests": manifests},
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def load_manifest_records(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        if text.startswith("["):
            loaded = json.loads(text)
            if not isinstance(loaded, list):
                raise ValueError(f"manifest file must contain an object, array, or JSONL: {path}")
            records.extend(loaded)
            continue
        if text.startswith("{"):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                records.append(loaded)
                continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            loaded = json.loads(line)
            if not isinstance(loaded, dict):
                raise ValueError(f"manifest JSONL line must be an object: {path}:{line_no}")
            records.append(loaded)
    return records


def submit_manifest_records(
    records: Iterable[tuple[int | None, dict[str, Any]]],
    *,
    processing_server: str,
    client_id: str,
    api_key: str = "",
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    results = []
    close_session = session is None
    http = session or requests.Session()
    try:
        for task_id, manifest in records:
            if dry_run:
                results.append({
                    "task_id": task_id,
                    "client_resource_id": manifest.get("client_resource_id", ""),
                    "state": "dry_run",
                })
                continue
            results.append(
                submit_processing_job(
                    manifest,
                    processing_server=processing_server,
                    client_id=client_id,
                    api_key=api_key,
                    session=http,
                )
            )
    finally:
        if close_session:
            http.close()
    return results


def main() -> int:
    parser = make_arg_parser(
        "提交已有加工 manifest 到资源加工服务器",
        extra_args=[
            ("--manifest", {"action": "append", "default": [], "help": "manifest JSON/JSONL 文件，可重复传入；未传则从本地 DB 读取"}),
            ("--processing-server", {"default": None, "help": "资源加工服务器地址，默认 RP_PROCESSING_SERVER_URL 或 http://localhost:9000"}),
            ("--client-id", {"default": None, "help": "客户端 ID，会写入 X-Client-Id 请求头"}),
            ("--api-key", {"default": None, "help": "资源加工服务器 API key，默认 RP_PROCESSING_SERVER_API_KEY/RP_API_KEY"}),
            ("--submit-state", {"default": "pending", "help": "从 DB 读取时筛选提交状态，默认 pending；传空字符串则不过滤"}),
            ("--batch-size", {"type": int, "default": 1, "help": "每次提交多少条 manifest；大于 1 时调用 /processing-jobs/batch"}),
            ("--no-wait", {"action": "store_true", "help": "只提交到加工服务器并记录 queued，不等待加工完成"}),
            ("--poll-interval", {"type": float, "default": 2.0, "help": "等待加工完成时的轮询间隔秒数，默认 2"}),
            ("--wait-timeout", {"type": float, "default": float(env("RP_PROCESSING_JOB_TIMEOUT", "3600")), "help": "等待加工完成的超时秒数；0 表示不超时"}),
            ("--dry-run", {"action": "store_true", "help": "只读取 manifest，不提交"}),
        ],
    )
    args = parser.parse_args()

    report = Report(label="加工任务提交")
    processing_server = args.processing_server or env("RP_PROCESSING_SERVER_URL", "http://localhost:9000")
    client_id = args.client_id or env("CLIENT_ID", "client")
    api_key = args.api_key or env("RP_PROCESSING_SERVER_API_KEY", env("RP_API_KEY", ""))
    cache = None
    if args.manifest:
        records = [(None, manifest) for manifest in load_manifest_records(args.manifest)]
    else:
        from ResourceProcessor.cache.local_cache import LocalCacheStore

        cache = LocalCacheStore(args.db_path)
        records = [
            (int(row["task_id"]), row["manifest"])
            for row in cache.iter_object_manifests(
                limit=args.limit,
                resource_type=args.resource_type,
                source=args.source_filter,
                submit_state=args.submit_state,
            )
        ]

    submitted = 0
    failed = 0
    with requests.Session() as session:
        batch_size = max(1, int(args.batch_size or 1))
        for start in range(0, len(records), batch_size):
            chunk = records[start:start + batch_size]
            try:
                if args.dry_run:
                    results = [
                        {
                            "task_id": task_id,
                            "client_resource_id": manifest.get("client_resource_id", ""),
                            "state": "dry_run",
                        }
                        for task_id, manifest in chunk
                    ]
                elif len(chunk) == 1:
                    task_id, manifest = chunk[0]
                    created = submit_processing_job(
                        manifest,
                        processing_server=processing_server,
                        client_id=client_id,
                        api_key=api_key,
                        session=session,
                    )
                    results = [
                        created if args.no_wait else {
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
                    ]
                    if task_id is not None:
                        results[0]["client_resource_id"] = manifest.get("client_resource_id", "")
                else:
                    response = submit_processing_batch(
                        [manifest for _, manifest in chunk],
                        processing_server=processing_server,
                        client_id=client_id,
                        api_key=api_key,
                        session=session,
                    )
                    jobs = response.get("jobs") or []
                    results = [
                        {
                            **(jobs[index] if index < len(jobs) else {}),
                            "batch_id": response.get("batch_id", ""),
                        }
                        for index, _ in enumerate(chunk)
                    ]
                    if not args.no_wait:
                        waited_results = []
                        for result in results:
                            job_id = str(result.get("job_id") or "")
                            waited = wait_processing_job(
                                job_id,
                                processing_server=processing_server,
                                client_id=client_id,
                                api_key=api_key,
                                poll_interval=args.poll_interval,
                                timeout_seconds=args.wait_timeout,
                                session=session,
                            )
                            waited_results.append({**result, **waited})
                        results = waited_results
                for (task_id, _manifest), result in zip(chunk, results):
                    if cache is not None and task_id is not None and not args.dry_run:
                        if args.no_wait:
                            cache.mark_object_manifest_queued(task_id, result)
                        else:
                            cache.mark_object_manifest_submitted(task_id, result)
                        cache.add_log(task_id, "processing_job_submitted", json.dumps(result, ensure_ascii=False))
                    submitted += 1
                    print(json.dumps(result, ensure_ascii=False))
            except Exception as exc:
                failed += len(chunk)
                for task_id, _manifest in chunk:
                    if cache is not None and task_id is not None:
                        cache.mark_object_manifest_submit_failed(task_id, str(exc))
                        cache.record_task_error(task_id, "processing_submit_error", str(exc)[:1000])
                report.fail("提交失败", f"batch_start={start}: {str(exc)[:160]}")
    if cache is not None:
        cache.close()
    report.ok("完成", f"提交 {submitted}, 失败 {failed}")
    return 0 if report.summary() else 1


if __name__ == "__main__":
    raise SystemExit(main())
