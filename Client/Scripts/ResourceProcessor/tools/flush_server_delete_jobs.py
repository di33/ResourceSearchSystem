"""Flush queued processing-server deletes recorded by crawler sync."""

from __future__ import annotations

import datetime
import os
import sqlite3
from pathlib import Path

import requests

from ResourceProcessor.cache.local_cache import LocalCacheStore
from ResourceProcessor.delete_processed_resource import delete_processed_resource
from ResourceProcessor.pipeline_common import Report, env, make_arg_parser, print_progress


TABLE = "resource_server_delete_job"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def flush(args) -> int:
    db_path = os.path.abspath(args.db_path)
    LocalCacheStore(db_path).close()
    report = Report(label="服务端资源删除队列")
    conn = sqlite3.connect(db_path, timeout=300)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=300000")
    sql = f"""SELECT * FROM {TABLE}
              WHERE status IN ('pending', 'failed') AND attempt_count < ?
              ORDER BY id"""
    params: list[object] = [args.max_attempts]
    if args.limit:
        sql += " LIMIT ?"
        params.append(args.limit)
    jobs = conn.execute(sql, params).fetchall()
    report.ok("待处理任务", f"{len(jobs)} 个")
    deleted = failed = 0
    processing_server = args.processing_server or env("RP_PROCESSING_SERVER_URL", "http://localhost:9000")
    client_id = args.client_id or env("CLIENT_ID", "client")
    api_key = args.api_key or env("RP_PROCESSING_SERVER_API_KEY", env("RP_API_KEY", ""))
    try:
        with requests.Session() as session:
            for index, row in enumerate(jobs, start=1):
                stable_id = str(row["client_resource_id"] or "")
                try:
                    result = delete_processed_resource(
                        processing_server=processing_server,
                        client_id=client_id,
                        api_key=api_key,
                        client_resource_id=stable_id,
                        delete_objects=False,
                        reason=str(row["reason"] or "crawler_removed"),
                        idempotency_key=f"{client_id}:{stable_id}:crawler-delete",
                        session=session,
                    )
                    state = str(result.get("state") or "")
                    if state not in {"deleted", "not_found"}:
                        raise RuntimeError(f"unexpected delete state: {state or '(empty)'}")
                    now = _now()
                    conn.execute(
                        f"""UPDATE {TABLE} SET status='deleted', last_error='',
                            updated_at=?, deleted_at=? WHERE id=?""",
                        (now, now, int(row["id"])),
                    )
                    conn.commit()
                    deleted += 1
                except Exception as exc:
                    conn.execute(
                        f"""UPDATE {TABLE} SET status='failed',
                            attempt_count=attempt_count+1, last_error=?, updated_at=?
                            WHERE id=?""",
                        (str(exc)[:1000], _now(), int(row["id"])),
                    )
                    conn.commit()
                    failed += 1
                    report.fail("删除失败", f"{stable_id}: {str(exc)[:160]}")
                if args.progress_every and index % args.progress_every == 0:
                    print_progress(index, len(jobs), f"删除 {deleted:,}, 失败 {failed:,}")
    finally:
        conn.close()
    report.ok("完成", f"删除 {deleted}, 失败 {failed}")
    return 0 if report.summary() else 1


def main() -> int:
    default_db = Path(__file__).resolve().parents[4] / "data" / "databases" / "pipeline.db"
    parser = make_arg_parser(
        "清理加工服务器已删除资源",
        extra_args=[
            ("--processing-server", {"default": None}),
            ("--client-id", {"default": None}),
            ("--api-key", {"default": None}),
            ("--max-attempts", {"type": int, "default": 10}),
            ("--progress-every", {"type": int, "default": 100}),
        ],
    )
    parser.set_defaults(db_path=str(default_db))
    return flush(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
