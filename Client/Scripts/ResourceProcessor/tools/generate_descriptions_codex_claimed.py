"""Generate Codex descriptions from SQLite with claim/lease concurrency.

This runner keeps one resource per ``codex exec`` call so every description has
a clean context. Concurrency is achieved by multiple workers claiming tasks from
SQLite with short transactions. The LLM call never holds a database lock.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import datetime as _dt
import json
import os
import random
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_CLIENT_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_CLIENT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CLIENT_SCRIPTS))

try:
    import ResourceProcessor.description.codex_exec_provider  # noqa: F401
except Exception:
    pass

from ResourceProcessor.cache.local_cache import LocalCacheStore  # noqa: E402
from ResourceProcessor.crawler.resource_adapter import build_description_input  # noqa: E402
from ResourceProcessor.description.description_generator import (  # noqa: E402
    DescriptionResult,
    build_description_result,
    generate_resource_description,
)
from ResourceProcessor.description.usage_classification import UsageClassification  # noqa: E402

LEASE_TABLE = "description_lease"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _future(seconds: int) -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=seconds)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=300, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=300000")
    return conn


def _ensure_lease_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {LEASE_TABLE} (
            task_id INTEGER PRIMARY KEY REFERENCES resource_task(id),
            owner TEXT NOT NULL,
            lease_until TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{LEASE_TABLE}_lease_until "
        f"ON {LEASE_TABLE}(lease_until)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_description_task_id "
        "ON resource_description(task_id)"
    )
    desc_cols = {row["name"] for row in conn.execute("PRAGMA table_info(resource_description)").fetchall()}
    for col_name, col_def in (
        ("usage_space", "TEXT NOT NULL DEFAULT ''"),
        ("usage_category", "TEXT NOT NULL DEFAULT ''"),
        ("usage_subcategories", "TEXT NOT NULL DEFAULT '[]'"),
        ("usage_classification_reason", "TEXT NOT NULL DEFAULT ''"),
        ("usage_classification_suggestion", "TEXT NOT NULL DEFAULT '{}'"),
        ("usage_classification_version", "TEXT NOT NULL DEFAULT ''"),
    ):
        if col_name not in desc_cols:
            conn.execute(f"ALTER TABLE resource_description ADD COLUMN {col_name} {col_def}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_file_task_id "
        "ON resource_file(task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_preview_task_id "
        "ON resource_preview(task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_task_state_id "
        "ON resource_task(process_state, id)"
    )


def _states_clause(retry_failed: bool) -> tuple[str, list[Any]]:
    if retry_failed:
        return (
            "(t.process_state = 'preview_ready' OR "
            "(t.process_state = 'description_failed' AND t.retry_count < ?))",
            [],
        )
    return "t.process_state = 'preview_ready'", []


def _claim_tasks(
    db_path: str,
    *,
    owner: str,
    limit: int,
    lease_seconds: int,
    retry_failed: bool,
    max_retries: int,
    resource_type: str,
    source: str,
) -> list[int]:
    if limit <= 0:
        return []

    conn = _connect(db_path)
    try:
        _ensure_lease_table(conn)
        now = _now()
        until = _future(lease_seconds)
        state_sql, params = _states_clause(retry_failed)
        if retry_failed:
            params.append(max_retries)

        where = [
            "t.resource_type <> 'audio_file'",
            state_sql,
            f"l.task_id IS NULL",
            "NOT EXISTS (SELECT 1 FROM resource_description d WHERE d.task_id = t.id)",
        ]
        if resource_type:
            where.append("t.resource_type = ?")
            params.append(resource_type)
        if source:
            where.append("t.source = ?")
            params.append(source)
        params.append(limit)

        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"DELETE FROM {LEASE_TABLE} WHERE lease_until < ?", (now,))
        rows = conn.execute(
            f"""
            SELECT t.id
            FROM resource_task t
            LEFT JOIN {LEASE_TABLE} l ON l.task_id = t.id
            WHERE {' AND '.join(where)}
            ORDER BY t.id
            LIMIT ?
            """,
            params,
        ).fetchall()
        task_ids = [int(row["id"]) for row in rows]
        for task_id in task_ids:
            conn.execute(
                f"""
                INSERT INTO {LEASE_TABLE}
                    (task_id, owner, lease_until, attempts, created_at, updated_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (task_id, owner, until, now, now),
            )
        conn.commit()
        return task_ids
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release_success(
    db_path: str,
    *,
    owner: str,
    task_id: int,
    result: DescriptionResult,
) -> bool:
    conn = _connect(db_path)
    try:
        now = _now()
        full = result.full_description or f"Main: {result.main_content}\nDetail: {result.detail_content}"
        conn.execute("BEGIN IMMEDIATE")
        lease = conn.execute(
            f"SELECT owner FROM {LEASE_TABLE} WHERE task_id = ?", (task_id,)
        ).fetchone()
        if lease is None or lease["owner"] != owner:
            conn.rollback()
            return False
        exists = conn.execute(
            "SELECT 1 FROM resource_description WHERE task_id = ? LIMIT 1",
            (task_id,),
        ).fetchone()
        if exists is None:
            conn.execute(
                """
                INSERT INTO resource_description
                    (task_id, main_content, detail_content, full_description,
                     prompt_version, quality_score, usage_space, usage_category,
                     usage_subcategories, usage_classification_reason,
                     usage_classification_suggestion, usage_classification_version,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    result.main_content,
                    result.detail_content,
                    full,
                    result.prompt_version,
                    result.description_quality_score,
                    result.usage_space,
                    result.usage_category,
                    json.dumps(result.usage_subcategories or [], ensure_ascii=False),
                    result.usage_classification_reason,
                    json.dumps(result.usage_classification_suggestion or {}, ensure_ascii=False),
                    result.usage_classification_version,
                    now,
                ),
            )
        conn.execute(
            """
            UPDATE resource_task
               SET process_state = 'description_ready',
                   last_error_code = '',
                   last_error_message = '',
                   updated_at = ?
             WHERE id = ?
            """,
            (now, task_id),
        )
        conn.execute(f"DELETE FROM {LEASE_TABLE} WHERE task_id = ?", (task_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _release_failure(
    db_path: str,
    *,
    owner: str,
    task_id: int,
    error_code: str,
    error_message: str,
) -> bool:
    conn = _connect(db_path)
    try:
        now = _now()
        conn.execute("BEGIN IMMEDIATE")
        lease = conn.execute(
            f"SELECT owner FROM {LEASE_TABLE} WHERE task_id = ?", (task_id,)
        ).fetchone()
        if lease is None or lease["owner"] != owner:
            conn.rollback()
            return False
        conn.execute(
            """
            UPDATE resource_task
               SET process_state = 'description_failed',
                   retry_count = retry_count + 1,
                   last_error_code = ?,
                   last_error_message = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (error_code, error_message[:500], now, task_id),
        )
        conn.execute(f"DELETE FROM {LEASE_TABLE} WHERE task_id = ?", (task_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _cleanup_owner_leases(db_path: str, owner: str) -> int:
    conn = _connect(db_path)
    try:
        _ensure_lease_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(f"DELETE FROM {LEASE_TABLE} WHERE owner = ?", (owner,))
        conn.commit()
        return int(cur.rowcount or 0)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _remaining_counts(db_path: str) -> dict[str, int]:
    conn = _connect(db_path)
    try:
        _ensure_lease_table(conn)
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN process_state = 'description_ready' THEN 1 ELSE 0 END) AS ready,
                SUM(CASE WHEN process_state = 'preview_ready' AND resource_type <> 'audio_file' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN process_state = 'description_failed' AND resource_type <> 'audio_file' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN process_state = 'preview_ready' AND resource_type = 'audio_file' THEN 1 ELSE 0 END) AS audio_pending
            FROM resource_task
            """
        ).fetchone()
        leased = conn.execute(f"SELECT COUNT(*) FROM {LEASE_TABLE}").fetchone()[0]
        return {
            "ready": int(row["ready"] or 0),
            "pending": int(row["pending"] or 0),
            "failed": int(row["failed"] or 0),
            "audio_pending": int(row["audio_pending"] or 0),
            "leased": int(leased or 0),
        }
    finally:
        conn.close()


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


def _rate_limit_ratio(counters: dict[str, int]) -> float:
    attempts = counters.get("requests", 0)
    if attempts <= 0:
        return 0.0
    return counters.get("rate_limited", 0) / attempts


async def _generate_with_retry(
    cache: LocalCacheStore,
    task_id: int,
    *,
    provider: str,
    max_attempts: int,
    base_delay_seconds: float,
    counters: dict[str, int],
) -> DescriptionResult:
    entity = cache.rebuild_entity_from_cache(task_id)
    if entity is None:
        raise RuntimeError("Task cannot be rebuilt from cache")

    desc_input = build_description_input(entity)
    if desc_input.resolved_llm_input_type == "audio":
        raise RuntimeError("Audio resources are skipped by Codex claimed runner")

    last_exc: Exception | None = None
    saw_rate_limit = False
    for attempt in range(1, max_attempts + 1):
        counters["requests"] += 1
        try:
            result = await generate_resource_description(desc_input, provider_name=provider)
            result = build_description_result(
                desc_input,
                main_content=result.main_content,
                detail_content=result.detail_content,
                prompt_version=result.prompt_version,
                description_quality_score=result.description_quality_score,
                classification=UsageClassification(
                    space=result.usage_space,
                    category=result.usage_category,
                    subcategories=result.usage_subcategories,
                    reason=result.usage_classification_reason,
                    suggestion=result.usage_classification_suggestion,
                    version=result.usage_classification_version,
                ),
            )
            if saw_rate_limit:
                counters["rate_limit_recovered"] += 1
            return result
        except Exception as exc:
            last_exc = exc
            is_rate_limit = _is_rate_limit_error(exc)
            if is_rate_limit:
                saw_rate_limit = True
                counters["rate_limited"] += 1
            if attempt >= max_attempts or not is_rate_limit:
                break
            delay = base_delay_seconds * (2 ** (attempt - 1)) + random.uniform(0.0, 1.0)
            await asyncio.sleep(delay)
    if last_exc is not None:
        if saw_rate_limit and _is_rate_limit_error(last_exc):
            counters["rate_limit_failed"] += 1
        raise last_exc
    raise RuntimeError("Description generation failed without exception")


async def _worker(
    name: str,
    args: argparse.Namespace,
    owner: str,
    counters: dict[str, int],
    counter_lock: asyncio.Lock,
) -> None:
    cache = LocalCacheStore(args.db_path)
    local_claims: list[int] = []
    try:
        while True:
            if not local_claims:
                async with counter_lock:
                    if args.limit is not None and counters["claimed"] >= args.limit:
                        break
                    claim_limit = args.claim_size
                    if args.limit is not None:
                        claim_limit = min(claim_limit, args.limit - counters["claimed"])
                    local_claims = _claim_tasks(
                        args.db_path,
                        owner=owner,
                        limit=claim_limit,
                        lease_seconds=args.lease_seconds,
                        retry_failed=args.retry_failed,
                        max_retries=args.max_retries,
                        resource_type=args.resource_type,
                        source=args.source,
                    )
                    counters["claimed"] += len(local_claims)
                if not local_claims:
                    break

            task_id = local_claims.pop(0)
            try:
                result = await _generate_with_retry(
                    cache,
                    task_id,
                    provider=args.llm_provider,
                    max_attempts=args.max_attempts,
                    base_delay_seconds=args.base_delay_seconds,
                    counters=counters,
                )
                if _release_success(args.db_path, owner=owner, task_id=task_id, result=result):
                    counters["ok"] += 1
                else:
                    counters["lost_lease"] += 1
            except Exception as exc:
                if _release_failure(
                    args.db_path,
                    owner=owner,
                    task_id=task_id,
                    error_code="desc_error",
                    error_message=str(exc),
                ):
                    counters["failed"] += 1
                else:
                    counters["lost_lease"] += 1
            finally:
                counters["processed"] += 1
                processed = counters["processed"]
                if processed <= args.progress_every or processed % args.progress_every == 0:
                    counts = _remaining_counts(args.db_path)
                    print(
                        f"[{_now()}] {name} processed={processed} ok={counters['ok']} "
                        f"failed={counters['failed']} lost_lease={counters['lost_lease']} "
                        f"requests={counters['requests']} "
                        f"rate_limited={counters['rate_limited']} "
                        f"rl_ratio={_rate_limit_ratio(counters):.3%} "
                        f"rate_limit_recovered={counters['rate_limit_recovered']} "
                        f"rate_limit_failed={counters['rate_limit_failed']} "
                        f"ready={counts['ready']} pending={counts['pending']} "
                        f"failed_state={counts['failed']} leased={counts['leased']}",
                        flush=True,
                    )
    finally:
        cache.close()


async def _run(args: argparse.Namespace) -> int:
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
    )

    owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    counts = _remaining_counts(args.db_path)
    print(
        f"Codex claimed descriptions owner={owner} workers={args.workers} "
        f"claim_size={args.claim_size} retry_failed={args.retry_failed} "
        f"ready={counts['ready']} pending={counts['pending']} "
        f"failed={counts['failed']} audio_pending={counts['audio_pending']} "
        f"leased={counts['leased']}",
        flush=True,
    )

    counters = {
        "claimed": 0,
        "processed": 0,
        "ok": 0,
        "failed": 0,
        "lost_lease": 0,
        "requests": 0,
        "rate_limited": 0,
        "rate_limit_recovered": 0,
        "rate_limit_failed": 0,
    }
    lock = asyncio.Lock()
    started = time.time()
    tasks = [
        asyncio.create_task(_worker(f"worker-{i + 1}", args, owner, counters, lock))
        for i in range(args.workers)
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        leftover = _cleanup_owner_leases(args.db_path, owner)
        if leftover:
            print(f"[{_now()}] cleaned leftover leases for owner={owner}: {leftover}", flush=True)
    elapsed = time.time() - started
    counts = _remaining_counts(args.db_path)
    print(
        f"Codex claimed complete processed={counters['processed']} ok={counters['ok']} "
        f"failed={counters['failed']} lost_lease={counters['lost_lease']} "
        f"requests={counters['requests']} rate_limited={counters['rate_limited']} "
        f"rl_ratio={_rate_limit_ratio(counters):.3%} "
        f"rate_limit_recovered={counters['rate_limit_recovered']} "
        f"rate_limit_failed={counters['rate_limit_failed']} "
        f"elapsed={elapsed:.1f}s ready={counts['ready']} pending={counts['pending']} "
        f"failed_state={counts['failed']} leased={counts['leased']}",
        flush=True,
    )
    return 0 if counters["failed"] == 0 and counters["lost_lease"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="DB-only claimed Codex descriptions")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--llm-provider", default=os.environ.get("CLIENT_LLM_PROVIDER", "codex"))
    parser.add_argument("--workers", "--concurrency", dest="workers", type=int, default=4)
    parser.add_argument("--claim-size", type=int, default=1)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--base-delay-seconds", type=float, default=3.0)
    parser.add_argument("--lease-seconds", type=int, default=1800)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--resource-type", default="")
    parser.add_argument("--source", default="")
    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.claim_size < 1:
        parser.error("--claim-size must be >= 1")
    if args.lease_seconds < 60:
        parser.error("--lease-seconds should be at least 60")

    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
