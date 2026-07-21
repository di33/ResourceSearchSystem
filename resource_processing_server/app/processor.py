from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from resource_contracts.path_safety import safe_file_name, safe_join_under
from resource_contracts.file_structure import scan_source_file_structure
from resource_contracts.resource_types import is_search_indexable_resource_type
from resource_processing_server.app.adapters import (
    build_processing_entity,
    description_result_from_provided,
    generate_description,
    generate_descriptions_batch,
    generate_previews,
    preview_ref_from_info,
)
from resource_processing_server.app.config import settings
from resource_processing_server.app.database import PostgresDatabase, migrate_legacy_sqlite
from resource_processing_server.app.legacy import ensure_resource_processor_imports
from resource_processing_server.app.models import (
    ChildResourceManifest,
    BatchJobOut,
    Classification,
    CreateJobOut,
    CreateBatchOut,
    DeleteProcessedResourceIn,
    DeleteProcessedResourceOut,
    DeletedObjectRef,
    JobOut,
    JobStatusOut,
    JobState,
    JobStep,
    FileStructure,
    ProcessingJob,
    PreviewRef,
    ReplaySnapshotOut,
    ResourceManifest,
)
from resource_processing_server.app.preview_renderer_client import PreviewRendererClient
from resource_processing_server.app.search_client import SearchServerClient
from resource_processing_server.app.source_files import resolve_local_source_files
from resource_processing_server.app.snapshots import ProcessedSnapshotStore
from resource_processing_server.app.snapshots import sqlite_connection
from resource_processing_server.app.storage import ObjectStorage, preview_object_name


logger = logging.getLogger(__name__)


def _error_text(exc: BaseException) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else repr(exc)


def _fingerprint(manifest: ResourceManifest) -> str:
    return _fingerprint_from_object(manifest.source_object, manifest.file_structure, manifest.package_object)


def _manifest_fingerprint(manifest: ResourceManifest) -> str:
    payload = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fingerprint_from_child(manifest: ChildResourceManifest) -> str:
    return _fingerprint_from_object(manifest.source_object, manifest.file_structure, manifest.package_object)


def _fingerprint_from_object(source_object, file_structure, package_object=None) -> str:
    payload = [f"{source_object.storage_profile_id}/{source_object.object_key}:{source_object.checksum or source_object.size}"]
    payload.extend(
        f"{item.path}:{item.checksum or item.size}"
        for item in (file_structure.entries if file_structure else [])
    )
    if package_object is not None:
        payload.append(f"package:{package_object.storage_profile_id}/{package_object.object_key}")
    return hashlib.sha256("|".join(sorted(payload)).encode("utf-8")).hexdigest()


def _ensure_indexable_manifest(manifest: ResourceManifest | ChildResourceManifest) -> None:
    if not is_search_indexable_resource_type(manifest.resource_type):
        raise ValueError(f"resource_type {manifest.resource_type!r} is not submitted to processing")


def _step(name: str, state: str, start: float, error: str = "") -> JobStep:
    return JobStep(name=name, state=state, duration_ms=int((time.perf_counter() - start) * 1000), error=error)


def _has_payload_value(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0:
        return False
    return True


def _compact_payload(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if _has_payload_value(value)}


def _object_ref_payload(ref) -> dict[str, Any]:
    return _compact_payload(ref.model_dump())


def _preview_ref_payload(ref: PreviewRef) -> dict[str, Any]:
    return _compact_payload(ref.model_dump())


def _preview_source(preview_refs: list[PreviewRef]) -> str:
    if preview_refs and all((ref.origin or "") == "provided" for ref in preview_refs):
        return "provided"
    return "generated"


def _as_child(manifest: ResourceManifest | ChildResourceManifest) -> ChildResourceManifest:
    if isinstance(manifest, ChildResourceManifest):
        return manifest
    return ChildResourceManifest(
        client_resource_id=manifest.client_resource_id,
        resource_type=manifest.resource_type,
        source_object=manifest.source_object,
        file_structure=manifest.file_structure,
        package_object=manifest.package_object,
        previews=manifest.previews,
        description=manifest.description,
        description_context=manifest.description_context,
        client_metadata=manifest.client_metadata,
        classification=manifest.classification,
        options=manifest.options,
    )


def _append_payload_object_ref(
    refs: list[dict[str, str]],
    *,
    storage_profile_id: str = "",
    object_key: str = "",
    kind: str = "",
    origin: str = "",
    renderer: str = "",
) -> None:
    key = str(object_key or "").strip()
    if not key:
        return
    refs.append({
        "storage_profile_id": str(storage_profile_id or ""),
        "object_key": key,
        "kind": kind,
        "origin": str(origin or ""),
        "renderer": str(renderer or ""),
    })


def _object_refs_from_snapshot(payload: dict[str, Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    source_object = payload.get("source_object")
    if isinstance(source_object, dict):
        _append_payload_object_ref(
            refs,
            storage_profile_id=str(source_object.get("storage_profile_id") or ""),
            object_key=str(source_object.get("object_key") or ""),
            kind="source_object",
        )
    for item in payload.get("previews") or []:
        if isinstance(item, dict):
            _append_payload_object_ref(
                refs,
                storage_profile_id=str(item.get("storage_profile_id") or ""),
                object_key=str(item.get("object_key") or ""),
                kind="preview",
                origin=str(item.get("origin") or ""),
                renderer=str(item.get("renderer") or ""),
            )
    return refs


def _dedupe_object_refs(refs: list[dict[str, Any]]) -> list[dict[str, str]]:
    deduped: dict[tuple[str, str], dict[str, str]] = {}
    for ref in refs:
        profile_id = str(ref.get("storage_profile_id") or "")
        key = str(ref.get("object_key") or "").strip()
        if not key:
            continue
        deduped.setdefault((profile_id, key), {
            "storage_profile_id": profile_id,
            "object_key": key,
            "kind": str(ref.get("kind") or ""),
            "origin": str(ref.get("origin") or ""),
            "renderer": str(ref.get("renderer") or ""),
        })
    return list(deduped.values())


def _generated_preview_refs(refs: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        ref for ref in _dedupe_object_refs(refs)
        if ref.get("kind") == "preview" and ref.get("origin") == "generated"
    ]


def _preview_refs_for_delete(preview_refs: list[PreviewRef]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for ref in preview_refs:
        _append_payload_object_ref(
            refs,
            storage_profile_id=ref.storage_profile_id,
            object_key=ref.object_key,
            kind="preview",
            origin=ref.origin,
            renderer=ref.renderer,
        )
    return refs


def _first_non_empty(*values: str) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _manifest_title(manifest: ChildResourceManifest) -> str:
    entries = manifest.file_structure.entries if manifest.file_structure else []
    first_source = entries[0] if entries else None
    return _first_non_empty(
        getattr(first_source, "name", "") if first_source is not None else "",
        manifest.source_object.file_name,
        Path(manifest.source_object.object_key).name,
        manifest.client_resource_id,
    )


def _classification_payload(classification: Classification | None, description) -> dict[str, Any]:
    payload = {
        "category": "",
        "tags": [],
        "style": [],
        "materials": [],
        "use_cases": [],
        "usage_space": description.usage_space,
        "usage_category": description.usage_category,
        "usage_subcategories": description.usage_subcategories,
        "usage_classification_reason": description.usage_classification_reason,
        "usage_classification_suggestion": description.usage_classification_suggestion or {},
        "usage_classification_version": description.usage_classification_version,
    }
    if classification is not None and classification.has_values():
        for key, value in classification.model_dump().items():
            if value not in ("", [], {}, None):
                payload[key] = value
    return payload


class JobCancelledError(RuntimeError):
    pass


class DescriptionBatcher:
    def __init__(self):
        self._pending: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._timer_task: asyncio.Task | None = None

    async def describe(self, *, entity, preview_paths: list[str], description_context: Any | None):
        if not settings.description_batch_enabled:
            return await generate_description(
                entity=entity,
                preview_paths=preview_paths,
                description_context=description_context,
            )

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        request = {
            "entity": entity,
            "preview_paths": preview_paths,
            "description_context": description_context,
            "future": future,
        }
        async with self._lock:
            self._pending.append(request)
            if len(self._pending) >= max(1, settings.description_batch_min_size):
                self._cancel_timer_locked()
                asyncio.create_task(self.flush())
            elif self._timer_task is None or self._timer_task.done():
                self._timer_task = asyncio.create_task(self._flush_after_wait())
        return await future

    async def _flush_after_wait(self) -> None:
        await asyncio.sleep(max(0.0, settings.description_batch_max_wait_seconds))
        await self.flush()

    async def flush(self) -> None:
        async with self._lock:
            if not self._pending:
                return
            batch = self._pending[: max(1, settings.description_batch_max_size)]
            self._pending = self._pending[len(batch):]
            self._timer_task = None
            if self._pending:
                self._timer_task = asyncio.create_task(self._flush_after_wait())

        futures = [item["future"] for item in batch]
        try:
            results = await generate_descriptions_batch(batch)
        except Exception as exc:
            for future in futures:
                if not future.done():
                    future.set_exception(exc)
            return

        if len(results) != len(futures):
            error = RuntimeError("description batch result count mismatch")
            for future in futures:
                if not future.done():
                    future.set_exception(error)
            return

        for future, result in zip(futures, results):
            if not future.done():
                future.set_result(result)

    def _cancel_timer_locked(self) -> None:
        if self._timer_task is not None and not self._timer_task.done():
            self._timer_task.cancel()
        self._timer_task = None


class JobStore:
    def __init__(
        self,
        db_path: str | None = None,
        *,
        database: PostgresDatabase | None = None,
        persist_intermediate: bool = True,
    ):
        self.db_path = db_path or ""
        self.database = database
        self.persist_intermediate = persist_intermediate
        self._jobs: dict[str, ProcessingJob] = {}
        self._lock = asyncio.Lock()
        if self.database is not None:
            self._create_tables()
        elif self.db_path:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._create_tables()

    def _connect(self):
        if self.database is not None:
            return self.database.connect()
        return sqlite_connection(self.db_path)

    def _create_tables(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processing_job (
                    job_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    client_resource_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    manifest_fingerprint TEXT NOT NULL DEFAULT '',
                    batch_id TEXT NOT NULL DEFAULT '',
                    search_resource_id TEXT NOT NULL DEFAULT '',
                    steps_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            if self.database is None:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(processing_job)").fetchall()}
                if "manifest_fingerprint" not in columns:
                    conn.execute(
                        "ALTER TABLE processing_job ADD COLUMN manifest_fingerprint TEXT NOT NULL DEFAULT ''"
                    )
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_processing_job_client_resource
                ON processing_job(client_id, client_resource_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_processing_job_state
                ON processing_job(state)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_processing_job_state_created_job
                ON processing_job(state, created_at, job_id)
            """)
            if self.database is not None:
                conn.execute(
                    "ALTER TABLE processing_job ADD COLUMN IF NOT EXISTS manifest_fingerprint TEXT NOT NULL DEFAULT ''"
                )
                conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_processing_job_manifest
                    ON processing_job(client_id, client_resource_id, manifest_fingerprint)
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS processing_schema_migration (
                        migration_key TEXT PRIMARY KEY,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            conn.commit()

    def _now(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _job_from_row(self, row: Mapping[str, Any] | None) -> ProcessingJob | None:
        if row is None:
            return None
        steps = [
            JobStep.model_validate(item)
            for item in json.loads(row["steps_json"] or "[]")
            if isinstance(item, dict)
        ]
        return ProcessingJob(
            job_id=row["job_id"],
            client_id=row["client_id"],
            client_resource_id=row["client_resource_id"],
            state=JobState(row["state"]),
            manifest=ResourceManifest.model_validate(json.loads(row["manifest_json"] or "{}")),
            batch_id=row["batch_id"] or "",
            search_resource_id=row["search_resource_id"] or "",
            steps=steps,
            error=row["error"],
        )

    @staticmethod
    def _active_state_values() -> list[str]:
        return [
            JobState.QUEUED.value,
            JobState.VALIDATING.value,
            JobState.PREVIEWING.value,
            JobState.DESCRIBING.value,
            JobState.SUBMITTING.value,
        ]

    @staticmethod
    def _terminal_state_values() -> set[str]:
        return {
            JobState.COMPLETED.value,
            JobState.FAILED.value,
            JobState.CANCELLED.value,
        }

    def _is_terminal_change(self, changes: dict[str, Any]) -> bool:
        state = changes.get("state")
        if isinstance(state, JobState):
            state = state.value
        return str(state or "") in self._terminal_state_values()

    async def _remember_job(self, job: ProcessingJob) -> None:
        async with self._lock:
            self._jobs[job.job_id] = job

    async def _forget_job(self, job_id: str) -> None:
        async with self._lock:
            self._jobs.pop(job_id, None)

    async def _memory_job(self, job_id: str) -> ProcessingJob | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def _update_memory_job(self, job_id: str, **changes) -> ProcessingJob | None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            data = job.model_dump()
            data.update(changes)
            updated = ProcessingJob.model_validate(data)
            self._jobs[job_id] = updated
            return updated

    async def _append_memory_step(self, job_id: str, step: JobStep) -> bool:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            data = job.model_dump()
            data["steps"] = [*job.steps, step]
            self._jobs[job_id] = ProcessingJob.model_validate(data)
            return True

    def mark_interrupted_failed(self, error: str) -> int:
        if not self.db_path:
            return 0
        active_states = self._active_state_values()
        with self._connect() as conn:
            cursor = conn.execute(
                f"""UPDATE processing_job
                    SET state = ?,
                        error = ?,
                        updated_at = ?
                    WHERE state IN ({",".join("?" for _ in active_states)})""",
                (JobState.FAILED.value, error[:2000], self._now(), *active_states),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def requeue_interrupted(self) -> int:
        if not self.db_path and self.database is None:
            return 0
        interrupted_states = [
            JobState.VALIDATING.value,
            JobState.PREVIEWING.value,
            JobState.DESCRIBING.value,
            JobState.SUBMITTING.value,
        ]
        with self._connect() as conn:
            cursor = conn.execute(
                f"""UPDATE processing_job
                    SET state = ?,
                        search_resource_id = '',
                        steps_json = '[]',
                        error = NULL,
                        updated_at = ?
                    WHERE state IN ({','.join('?' for _ in interrupted_states)})""",
                (JobState.QUEUED.value, self._now(), *interrupted_states),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    async def create(self, *, client_id: str, manifest: ResourceManifest, batch_id: str = "") -> ProcessingJob:
        manifest_fingerprint = _manifest_fingerprint(manifest)
        job = ProcessingJob(
            job_id=f"job_{uuid.uuid4().hex[:16]}",
            client_id=client_id,
            client_resource_id=manifest.client_resource_id,
            state=JobState.QUEUED,
            manifest=manifest,
            batch_id=batch_id,
        )
        if self.database is not None:
            now = self._now()
            with self._connect() as conn:
                row = conn.execute(
                    """INSERT INTO processing_job
                           (job_id, client_id, client_resource_id, state, manifest_json,
                            manifest_fingerprint, batch_id, search_resource_id, steps_json,
                            error, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, '', '[]', NULL, ?, ?)
                       ON CONFLICT (client_id, client_resource_id, manifest_fingerprint)
                       DO UPDATE SET
                           manifest_json = excluded.manifest_json,
                           batch_id = excluded.batch_id,
                           state = CASE
                               WHEN processing_job.state IN ('failed', 'cancelled') THEN 'queued'
                               ELSE processing_job.state
                           END,
                           search_resource_id = CASE
                               WHEN processing_job.state IN ('failed', 'cancelled') THEN ''
                               ELSE processing_job.search_resource_id
                           END,
                           steps_json = CASE
                               WHEN processing_job.state IN ('failed', 'cancelled') THEN '[]'
                               ELSE processing_job.steps_json
                           END,
                           error = CASE
                               WHEN processing_job.state IN ('failed', 'cancelled') THEN NULL
                               ELSE processing_job.error
                           END,
                           updated_at = CASE
                               WHEN processing_job.state IN ('failed', 'cancelled') THEN excluded.updated_at
                               ELSE processing_job.updated_at
                           END
                       RETURNING *""",
                    (
                        job.job_id,
                        job.client_id,
                        job.client_resource_id,
                        job.state.value,
                        manifest.model_dump_json(),
                        manifest_fingerprint,
                        batch_id,
                        now,
                        now,
                    ),
                ).fetchone()
                conn.commit()
            created = self._job_from_row(row)
            if created is None:
                raise RuntimeError("processing job insert returned no row")
            return created
        if self.db_path or self.database is not None:
            now = self._now()
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO processing_job
                       (job_id, client_id, client_resource_id, state, manifest_json,
                        manifest_fingerprint, batch_id, search_resource_id, steps_json,
                        error, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, '', '[]', NULL, ?, ?)""",
                    (
                        job.job_id,
                        job.client_id,
                        job.client_resource_id,
                        job.state.value,
                        manifest.model_dump_json(),
                        manifest_fingerprint,
                        batch_id,
                        now,
                        now,
                    ),
                )
                conn.commit()
            if not self.persist_intermediate:
                await self._remember_job(job)
            return job
        async with self._lock:
            self._jobs[job.job_id] = job
        return job

    async def claim_next_queued(self) -> ProcessingJob | None:
        if self.database is not None:
            with self._connect() as conn:
                row = conn.execute(
                    """WITH candidate AS (
                           SELECT job_id
                           FROM processing_job
                           WHERE state = ?
                           ORDER BY created_at, job_id
                           FOR UPDATE SKIP LOCKED
                           LIMIT 1
                       )
                       UPDATE processing_job AS job
                       SET state = ?, updated_at = ?
                       FROM candidate
                       WHERE job.job_id = candidate.job_id
                       RETURNING job.*""",
                    (JobState.QUEUED.value, JobState.VALIDATING.value, self._now()),
                ).fetchone()
                conn.commit()
            return self._job_from_row(row)
        if self.db_path or self.database is not None:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM processing_job WHERE state = ? ORDER BY created_at, job_id LIMIT 1",
                    (JobState.QUEUED.value,),
                ).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE processing_job SET state = ?, updated_at = ? WHERE job_id = ? AND state = ?",
                    (JobState.VALIDATING.value, self._now(), row["job_id"], JobState.QUEUED.value),
                )
                conn.commit()
            return await self.get(str(row["job_id"]))
        return None

    async def get(self, job_id: str) -> ProcessingJob | None:
        memory_job = await self._memory_job(job_id)
        if memory_job is not None:
            return memory_job
        if self.db_path or self.database is not None:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM processing_job WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            return self._job_from_row(row)
        async with self._lock:
            return self._jobs.get(job_id)

    async def get_many(self, job_ids: list[str]) -> dict[str, ProcessingJob]:
        normalized = list(dict.fromkeys(str(job_id or "").strip() for job_id in job_ids if str(job_id or "").strip()))
        if not normalized:
            return {}
        if self.db_path or self.database is not None:
            placeholders = ",".join("?" for _ in normalized)
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT * FROM processing_job WHERE job_id IN ({placeholders})",
                    normalized,
                ).fetchall()
            jobs = [self._job_from_row(row) for row in rows]
            return {job.job_id: job for job in jobs if job is not None}
        async with self._lock:
            return {job_id: self._jobs[job_id] for job_id in normalized if job_id in self._jobs}

    async def update(self, job_id: str, **changes) -> ProcessingJob | None:
        if self.db_path or self.database is not None:
            allowed = {"state", "search_resource_id", "error"}
            normalized_changes: dict[str, Any] = {}
            updates: list[str] = []
            params: list[Any] = []
            for key, value in changes.items():
                if key not in allowed:
                    continue
                if key == "state" and isinstance(value, JobState):
                    value = value.value
                normalized_changes[key] = value
                updates.append(f"{key} = ?")
                params.append(value)
            if not updates:
                return await self.get(job_id)

            memory_job = await self._update_memory_job(job_id, **normalized_changes)
            should_persist = self.persist_intermediate or self._is_terminal_change(normalized_changes)
            if not should_persist:
                return memory_job or await self.get(job_id)

            updates.append("updated_at = ?")
            params.append(self._now())
            if memory_job is not None:
                updates.append("steps_json = ?")
                params.append(json.dumps([step.model_dump() for step in memory_job.steps], ensure_ascii=False))
            params.append(job_id)
            with self._connect() as conn:
                cursor = conn.execute(
                    f"UPDATE processing_job SET {', '.join(updates)} WHERE job_id = ?",
                    params,
                )
                conn.commit()
                if not cursor.rowcount:
                    return None
            if self._is_terminal_change(normalized_changes):
                await self._forget_job(job_id)
            return await self.get(job_id)
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            data = job.model_dump()
            data.update(changes)
            job = ProcessingJob.model_validate(data)
            self._jobs[job_id] = job
        return job

    async def append_step(self, job_id: str, step: JobStep) -> None:
        if self.db_path or self.database is not None:
            if not self.persist_intermediate and await self._append_memory_step(job_id, step):
                return
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT steps_json FROM processing_job WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                steps = json.loads(row["steps_json"] or "[]")
                steps.append(step.model_dump())
                conn.execute(
                    """UPDATE processing_job
                       SET steps_json = ?, updated_at = ?
                       WHERE job_id = ?""",
                    (json.dumps(steps, ensure_ascii=False), self._now(), job_id),
                )
                conn.commit()
            return
        async with self._lock:
            job = self._jobs[job_id]
            job.steps.append(step)

    async def cancel_active_for_resource(self, *, client_id: str, client_resource_id: str, error: str = "") -> int:
        active_states = self._active_state_values()
        if self.db_path or self.database is not None:
            now = self._now()
            with self._connect() as conn:
                cursor = conn.execute(
                    f"""UPDATE processing_job
                        SET state = ?,
                            error = ?,
                            updated_at = ?
                        WHERE client_id = ?
                          AND client_resource_id = ?
                          AND state IN ({",".join("?" for _ in active_states)})""",
                    (
                        JobState.CANCELLED.value,
                        error[:2000] if error else None,
                        now,
                        client_id,
                        client_resource_id,
                        *active_states,
                    ),
                )
                conn.commit()
                db_count = int(cursor.rowcount or 0)
            memory_count = 0
            async with self._lock:
                for job_id, job in list(self._jobs.items()):
                    if job.client_id != client_id or job.client_resource_id != client_resource_id:
                        continue
                    if job.state.value not in active_states:
                        continue
                    data = job.model_dump()
                    data.update({"state": JobState.CANCELLED, "error": error or None})
                    self._jobs[job_id] = ProcessingJob.model_validate(data)
                    memory_count += 1
            return max(db_count, memory_count)

        count = 0
        async with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.client_id != client_id or job.client_resource_id != client_resource_id:
                    continue
                if job.state.value not in active_states:
                    continue
                data = job.model_dump()
                data.update({"state": JobState.CANCELLED, "error": error or None})
                self._jobs[job_id] = ProcessingJob.model_validate(data)
                count += 1
        return count


class ProcessingService:
    def __init__(
        self,
        *,
        storage: ObjectStorage | None = None,
        search_client: SearchServerClient | None = None,
        snapshot_store: ProcessedSnapshotStore | None = None,
        store: JobStore | None = None,
        database: PostgresDatabase | None = None,
    ):
        ensure_resource_processor_imports()
        self.database = database
        if store is None and self.database is None and settings.database_url:
            self.database = PostgresDatabase(
                settings.database_url,
                min_size=settings.database_pool_min_size,
                max_size=settings.database_pool_max_size,
            )
        self.storage = storage or ObjectStorage()
        self.search_client = search_client or SearchServerClient()
        self.snapshot_store = snapshot_store or ProcessedSnapshotStore(
            settings.snapshot_db_path,
            database=self.database,
        )
        self.store = store or JobStore(
            settings.snapshot_db_path,
            database=self.database,
            persist_intermediate=self.database is not None,
        )
        if store is None and self.database is not None:
            if settings.migrate_legacy_sqlite:
                migrate_legacy_sqlite(self.database, settings.snapshot_db_path)
            self.store.requeue_interrupted()
        elif store is None:
            self.store.mark_interrupted_failed("server restarted before processing job completed")
        self.description_batcher = DescriptionBatcher()
        self.preview_renderer = PreviewRendererClient()
        self._resource_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._worker_tasks: list[asyncio.Task] = []
        self._worker_stop = asyncio.Event()

    async def start_workers(self) -> None:
        if settings.process_inline or self.database is None or self._worker_tasks:
            return
        self._worker_stop.clear()
        self._worker_tasks = [
            asyncio.create_task(self._worker_loop(index), name=f"processing-job-worker-{index + 1}")
            for index in range(max(1, int(settings.job_worker_concurrency)))
        ]

    async def stop_workers(self) -> None:
        self._worker_stop.set()
        tasks, self._worker_tasks = self._worker_tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        close = getattr(self.search_client, "close", None)
        if close is not None:
            await close()
        if self.database is not None:
            self.database.close()

    async def _worker_loop(self, worker_index: int) -> None:
        while not self._worker_stop.is_set():
            try:
                job = await self.store.claim_next_queued()
                if job is None:
                    await asyncio.sleep(max(0.05, float(settings.job_worker_idle_seconds)))
                    continue
                await self.run_job(job.job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Processing worker %s crashed outside a job boundary", worker_index + 1)
                # Keep the durable queue alive after a per-job or transient DB error.
                await asyncio.sleep(max(0.1, float(settings.job_worker_idle_seconds)))

    def _resource_lock(self, *, client_id: str, client_resource_id: str) -> asyncio.Lock:
        key = (client_id, client_resource_id)
        lock = self._resource_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._resource_locks[key] = lock
        return lock

    async def create_job(self, *, client_id: str, manifest: ResourceManifest) -> CreateJobOut:
        _ensure_indexable_manifest(manifest)
        self.snapshot_store.clear_delete_marker(client_id=client_id, client_resource_id=manifest.client_resource_id)
        job = await self.store.create(client_id=client_id, manifest=manifest)
        return CreateJobOut(
            job_id=job.job_id,
            state=job.state,
            resource_fingerprint=_fingerprint(manifest),
        )

    async def create_batch(self, *, client_id: str, manifests: list[ResourceManifest]) -> CreateBatchOut:
        for manifest in manifests:
            _ensure_indexable_manifest(manifest)
        batch_id = f"batch_{uuid.uuid4().hex[:16]}"
        jobs: list[BatchJobOut] = []
        for manifest in manifests:
            self.snapshot_store.clear_delete_marker(client_id=client_id, client_resource_id=manifest.client_resource_id)
            job = await self.store.create(client_id=client_id, manifest=manifest, batch_id=batch_id)
            jobs.append(BatchJobOut(
                job_id=job.job_id,
                client_resource_id=manifest.client_resource_id,
                state=job.state,
                resource_fingerprint=_fingerprint(manifest),
            ))
        return CreateBatchOut(batch_id=batch_id, jobs=jobs)

    async def get_job(self, job_id: str, *, client_id: str) -> JobOut | None:
        job = await self.store.get(job_id)
        if job is None or job.client_id != client_id:
            return None
        return JobOut(
            job_id=job.job_id,
            state=job.state,
            client_resource_id=job.client_resource_id,
            search_resource_id=job.search_resource_id,
            steps=job.steps,
            error=job.error,
        )

    async def get_job_statuses(self, job_ids: list[str], *, client_id: str) -> tuple[list[JobStatusOut], list[str]]:
        jobs_by_id = await self.store.get_many(job_ids)
        statuses: list[JobStatusOut] = []
        missing: list[str] = []
        for job_id in job_ids:
            job = jobs_by_id.get(job_id)
            if job is None or job.client_id != client_id:
                missing.append(job_id)
                continue
            statuses.append(JobStatusOut(
                job_id=job.job_id,
                state=job.state,
                client_resource_id=job.client_resource_id,
                search_resource_id=job.search_resource_id,
                error=job.error,
            ))
        return statuses, missing

    async def cancel_job(self, job_id: str, *, client_id: str) -> JobOut | None:
        job = await self.store.get(job_id)
        if job is None or job.client_id != client_id:
            return None
        updated = await self.store.update(job_id, state=JobState.CANCELLED)
        if updated is None:
            return None
        return await self.get_job(updated.job_id, client_id=client_id)

    async def retry_job(self, job_id: str, *, client_id: str) -> CreateJobOut | None:
        job = await self.store.get(job_id)
        if job is None or job.client_id != client_id:
            return None
        self.snapshot_store.clear_delete_marker(client_id=job.client_id, client_resource_id=job.client_resource_id)
        retry = await self.store.create(client_id=job.client_id, manifest=job.manifest)
        return CreateJobOut(job_id=retry.job_id, state=retry.state, resource_fingerprint=_fingerprint(retry.manifest))

    async def replay_snapshot(self, *, client_id: str, client_resource_id: str) -> ReplaySnapshotOut | None:
        row = self.snapshot_store.get(client_id=client_id, client_resource_id=client_resource_id)
        if row is None:
            return None
        payload = row["snapshot"]
        try:
            result = await self.search_client.upsert_resource(payload)
        except Exception as exc:
            self.snapshot_store.mark_upsert_failed(
                client_id=client_id,
                client_resource_id=client_resource_id,
                error=str(exc),
            )
            return ReplaySnapshotOut(
                client_id=client_id,
                client_resource_id=client_resource_id,
                state="upsert_failed",
                error_message=str(exc),
            )
        resource_id = str(result.get("resource_id") or "")
        self.snapshot_store.mark_upserted(
            client_id=client_id,
            client_resource_id=client_resource_id,
            search_resource_id=resource_id,
        )
        return ReplaySnapshotOut(
            client_id=client_id,
            client_resource_id=client_resource_id,
            state="upserted",
            search_resource_id=resource_id,
        )

    async def replay_snapshots(
        self,
        *,
        client_id: str,
        search_upsert_state: str = "",
        limit: int | None = None,
    ) -> list[ReplaySnapshotOut]:
        results: list[ReplaySnapshotOut] = []
        for row in self.snapshot_store.iter_snapshots(
            client_id=client_id,
            search_upsert_state=search_upsert_state,
            limit=limit,
        ):
            result = await self.replay_snapshot(
                client_id=row["client_id"],
                client_resource_id=row["client_resource_id"],
            )
            if result is not None:
                results.append(result)
        return results

    async def delete_processed_resource(
        self,
        *,
        client_id: str,
        request: DeleteProcessedResourceIn,
    ) -> DeleteProcessedResourceOut:
        client_resource_id = str(request.client_resource_id or "").strip()
        resource_id = str(request.resource_id or "").strip()
        if not client_resource_id and not resource_id:
            return DeleteProcessedResourceOut(
                client_resource_id=client_resource_id,
                search_resource_id=resource_id,
                state="failed",
                error_message="client_resource_id or resource_id is required",
            )
        if resource_id and not client_resource_id and not settings.allow_resource_id_delete:
            return DeleteProcessedResourceOut(
                client_resource_id=client_resource_id,
                search_resource_id=resource_id,
                state="failed",
                error_message="resource_id-only delete is disabled; use client_resource_id or enable admin delete",
            )

        idempotency_key = request.idempotency_key or f"{client_id}:{client_resource_id or resource_id}:delete"
        if client_resource_id:
            async with self._resource_lock(client_id=client_id, client_resource_id=client_resource_id):
                return await self._delete_processed_resource_locked(
                    client_id=client_id,
                    client_resource_id=client_resource_id,
                    resource_id=resource_id,
                    idempotency_key=idempotency_key,
                    request=request,
                )
        return await self._delete_processed_resource_locked(
            client_id=client_id,
            client_resource_id=client_resource_id,
            resource_id=resource_id,
            idempotency_key=idempotency_key,
            request=request,
        )

    async def _delete_processed_resource_locked(
        self,
        *,
        client_id: str,
        client_resource_id: str,
        resource_id: str,
        idempotency_key: str,
        request: DeleteProcessedResourceIn,
    ) -> DeleteProcessedResourceOut:
        if client_resource_id:
            self.snapshot_store.mark_deleted(
                client_id=client_id,
                client_resource_id=client_resource_id,
                resource_id=resource_id,
                idempotency_key=idempotency_key,
                reason=request.reason,
            )
            await self.store.cancel_active_for_resource(
                client_id=client_id,
                client_resource_id=client_resource_id,
                error="resource delete requested",
            )

        snapshot_refs: list[dict[str, str]] = []
        if client_resource_id:
            row = self.snapshot_store.get(client_id=client_id, client_resource_id=client_resource_id)
            if row is not None:
                snapshot_refs = _object_refs_from_snapshot(row["snapshot"])

        search_payload = {
            "idempotency_key": idempotency_key,
            "client_id": client_id,
            "client_resource_id": client_resource_id,
            "resource_id": resource_id,
            "mode": "hard",
            "delete_objects": False,
            "reason": request.reason,
        }
        try:
            search_result = await self.search_client.delete_resource(search_payload)
        except Exception as exc:
            return DeleteProcessedResourceOut(
                client_resource_id=client_resource_id,
                search_resource_id=resource_id,
                state="search_delete_failed",
                object_refs=[DeletedObjectRef(**ref) for ref in _dedupe_object_refs(snapshot_refs)],
                error_message=str(exc),
            )

        search_refs = search_result.get("object_refs") or []
        object_refs = _dedupe_object_refs([*snapshot_refs, *search_refs])
        search_state = str(search_result.get("state") or "")
        search_deleted = bool(search_result.get("deleted", False))
        search_resource_id = str(search_result.get("resource_id") or resource_id)

        deletable_refs = _generated_preview_refs(object_refs)
        objects_deleted = 0
        if request.delete_objects and deletable_refs:
            try:
                objects_deleted = self.storage.delete_refs(deletable_refs)
            except Exception as exc:
                return DeleteProcessedResourceOut(
                    client_resource_id=client_resource_id,
                    search_resource_id=search_resource_id,
                    state="object_delete_failed",
                    search_deleted=search_deleted,
                    objects_deleted=objects_deleted,
                    snapshot_deleted=False,
                    object_refs=[DeletedObjectRef(**ref) for ref in object_refs],
                    error_message=str(exc),
                )

        snapshot_deleted = False
        if client_resource_id:
            snapshot_deleted = self.snapshot_store.delete(
                client_id=client_id,
                client_resource_id=client_resource_id,
            ) > 0

        if search_state == "not_found" and not snapshot_deleted and not object_refs:
            state = "not_found"
        else:
            state = "deleted"

        return DeleteProcessedResourceOut(
            client_resource_id=client_resource_id,
            search_resource_id=search_resource_id,
            state=state,
            search_deleted=search_deleted,
            objects_deleted=objects_deleted,
            snapshot_deleted=snapshot_deleted,
            object_refs=[DeletedObjectRef(**ref) for ref in object_refs],
        )

    async def run_job(self, job_id: str) -> None:
        job = await self.store.get(job_id)
        if job is None or job.state in {JobState.CANCELLED, JobState.COMPLETED, JobState.FAILED}:
            return
        try:
            await self._abort_if_deleted_or_cancelled(job_id, job.client_id, job.client_resource_id)
            await self._process_one(job_id, job.client_id, _as_child(job.manifest))
            latest = await self.store.get(job_id)
            await self.store.update(job_id, state=JobState.COMPLETED, search_resource_id=latest.search_resource_id if latest else "")
        except JobCancelledError as exc:
            await self.store.update(job_id, state=JobState.CANCELLED, error=str(exc))
        except Exception as exc:
            error = _error_text(exc)
            logger.exception("Processing job %s failed: %s", job_id, error)
            await self.store.update(job_id, state=JobState.FAILED, error=error)

    async def _abort_if_deleted_or_cancelled(
        self,
        job_id: str,
        client_id: str,
        client_resource_id: str,
        preview_refs: list[PreviewRef] | None = None,
    ) -> None:
        job = await self.store.get(job_id)
        if job is None:
            raise JobCancelledError("job no longer exists")
        if job.state == JobState.CANCELLED:
            await self._cleanup_cancelled_previews(job_id, preview_refs or [])
            raise JobCancelledError(job.error or "job cancelled")
        is_deleted = await asyncio.to_thread(
            self.snapshot_store.is_deleted,
            client_id=client_id,
            client_resource_id=client_resource_id,
        )
        if is_deleted:
            await self._cleanup_cancelled_previews(job_id, preview_refs or [])
            await self.store.update(job_id, state=JobState.CANCELLED, error="resource delete requested")
            raise JobCancelledError("resource delete requested")

    async def _cleanup_cancelled_previews(self, job_id: str, preview_refs: list[PreviewRef]) -> None:
        deletable_refs = _generated_preview_refs(_preview_refs_for_delete(preview_refs))
        if not deletable_refs:
            return
        start = time.perf_counter()
        try:
            deleted = await asyncio.to_thread(self.storage.delete_refs, deletable_refs)
            await self.store.append_step(
                job_id,
                _step("cleanup_cancelled_previews", "completed", start, error=f"deleted={deleted}"),
            )
        except Exception as exc:
            await self.store.append_step(
                job_id,
                _step("cleanup_cancelled_previews", "failed", start, error=str(exc)),
            )

    async def _process_one(
        self,
        job_id: str,
        client_id: str,
        manifest: ChildResourceManifest,
    ) -> str:
        job_root = safe_join_under(settings.work_dir, job_id, fallback="job")
        work_root = safe_join_under(job_root, manifest.client_resource_id, fallback="resource")
        source_dir = work_root / "source"
        preview_dir = work_root / "previews"

        try:
            await self._abort_if_deleted_or_cancelled(job_id, client_id, manifest.client_resource_id)
            await self.store.update(job_id, state=JobState.VALIDATING)
            start = time.perf_counter()
            local_sources: list[Path] = []
            local_source_object: Path | None = None
            source_object_name = safe_file_name(
                manifest.source_object.file_name or Path(manifest.source_object.object_key).name,
                "source",
            )
            if manifest.file_structure is None:
                local_source_object = await asyncio.to_thread(
                    self.storage.download_ref, manifest.source_object, source_dir, source_object_name,
                )
                structure = await asyncio.to_thread(
                    scan_source_file_structure,
                    local_source_object,
                    checksum=manifest.source_object.checksum,
                )
                manifest.file_structure = FileStructure.model_validate(structure)
                await self.store.append_step(job_id, _step("file_structure_generated", "completed", start))
            else:
                await self.store.append_step(job_id, _step("file_structure_provided", "completed", start))
            if not manifest.previews:
                if local_source_object is None:
                    local_source_object = await asyncio.to_thread(
                        self.storage.download_ref, manifest.source_object, source_dir, source_object_name,
                    )
                local_sources = await asyncio.to_thread(
                    resolve_local_source_files,
                    local_source_object,
                    manifest.file_structure.entries,
                    source_dir,
                )
                await self.store.append_step(job_id, _step("source_content_extracted", "completed", start))
            else:
                await self.store.append_step(job_id, _step("source_content_skipped", "completed", start))
            await self._abort_if_deleted_or_cancelled(job_id, client_id, manifest.client_resource_id)

            entity = build_processing_entity(
                client_id=client_id,
                client_resource_id=manifest.client_resource_id,
                resource_type=manifest.resource_type,
                file_entries=manifest.file_structure.entries,
                local_source_paths=local_sources,
                description_context=manifest.description_context,
            )

            await self.store.update(job_id, state=JobState.PREVIEWING)
            start = time.perf_counter()
            preview_refs, local_preview_paths = await self._resolve_previews(
                job_id=job_id,
                client_id=client_id,
                manifest=manifest,
                preview_dir=preview_dir,
                entity=entity,
            )
            await self.store.append_step(job_id, _step("preview", "completed", start))
            await self._abort_if_deleted_or_cancelled(
                job_id,
                client_id,
                manifest.client_resource_id,
                preview_refs,
            )

            await self.store.update(job_id, state=JobState.DESCRIBING)
            start = time.perf_counter()
            if manifest.description is not None:
                description = description_result_from_provided(manifest.description)
                description_step_name = "description_provided"
            else:
                description = await self.description_batcher.describe(
                    entity=entity,
                    preview_paths=local_preview_paths,
                    description_context=manifest.description_context,
                )
                description_step_name = "description"
            await self.store.append_step(job_id, _step(description_step_name, "completed", start))
            await self._abort_if_deleted_or_cancelled(
                job_id,
                client_id,
                manifest.client_resource_id,
                preview_refs,
            )

            await self.store.update(job_id, state=JobState.SUBMITTING)
            start = time.perf_counter()
            async with self._resource_lock(client_id=client_id, client_resource_id=manifest.client_resource_id):
                await self._abort_if_deleted_or_cancelled(
                    job_id,
                    client_id=client_id,
                    client_resource_id=manifest.client_resource_id,
                    preview_refs=preview_refs,
                )
                payload = self._build_upsert_payload(
                    client_id=client_id,
                    manifest=manifest,
                    preview_refs=preview_refs,
                    description=description,
                )
                submit_marks: dict[str, int] = {}

                def mark_submit(name: str) -> None:
                    submit_marks[name] = int((time.perf_counter() - start) * 1000)

                old_snapshot = await asyncio.to_thread(
                    self.snapshot_store.get,
                    client_id=client_id,
                    client_resource_id=manifest.client_resource_id,
                )
                mark_submit("snapshot_get")
                old_refs = _object_refs_from_snapshot(old_snapshot["snapshot"]) if old_snapshot is not None else []
                await asyncio.to_thread(
                    self.snapshot_store.save_pending,
                    payload,
                    resource_fingerprint=_fingerprint_from_child(manifest),
                )
                mark_submit("snapshot_save_pending")
                await self._abort_if_deleted_or_cancelled(
                    job_id,
                    client_id=client_id,
                    client_resource_id=manifest.client_resource_id,
                    preview_refs=preview_refs,
                )
                try:
                    result = await self.search_client.upsert_resource(payload)
                    mark_submit("search_http")
                except Exception as exc:
                    await asyncio.to_thread(
                        self.snapshot_store.mark_upsert_failed,
                        client_id=client_id,
                        client_resource_id=manifest.client_resource_id,
                        error=str(exc),
                    )
                    raise
                await self.store.append_step(job_id, _step("search_upsert", "completed", start))
                resource_id = str(result.get("resource_id") or "")
                await asyncio.to_thread(
                    self.snapshot_store.mark_upserted,
                    client_id=client_id,
                    client_resource_id=manifest.client_resource_id,
                    search_resource_id=resource_id,
                )
                mark_submit("snapshot_mark_upserted")
                await self._cleanup_replaced_objects(job_id, old_refs, _object_refs_from_snapshot(payload))
                mark_submit("cleanup_replaced_objects")
                if submit_marks.get("cleanup_replaced_objects", 0) >= 1000:
                    await self.store.append_step(
                        job_id,
                        JobStep(
                            name="search_upsert_breakdown",
                            state="completed",
                            duration_ms=submit_marks["cleanup_replaced_objects"],
                            error=json.dumps(submit_marks, separators=(",", ":")),
                        ),
                    )
                await self.store.update(job_id, search_resource_id=resource_id)
            return resource_id
        finally:
            if not settings.keep_work_dir:
                await asyncio.to_thread(shutil.rmtree, job_root, ignore_errors=True)

    async def _cleanup_replaced_objects(
        self,
        job_id: str,
        old_refs: list[dict[str, str]],
        new_refs: list[dict[str, str]],
    ) -> None:
        old_deduped = _dedupe_object_refs(old_refs)
        if not old_deduped:
            return
        new_keys = {
            (ref["storage_profile_id"], ref["object_key"])
            for ref in _dedupe_object_refs(new_refs)
        }
        stale_refs = [
            ref for ref in old_deduped
            if (ref["storage_profile_id"], ref["object_key"]) not in new_keys
        ]
        stale_refs = _generated_preview_refs(stale_refs)
        if not stale_refs:
            return

        start = time.perf_counter()
        try:
            deleted = await asyncio.to_thread(self.storage.delete_refs, stale_refs)
            await self.store.append_step(
                job_id,
                _step("cleanup_replaced_objects", "completed", start, error=f"deleted={deleted}"),
            )
        except Exception as exc:
            await self.store.append_step(
                job_id,
                _step("cleanup_replaced_objects", "failed", start, error=str(exc)),
            )

    async def _resolve_previews(
        self,
        *,
        job_id: str,
        client_id: str,
        manifest: ChildResourceManifest,
        preview_dir: Path,
        entity,
    ) -> tuple[list[PreviewRef], list[str]]:
        preview_refs: list[PreviewRef] = []
        local_preview_paths: list[str] = []
        entity_with_sources = None

        def ensure_entity_with_sources() -> Any:
            nonlocal entity_with_sources
            if entity_with_sources is not None:
                return entity_with_sources
            if getattr(entity, "files", None) and getattr(entity.files[0], "file_path", ""):
                entity_with_sources = entity
                return entity_with_sources
            entity_with_sources = self._entity_with_sources(
                client_id=client_id,
                manifest=manifest,
                source_dir=preview_dir.parent / "source",
            )
            return entity_with_sources

        if manifest.previews:
            for index, ref in enumerate(manifest.previews):
                filename = safe_file_name(Path(ref.object_key).name, f"preview_{index}.webp")
                local = await asyncio.to_thread(
                    self._download_and_validate_preview,
                    ref,
                    preview_dir / "provided",
                    filename,
                    ensure_entity_with_sources,
                )
                if local is not None:
                    preview_refs.append(ref.model_copy(update={"origin": "provided", "renderer": ""}))
                    local_preview_paths.append(str(local))

            if not preview_refs:
                await self.store.append_step(
                    job_id,
                    JobStep(
                        name="provided_preview_fallback",
                        state="completed",
                        error="provided previews were supplied but none are valid",
                    ),
                )
            else:
                return preview_refs, local_preview_paths

        if self.preview_renderer.enabled:
            source_object_url = await asyncio.to_thread(
                self.storage.generate_read_url,
                manifest.source_object,
                expires=settings.preview_renderer_url_expires,
            )
            rendered_files = await self.preview_renderer.render_previews(
                client_id=client_id,
                manifest=manifest,
                source_object_url=source_object_url,
                output_dir=preview_dir / "rendered",
            )
            for index, rendered in enumerate(rendered_files):
                is_valid = await asyncio.to_thread(
                    self._validate_local_preview_for_entity,
                    str(rendered.path),
                    ensure_entity_with_sources,
                )
                if not is_valid:
                    raise RuntimeError(f"preview renderer returned invalid preview: {rendered.file_name}")
                uploaded = await asyncio.to_thread(
                    self.storage.upload_preview,
                    str(rendered.path),
                    client_id=client_id,
                    client_resource_id=manifest.client_resource_id,
                    preview_name=rendered.file_name or f"preview_{index}.webp",
                    role=rendered.role or "primary",
                )
                preview_refs.append(PreviewRef(
                    role=rendered.role or uploaded.role or "primary",
                    storage_profile_id=uploaded.storage_profile_id,
                    object_key=uploaded.object_key,
                    width=rendered.width,
                    height=rendered.height,
                    size=rendered.size or uploaded.size,
                    checksum=rendered.checksum or uploaded.checksum,
                    strategy=rendered.strategy or "static",
                    origin="generated",
                    renderer=rendered.renderer or "preview-renderer",
                ))
                local_preview_paths.append(str(rendered.path))
            if not preview_refs:
                raise RuntimeError("preview renderer produced no valid previews")
            return preview_refs, local_preview_paths

        entity = ensure_entity_with_sources()
        generated_infos = await generate_previews(entity, preview_dir / "generated")
        primary_used = False
        gallery_index = 1
        for info in generated_infos:
            path = getattr(info, "path", "") or ""
            if not path:
                continue
            role = getattr(info, "role", "") or "primary"
            use_primary = role == "primary" and not primary_used
            name = preview_object_name(path, use_primary=use_primary, gallery_index=gallery_index)
            if use_primary:
                primary_used = True
            else:
                gallery_index += 1
            uploaded = await asyncio.to_thread(
                self.storage.upload_preview,
                path,
                client_id=client_id,
                client_resource_id=manifest.client_resource_id,
                preview_name=name,
                role="primary" if use_primary else "gallery",
            )
            preview_refs.append(preview_ref_from_info(info, uploaded))
            local_preview_paths.append(path)
        if not preview_refs:
            raise RuntimeError("preview generation produced no valid previews")
        return preview_refs, local_preview_paths

    def _download_source_files(
        self,
        manifest: ChildResourceManifest,
        source_dir: Path,
        source_object_name: str,
    ) -> list[Path]:
        local_source_object = self.storage.download_ref(
            manifest.source_object,
            source_dir,
            source_object_name,
        )
        entries = manifest.file_structure.entries if manifest.file_structure else []
        return resolve_local_source_files(local_source_object, entries, source_dir)

    def _download_and_validate_preview(
        self,
        ref: PreviewRef,
        target_dir: Path,
        filename: str,
        entity_factory,
    ) -> Path | None:
        local = self.storage.download_ref(ref, target_dir, filename)
        if self._validate_local_preview_for_entity(str(local), entity_factory):
            return local
        return None

    def _validate_local_preview(self, path: str) -> bool:
        ensure_resource_processor_imports()
        from ResourceProcessor.preview.thumbnail_generator import validate_preview

        ok, _reason = validate_preview(path)
        return bool(ok)

    def _validate_local_preview_for_entity(self, path: str, entity_factory) -> bool:
        if self._validate_local_preview(path):
            return True
        entity = entity_factory()
        source_path = str(getattr(entity.files[0], "file_path", "") or "") if getattr(entity, "files", None) else ""
        if not source_path:
            return False
        from ResourceProcessor.preview.crawler_thumbnail_policy import _solid_source_matches_preview_failure
        from ResourceProcessor.preview.thumbnail_generator import validate_preview

        ok, reason = validate_preview(path)
        if ok:
            return True
        if _solid_source_matches_preview_failure(source_path, reason):
            ok, _reason = validate_preview(path, allow_solid_color=True)
            return bool(ok)
        return False

    def _entity_with_sources(
        self,
        *,
        client_id: str,
        manifest: ChildResourceManifest,
        source_dir: Path,
    ):
        source_object_name = safe_file_name(
            manifest.source_object.file_name or Path(manifest.source_object.object_key).name,
            "source",
        )
        local_source_object = self.storage.download_ref(manifest.source_object, source_dir, source_object_name)
        entries = manifest.file_structure.entries if manifest.file_structure else []
        local_sources = resolve_local_source_files(local_source_object, entries, source_dir)
        return build_processing_entity(
            client_id=client_id,
            client_resource_id=manifest.client_resource_id,
            resource_type=manifest.resource_type,
            file_entries=entries,
            local_source_paths=local_sources,
            description_context=manifest.description_context,
        )

    def _build_upsert_payload(
        self,
        *,
        client_id: str,
        manifest: ChildResourceManifest,
        preview_refs: list[PreviewRef],
        description,
    ) -> dict[str, Any]:
        payload = {
            "idempotency_key": f"{client_id}:{manifest.client_resource_id}:{settings.pipeline_version}",
            "client_id": client_id,
            "client_resource_id": manifest.client_resource_id,
            "resource_type": manifest.resource_type,
            "title": _manifest_title(manifest),
            "source_object": _object_ref_payload(manifest.source_object),
            "file_structure": manifest.file_structure.model_dump() if manifest.file_structure else None,
            "package_object": _object_ref_payload(manifest.package_object) if manifest.package_object else None,
            "previews": [_preview_ref_payload(item) for item in preview_refs],
            "description": {
                "summary": description.main_content,
                "detail": description.detail_content,
                "full": description.full_description,
            },
            "classification": _classification_payload(manifest.classification, description),
            "processing": {
                "pipeline_version": settings.pipeline_version,
                "description_model": settings.llm_model or settings.llm_provider,
                "description_prompt_version": description.prompt_version,
                "preview_source": _preview_source(preview_refs),
                "description_source": "provided" if manifest.description else "generated",
            },
        }
        if manifest.client_metadata:
            payload["client_metadata"] = dict(manifest.client_metadata)
        return payload
