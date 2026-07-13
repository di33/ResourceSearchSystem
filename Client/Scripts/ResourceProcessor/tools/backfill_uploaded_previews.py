"""Backfill uploaded preview refs into existing object manifests.

This is useful for historical manifests that uploaded source objects before
local previews existed. The tool reuses the existing source object, uploads only
local preview files, and marks the manifest pending so upload_resources can
re-submit it to SearchServer.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from ObjectStorageUpload.resource_manifest import (
    _thread_uploader,
    object_fingerprint_for_entity,
    upload_entity_objects,
    upload_options_payload,
)
from ObjectStorageUpload.storage_profiles import load_storage_profiles
from ResourceProcessor.cache.local_cache import LocalCacheStore
from ResourceProcessor.pipeline_common import Report, env, make_arg_parser


@dataclass(frozen=True)
class _BackfillConfig:
    db_path: str
    client_id: str
    storage_profile_id: str
    key_prefix: str
    include_descriptions: bool
    dry_run: bool
    submit_state_after: str


@dataclass
class _BackfillResult:
    task_id: int
    uploaded: int = 0
    skipped: int = 0
    failed: int = 0
    reason: str = ""


def _split_values(values) -> list[str]:
    result: list[str] = []
    for value in values or []:
        for part in str(value or "").replace(";", ",").split(","):
            text = part.strip()
            if text:
                result.append(text)
    return list(dict.fromkeys(result))


def _manifest_has_preview_objects(manifest: dict[str, Any]) -> bool:
    return any(
        isinstance(item, dict) and item.get("object_key")
        for item in manifest.get("previews") or []
    )


def _select_candidate_task_ids(
    db_path: str,
    *,
    resource_types: list[str],
    process_states: list[str],
    submit_states: list[str],
    limit: int | None,
) -> list[int]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        sql = """
            SELECT rt.id
            FROM resource_task rt
            JOIN resource_object_manifest rom ON rom.task_id = rt.id
            WHERE rom.upload_state = 'uploaded'
              AND EXISTS (SELECT 1 FROM resource_preview rp WHERE rp.task_id = rt.id)
              AND COALESCE(json_array_length(json_extract(rom.manifest_json, '$.previews')), 0) = 0
        """
        params: list[Any] = []
        if resource_types:
            placeholders = ",".join("?" for _ in resource_types)
            sql += f" AND rt.resource_type IN ({placeholders})"
            params.extend(resource_types)
        if process_states:
            placeholders = ",".join("?" for _ in process_states)
            sql += f" AND rt.process_state IN ({placeholders})"
            params.extend(process_states)
        if submit_states:
            placeholders = ",".join("?" for _ in submit_states)
            sql += f" AND rom.submit_state IN ({placeholders})"
            params.extend(submit_states)
        sql += " ORDER BY rt.id"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [int(row["id"]) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _backfill_one(task_id: int, config: _BackfillConfig) -> _BackfillResult:
    cache = LocalCacheStore(config.db_path)
    try:
        entity = cache.rebuild_entity_from_cache(task_id)
        if entity is None:
            return _BackfillResult(task_id, skipped=1, reason="missing entity")
        if not entity.previews:
            return _BackfillResult(task_id, skipped=1, reason="missing local previews")
        if not any(preview.path and Path(preview.path).is_file() for preview in entity.previews):
            return _BackfillResult(task_id, skipped=1, reason="missing preview files")

        old_record = cache.get_object_manifest(task_id)
        old_manifest = old_record.get("manifest") if old_record else {}
        if not isinstance(old_manifest, dict) or not old_manifest:
            return _BackfillResult(task_id, skipped=1, reason="missing object manifest")
        if _manifest_has_preview_objects(old_manifest):
            return _BackfillResult(task_id, skipped=1, reason="already has previews")

        uploader = None if config.dry_run else _thread_uploader(config.storage_profile_id)
        manifest = upload_entity_objects(
            entity,
            uploader=uploader,
            client_id=config.client_id,
            include_previews=True,
            key_prefix=config.key_prefix,
            include_descriptions=config.include_descriptions,
            storage_profile_id=config.storage_profile_id,
            dry_run=config.dry_run,
            package_object=old_manifest.get("package_object") or None,
            reuse_source_manifest=old_manifest,
        )
        for key in ("source_files", "child_resources"):
            if key in old_manifest and key not in manifest:
                manifest[key] = old_manifest[key]
        if not _manifest_has_preview_objects(manifest):
            return _BackfillResult(task_id, skipped=1, reason="no uploaded preview refs")

        if not config.dry_run:
            object_fingerprint, _ = object_fingerprint_for_entity(
                entity,
                client_id=config.client_id,
                storage_profile_id=config.storage_profile_id,
                key_prefix=config.key_prefix,
                include_previews=True,
            )
            cache.upsert_object_manifest(
                task_id,
                manifest,
                submit_state=config.submit_state_after,
                object_fingerprint=object_fingerprint,
                upload_options=upload_options_payload(
                    client_id=config.client_id,
                    storage_profile_id=config.storage_profile_id,
                    key_prefix=config.key_prefix,
                    include_previews=True,
                    include_descriptions=config.include_descriptions,
                ),
            )
            cache.add_log(task_id, "preview_manifest_backfilled", f"previews={len(manifest.get('previews') or [])}")
        return _BackfillResult(task_id, uploaded=1)
    except Exception as exc:
        try:
            cache.record_task_error(task_id, "preview_manifest_backfill_error", str(exc)[:1000])
        except Exception:
            pass
        return _BackfillResult(task_id, failed=1, reason=str(exc)[:200])
    finally:
        cache.close()


def main() -> int:
    parser = make_arg_parser(
        "补刷已上传对象 manifest 中缺失的预览引用",
        extra_args=[
            ("--client-id", {"default": None, "help": "客户端 ID，默认 CLIENT_ID/client"}),
            ("--storage-profile-id", {"default": None, "help": "对象存储 profile ID"}),
            ("--key-prefix", {"default": "", "help": "对象 key 根前缀"}),
            ("--resource-types", {"action": "append", "default": [], "help": "资源类型，可逗号分隔或重复传入"}),
            ("--process-states", {"action": "append", "default": [], "help": "任务状态，可逗号分隔或重复传入"}),
            ("--submit-states", {"action": "append", "default": [], "help": "manifest 提交状态，可逗号分隔或重复传入"}),
            ("--no-descriptions", {"action": "store_true", "help": "刷新 manifest 时不携带本地描述"}),
            ("--submit-state-after", {"default": "pending", "help": "补刷成功后写入的 submit_state，默认 pending"}),
            ("--dry-run", {"action": "store_true", "help": "只规划，不上传预览、不写 DB"}),
            ("--workers", {"type": int, "default": int(env("PREVIEW_BACKFILL_WORKERS", "16")), "help": "并发 worker 数，默认 16"}),
        ],
    )
    args = parser.parse_args()
    report = Report(label="预览 manifest 补刷")
    resource_types = _split_values([args.resource_type, *args.resource_types]) or ["animation_sequence"]
    process_states = _split_values(args.process_states) or ["committed"]
    submit_states = _split_values(args.submit_states) or ["submitted"]
    resolved_profile_id = load_storage_profiles().get(args.storage_profile_id or None).profile_id
    config = _BackfillConfig(
        db_path=os.path.abspath(args.db_path),
        client_id=args.client_id or env("CLIENT_ID", "client"),
        storage_profile_id=resolved_profile_id,
        key_prefix=args.key_prefix or "",
        include_descriptions=not args.no_descriptions,
        dry_run=bool(args.dry_run),
        submit_state_after=str(args.submit_state_after or "pending"),
    )
    task_ids = _select_candidate_task_ids(
        config.db_path,
        resource_types=resource_types,
        process_states=process_states,
        submit_states=submit_states,
        limit=args.limit,
    )
    report.ok("候选资源", f"{len(task_ids)} 个")
    if not task_ids:
        report.ok("完成", "无资源需要补刷")
        return 0 if report.summary() else 1

    uploaded = skipped = failed = 0
    started = time.time()
    workers = max(1, int(args.workers or 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_backfill_one, task_id, config) for task_id in task_ids]
        for index, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            result = future.result()
            uploaded += result.uploaded
            skipped += result.skipped
            failed += result.failed
            if result.failed:
                report.fail("补刷失败", f"task_id={result.task_id}: {result.reason}")
            elif result.skipped and result.reason not in {"already has previews"}:
                report.ok("跳过", f"task_id={result.task_id}: {result.reason}")
            if index % 100 == 0 or index == len(task_ids):
                elapsed = max(0.001, time.time() - started)
                print(
                    f"  progress processed={index}/{len(task_ids)} uploaded={uploaded} "
                    f"skipped={skipped} failed={failed} rate={index / elapsed:.1f}/s",
                    flush=True,
                )

    report.ok("完成", f"补刷 {uploaded}, 跳过 {skipped}, 失败 {failed}")
    return 0 if report.summary() and failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
