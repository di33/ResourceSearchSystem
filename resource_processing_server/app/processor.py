from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from resource_contracts.path_safety import safe_file_name, safe_join_under
from resource_processing_server.app.adapters import (
    build_processing_entity,
    description_result_from_provided,
    generate_description,
    generate_descriptions_batch,
    generate_previews,
    preview_ref_from_info,
)
from resource_processing_server.app.config import settings
from resource_processing_server.app.legacy import ensure_resource_processor_imports
from resource_processing_server.app.models import (
    ChildResourceManifest,
    BatchJobOut,
    CreateJobOut,
    CreateBatchOut,
    DeleteProcessedResourceIn,
    DeleteProcessedResourceOut,
    DeletedObjectRef,
    JobOut,
    JobState,
    JobStep,
    ProcessingJob,
    PreviewRef,
    ReplaySnapshotOut,
    ResourceManifest,
)
from resource_processing_server.app.preview_renderer_client import PreviewRendererClient
from resource_processing_server.app.search_client import SearchServerClient
from resource_processing_server.app.source_files import resolve_local_source_files
from resource_processing_server.app.snapshots import ProcessedSnapshotStore
from resource_processing_server.app.storage import ObjectStorage, preview_object_name


def _fingerprint(manifest: ResourceManifest) -> str:
    return _fingerprint_from_object(manifest.source_object, manifest.source_files, manifest.package_object)


def _fingerprint_from_child(manifest: ChildResourceManifest) -> str:
    return _fingerprint_from_object(manifest.source_object, manifest.source_files, manifest.package_object)


def _fingerprint_from_object(source_object, source_files, package_object=None) -> str:
    payload = [f"{source_object.storage_profile_id}/{source_object.object_key}:{source_object.checksum or source_object.etag or source_object.size}"]
    payload.extend(
        f"{item.path_in_package}:{item.checksum or item.file_size}"
        for item in source_files
    )
    if package_object is not None:
        payload.append(f"package:{package_object.storage_profile_id}/{package_object.object_key}")
    return hashlib.sha256("|".join(sorted(payload)).encode("utf-8")).hexdigest()


def _step(name: str, state: str, start: float, error: str = "") -> JobStep:
    return JobStep(name=name, state=state, duration_ms=int((time.perf_counter() - start) * 1000), error=error)


def _as_child(manifest: ResourceManifest | ChildResourceManifest) -> ChildResourceManifest:
    if isinstance(manifest, ChildResourceManifest):
        return manifest
    return ChildResourceManifest(
        client_resource_id=manifest.client_resource_id,
        resource_type=manifest.resource_type,
        source_object=manifest.source_object,
        source_files=manifest.source_files,
        package_object=manifest.package_object,
        provided_previews=manifest.provided_previews,
        provided_description=manifest.provided_description,
        client_metadata=manifest.client_metadata,
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
    for item in payload.get("source_files") or []:
        if isinstance(item, dict):
            _append_payload_object_ref(
                refs,
                storage_profile_id=str(item.get("storage_profile_id") or ""),
                object_key=str(item.get("object_key") or ""),
                kind="source_file",
                origin=str(item.get("origin") or ""),
                renderer=str(item.get("renderer") or ""),
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
    metadata = manifest.client_metadata
    metadata_title = ""
    if isinstance(metadata, dict):
        metadata_title = str(metadata.get("title") or "").strip()
    first_source = manifest.source_files[0] if manifest.source_files else None
    return _first_non_empty(
        metadata_title,
        getattr(first_source, "file_name", "") if first_source is not None else "",
        manifest.source_object.file_name,
        Path(manifest.source_object.object_key).name,
        manifest.client_resource_id,
    )


class JobCancelledError(RuntimeError):
    pass


class DescriptionBatcher:
    def __init__(self):
        self._pending: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._timer_task: asyncio.Task | None = None

    async def describe(self, *, entity, preview_paths: list[str], client_metadata: Any | None):
        if not settings.description_batch_enabled:
            return await generate_description(
                entity=entity,
                preview_paths=preview_paths,
                client_metadata=client_metadata,
            )

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        request = {
            "entity": entity,
            "preview_paths": preview_paths,
            "client_metadata": client_metadata,
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
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or ""
        self._jobs: dict[str, ProcessingJob] = {}
        self._lock = asyncio.Lock()
        if self.db_path:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._create_tables()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=300)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=300000")
        return conn

    def _create_tables(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processing_job (
                    job_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    client_resource_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    batch_id TEXT NOT NULL DEFAULT '',
                    search_resource_id TEXT NOT NULL DEFAULT '',
                    steps_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_processing_job_client_resource
                ON processing_job(client_id, client_resource_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_processing_job_state
                ON processing_job(state)
            """)
            conn.commit()

    def _now(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    def _job_from_row(self, row: sqlite3.Row | None) -> ProcessingJob | None:
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

    async def create(self, *, client_id: str, manifest: ResourceManifest, batch_id: str = "") -> ProcessingJob:
        job = ProcessingJob(
            job_id=f"job_{uuid.uuid4().hex[:16]}",
            client_id=client_id,
            client_resource_id=manifest.client_resource_id,
            state=JobState.QUEUED,
            manifest=manifest,
            batch_id=batch_id,
        )
        if self.db_path:
            now = self._now()
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO processing_job
                       (job_id, client_id, client_resource_id, state, manifest_json,
                        batch_id, search_resource_id, steps_json, error, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, '', '[]', NULL, ?, ?)""",
                    (
                        job.job_id,
                        job.client_id,
                        job.client_resource_id,
                        job.state.value,
                        manifest.model_dump_json(),
                        batch_id,
                        now,
                        now,
                    ),
                )
                conn.commit()
            return job
        async with self._lock:
            self._jobs[job.job_id] = job
        return job

    async def get(self, job_id: str) -> ProcessingJob | None:
        if self.db_path:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM processing_job WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            return self._job_from_row(row)
        async with self._lock:
            return self._jobs.get(job_id)

    async def update(self, job_id: str, **changes) -> ProcessingJob | None:
        if self.db_path:
            allowed = {"state", "search_resource_id", "error"}
            updates: list[str] = []
            params: list[Any] = []
            for key, value in changes.items():
                if key not in allowed:
                    continue
                if key == "state" and isinstance(value, JobState):
                    value = value.value
                updates.append(f"{key} = ?")
                params.append(value)
            if not updates:
                return await self.get(job_id)
            updates.append("updated_at = ?")
            params.append(self._now())
            params.append(job_id)
            with self._connect() as conn:
                cursor = conn.execute(
                    f"UPDATE processing_job SET {', '.join(updates)} WHERE job_id = ?",
                    params,
                )
                conn.commit()
                if not cursor.rowcount:
                    return None
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
        if self.db_path:
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
        if self.db_path:
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
                        self._now(),
                        client_id,
                        client_resource_id,
                        *active_states,
                    ),
                )
                conn.commit()
                return int(cursor.rowcount or 0)

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
    ):
        ensure_resource_processor_imports()
        self.storage = storage or ObjectStorage()
        self.search_client = search_client or SearchServerClient()
        self.snapshot_store = snapshot_store or ProcessedSnapshotStore()
        self.store = store or JobStore(settings.snapshot_db_path)
        if store is None:
            self.store.mark_interrupted_failed("server restarted before processing job completed")
        self.description_batcher = DescriptionBatcher()
        self.preview_renderer = PreviewRendererClient()
        self._resource_locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _resource_lock(self, *, client_id: str, client_resource_id: str) -> asyncio.Lock:
        key = (client_id, client_resource_id)
        lock = self._resource_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._resource_locks[key] = lock
        return lock

    async def create_job(self, *, client_id: str, manifest: ResourceManifest) -> CreateJobOut:
        self.snapshot_store.clear_delete_marker(client_id=client_id, client_resource_id=manifest.client_resource_id)
        job = await self.store.create(client_id=client_id, manifest=manifest)
        return CreateJobOut(
            job_id=job.job_id,
            state=job.state,
            resource_fingerprint=_fingerprint(manifest),
        )

    async def create_batch(self, *, client_id: str, manifests: list[ResourceManifest]) -> CreateBatchOut:
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
                client_id=client_id,
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
        if job is None or job.state == JobState.CANCELLED:
            return
        try:
            await self._abort_if_deleted_or_cancelled(job_id, job.client_id, job.client_resource_id)
            await self._process_one(job_id, job.client_id, _as_child(job.manifest))
            latest = await self.store.get(job_id)
            await self.store.update(job_id, state=JobState.COMPLETED, search_resource_id=latest.search_resource_id if latest else "")
        except JobCancelledError as exc:
            await self.store.update(job_id, state=JobState.CANCELLED, error=str(exc))
        except Exception as exc:
            await self.store.update(job_id, state=JobState.FAILED, error=str(exc))

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
        if self.snapshot_store.is_deleted(client_id=client_id, client_resource_id=client_resource_id):
            await self._cleanup_cancelled_previews(job_id, preview_refs or [])
            await self.store.update(job_id, state=JobState.CANCELLED, error="resource delete requested")
            raise JobCancelledError("resource delete requested")

    async def _cleanup_cancelled_previews(self, job_id: str, preview_refs: list[PreviewRef]) -> None:
        deletable_refs = _generated_preview_refs(_preview_refs_for_delete(preview_refs))
        if not deletable_refs:
            return
        start = time.perf_counter()
        try:
            deleted = self.storage.delete_refs(deletable_refs)
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
            source_object_name = safe_file_name(
                manifest.source_object.file_name or Path(manifest.source_object.object_key).name,
                "source",
            )
            local_source_object = self.storage.download_ref(manifest.source_object, source_dir, source_object_name)
            local_sources = resolve_local_source_files(local_source_object, manifest.source_files, source_dir)
            await self.store.append_step(job_id, _step("validate_objects", "completed", start))
            await self._abort_if_deleted_or_cancelled(job_id, client_id, manifest.client_resource_id)

            entity = build_processing_entity(
                client_id=client_id,
                client_resource_id=manifest.client_resource_id,
                resource_type=manifest.resource_type,
                source_files=manifest.source_files,
                local_source_paths=local_sources,
                client_metadata=manifest.client_metadata,
            )

            await self.store.update(job_id, state=JobState.PREVIEWING)
            start = time.perf_counter()
            preview_refs, local_preview_paths = await self._resolve_previews(
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
            if manifest.provided_description is not None:
                description = description_result_from_provided(manifest.provided_description)
                description_step_name = "description_provided"
            else:
                description = await self.description_batcher.describe(
                    entity=entity,
                    preview_paths=local_preview_paths,
                    client_metadata=manifest.client_metadata,
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
                old_snapshot = self.snapshot_store.get(client_id=client_id, client_resource_id=manifest.client_resource_id)
                old_refs = _object_refs_from_snapshot(old_snapshot["snapshot"]) if old_snapshot is not None else []
                self.snapshot_store.save_pending(payload, resource_fingerprint=_fingerprint_from_child(manifest))
                await self._abort_if_deleted_or_cancelled(
                    job_id,
                    client_id=client_id,
                    client_resource_id=manifest.client_resource_id,
                    preview_refs=preview_refs,
                )
                try:
                    result = await self.search_client.upsert_resource(payload)
                except Exception as exc:
                    self.snapshot_store.mark_upsert_failed(
                        client_id=client_id,
                        client_resource_id=manifest.client_resource_id,
                        error=str(exc),
                    )
                    raise
                await self.store.append_step(job_id, _step("search_upsert", "completed", start))
                resource_id = str(result.get("resource_id") or "")
                self.snapshot_store.mark_upserted(
                    client_id=client_id,
                    client_resource_id=manifest.client_resource_id,
                    search_resource_id=resource_id,
                )
                await self._cleanup_replaced_objects(job_id, old_refs, _object_refs_from_snapshot(payload))
                await self.store.update(job_id, search_resource_id=resource_id)
            return resource_id
        finally:
            if not settings.keep_work_dir:
                shutil.rmtree(job_root, ignore_errors=True)

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
            deleted = self.storage.delete_refs(stale_refs)
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
        client_id: str,
        manifest: ChildResourceManifest,
        preview_dir: Path,
        entity,
    ) -> tuple[list[PreviewRef], list[str]]:
        preview_refs: list[PreviewRef] = []
        local_preview_paths: list[str] = []

        if manifest.provided_previews:
            for index, ref in enumerate(manifest.provided_previews):
                filename = safe_file_name(Path(ref.object_key).name, f"preview_{index}.webp")
                local = self.storage.download_ref(ref, preview_dir / "provided", filename)
                if self._validate_local_preview(str(local)):
                    preview_refs.append(ref)
                    local_preview_paths.append(str(local))

            if not preview_refs:
                raise RuntimeError("provided previews were supplied but none are valid")
            return preview_refs, local_preview_paths

        if self.preview_renderer.enabled:
            source_object_url = self.storage.generate_read_url(
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
                if not self._validate_local_preview(str(rendered.path)):
                    raise RuntimeError(f"preview renderer returned invalid preview: {rendered.file_name}")
                uploaded = self.storage.upload_preview(
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
            uploaded = self.storage.upload_preview(
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

    def _validate_local_preview(self, path: str) -> bool:
        ensure_resource_processor_imports()
        from ResourceProcessor.preview.thumbnail_generator import validate_preview

        ok, _reason = validate_preview(path)
        return bool(ok)

    def _build_upsert_payload(
        self,
        *,
        client_id: str,
        manifest: ChildResourceManifest,
        preview_refs: list[PreviewRef],
        description,
    ) -> dict[str, Any]:
        return {
            "idempotency_key": f"{client_id}:{manifest.client_resource_id}:{settings.pipeline_version}",
            "client_id": client_id,
            "client_resource_id": manifest.client_resource_id,
            "resource_type": manifest.resource_type,
            "client_metadata": manifest.client_metadata,
            "title": _manifest_title(manifest),
            "source_object": manifest.source_object.model_dump(),
            "source_files": [item.model_dump() for item in manifest.source_files],
            "package_object": manifest.package_object.model_dump() if manifest.package_object else None,
            "previews": [item.model_dump() for item in preview_refs],
            "description": {
                "summary": description.main_content,
                "detail": description.detail_content,
                "full": description.full_description,
            },
            "classification": {
                "usage_space": description.usage_space,
                "usage_category": description.usage_category,
                "usage_subcategories": description.usage_subcategories,
                "usage_classification_reason": description.usage_classification_reason,
                "usage_classification_suggestion": description.usage_classification_suggestion or {},
                "usage_classification_version": description.usage_classification_version,
            },
            "processing": {
                "pipeline_version": settings.pipeline_version,
                "description_model": settings.llm_model or settings.llm_provider,
                "description_prompt_version": description.prompt_version,
                "preview_source": "provided" if manifest.provided_previews else "generated",
                "description_source": "provided" if manifest.provided_description else "generated",
            },
        }
