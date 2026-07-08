"""Delete processed resources through resource_processing_server."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import requests

from ResourceProcessor.pipeline_common import Report, env, make_arg_parser


LOCAL_CHILD_TABLES = (
    "description_lease",
    "resource_object_manifest",
    "resource_description",
    "resource_upload_job",
    "process_log",
    "resource_preview",
    "resource_file",
)


def delete_processed_resource(
    *,
    processing_server: str,
    client_id: str,
    api_key: str = "",
    client_resource_id: str = "",
    resource_id: str = "",
    delete_objects: bool = True,
    reason: str = "",
    idempotency_key: str = "",
    session: requests.Session | None = None,
) -> dict[str, Any]:
    http = session or requests.Session()
    headers = {"X-Client-Id": client_id}
    if api_key:
        headers["X-API-Key"] = api_key
    response = http.post(
        f"{processing_server.rstrip('/')}/processed-resources/delete",
        json={
            "client_resource_id": client_resource_id,
            "resource_id": resource_id,
            "idempotency_key": idempotency_key,
            "delete_objects": delete_objects,
            "reason": reason,
        },
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    return response.json()


def _split_values(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for part in str(value or "").replace(";", ",").split(","):
            text = part.strip()
            if text:
                result.append(text)
    return list(dict.fromkeys(result))


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not _table_exists(conn, table_name):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def cleanup_local_pipeline_db(db_path: str, client_resource_id: str, *, dry_run: bool = False) -> dict[str, int]:
    path = Path(db_path)
    if not path.exists():
        return {"tasks": 0, "child_rows": 0}

    conn = sqlite3.connect(str(path), timeout=300)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        task_columns = _table_columns(conn, "resource_task")
        if "source_resource_id" not in task_columns:
            return {"tasks": 0, "child_rows": 0}
        rows = conn.execute(
            "SELECT id FROM resource_task WHERE source_resource_id = ?",
            (client_resource_id,),
        ).fetchall()
        task_ids = [int(row["id"]) for row in rows]
        if not task_ids:
            return {"tasks": 0, "child_rows": 0}

        placeholders = ",".join("?" for _ in task_ids)
        child_rows = 0
        for table_name in LOCAL_CHILD_TABLES:
            if not _table_exists(conn, table_name):
                continue
            columns = _table_columns(conn, table_name)
            if "task_id" not in columns:
                continue
            child_rows += int(conn.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE task_id IN ({placeholders})",
                task_ids,
            ).fetchone()[0])
            if not dry_run:
                conn.execute(f"DELETE FROM {table_name} WHERE task_id IN ({placeholders})", task_ids)

        if not dry_run:
            conn.execute(f"DELETE FROM resource_task WHERE id IN ({placeholders})", task_ids)
            conn.commit()
        return {"tasks": len(task_ids), "child_rows": child_rows}
    finally:
        conn.close()


def main() -> int:
    parser = make_arg_parser(
        "删除已加工资源",
        extra_args=[
            ("--processing-server", {"default": None, "help": "资源加工服务器地址，默认 RP_PROCESSING_SERVER_URL 或 http://localhost:8100"}),
            ("--client-id", {"default": None, "help": "客户端 ID，会写入 X-Client-Id 请求头"}),
            ("--api-key", {"default": None, "help": "资源加工服务器 API key，默认 RP_PROCESSING_SERVER_API_KEY/RP_API_KEY"}),
            ("--client-resource-id", {"action": "append", "default": [], "help": "客户端资源 ID；支持逗号分隔或重复传入"}),
            ("--resource-id", {"action": "append", "default": [], "help": "SearchServer resource_id；支持逗号分隔或重复传入"}),
            ("--no-delete-objects", {"action": "store_true", "help": "只删除服务端 DB/向量和 snapshot，不删除对象存储文件"}),
            ("--keep-local-db", {"action": "store_true", "help": "远端删除成功后不清理本地 pipeline.db"}),
            ("--reason", {"default": "client_delete", "help": "删除原因，写入删除请求"}),
            ("--dry-run", {"action": "store_true", "help": "只打印删除计划，不提交"}),
        ],
    )
    args = parser.parse_args()

    client_resource_ids = _split_values(args.client_resource_id)
    resource_ids = _split_values(args.resource_id)
    if not client_resource_ids and not resource_ids:
        parser.error("必须传 --client-resource-id 或 --resource-id")

    report = Report(label="删除已加工资源")
    processing_server = args.processing_server or env("RP_PROCESSING_SERVER_URL", "http://localhost:8100")
    client_id = args.client_id or env("CLIENT_ID", "client")
    api_key = args.api_key or env("RP_PROCESSING_SERVER_API_KEY", env("RP_API_KEY", ""))
    delete_objects = not args.no_delete_objects
    submitted = 0
    failed = 0

    requests_to_send = [
        {"client_resource_id": value, "resource_id": ""}
        for value in client_resource_ids
    ]
    requests_to_send.extend(
        {"client_resource_id": "", "resource_id": value}
        for value in resource_ids
    )

    with requests.Session() as session:
        for item in requests_to_send:
            stable_id = item["client_resource_id"] or item["resource_id"]
            idempotency_key = f"{client_id}:{stable_id}:delete"
            if args.dry_run:
                print(json.dumps({
                    **item,
                    "idempotency_key": idempotency_key,
                    "delete_objects": delete_objects,
                    "reason": args.reason,
                    "state": "dry_run",
                }, ensure_ascii=False))
                submitted += 1
                continue
            try:
                result = delete_processed_resource(
                    processing_server=processing_server,
                    client_id=client_id,
                    api_key=api_key,
                    client_resource_id=item["client_resource_id"],
                    resource_id=item["resource_id"],
                    idempotency_key=idempotency_key,
                    delete_objects=delete_objects,
                    reason=args.reason,
                    session=session,
                )
                if (
                    item["client_resource_id"]
                    and not args.keep_local_db
                    and result.get("state") in {"deleted", "not_found"}
                ):
                    result["local_db_cleanup"] = cleanup_local_pipeline_db(
                        args.db_path,
                        item["client_resource_id"],
                        dry_run=False,
                    )
                submitted += 1
                print(json.dumps(result, ensure_ascii=False))
            except Exception as exc:
                failed += 1
                report.fail("删除失败", f"{stable_id}: {str(exc)[:160]}")

    report.ok("完成", f"请求 {submitted}, 失败 {failed}")
    return 0 if report.summary() else 1


if __name__ == "__main__":
    raise SystemExit(main())
