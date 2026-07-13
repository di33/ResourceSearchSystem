from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status

from resource_contracts.auth import (
    ClientAuthConfig,
    extract_api_key,
    parse_client_api_keys,
    split_csv,
    validate_client_api_key,
)
from resource_processing_server.app.config import settings
from resource_processing_server.app.models import (
    CreateBatchOut,
    CreateJobOut,
    DeleteProcessedResourceIn,
    DeleteProcessedResourceOut,
    JobOut,
    JobStatusBatchIn,
    JobStatusBatchOut,
    ReplaySnapshotOut,
    ReplaySnapshotsOut,
    ResourceBatchManifest,
    ResourceManifest,
)
from resource_processing_server.app.processor import ProcessingService

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

service = ProcessingService()


async def _replay_failed_snapshots_on_startup() -> None:
    if not settings.replay_failed_snapshots_on_startup:
        return
    try:
        items = await service.replay_snapshots(
            client_id="",
            search_upsert_state="upsert_failed",
            limit=settings.replay_failed_snapshots_startup_limit,
        )
    except Exception:
        logger.exception("Startup replay of failed snapshots crashed")
        return
    if not items:
        return
    states: dict[str, int] = {}
    for item in items:
        states[item.state] = states.get(item.state, 0) + 1
    logger.info("Startup replayed %s failed snapshots: %s", len(items), states)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await service.start_workers()
    task = asyncio.create_task(_replay_failed_snapshots_on_startup())
    try:
        yield
    finally:
        if not task.done():
            task.cancel()
        await service.stop_workers()

app = FastAPI(
    title="资源加工服务器",
    description="Consumes object-storage resource manifests, generates previews/descriptions, and upserts to search server.",
    version="0.1.0",
    debug=settings.debug,
    lifespan=lifespan,
)


def _client_auth_config() -> ClientAuthConfig:
    return ClientAuthConfig(
        client_keys=parse_client_api_keys(settings.client_api_keys),
        admin_keys=split_csv(settings.admin_api_keys),
        debug=settings.debug,
    )


async def require_client_id(
    x_client_id: str | None = Header(default=None, alias="X-Client-Id"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> str:
    client_id = (x_client_id or "").strip()
    if not client_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-Client-Id header is required")
    api_key = extract_api_key(authorization=authorization, x_api_key=x_api_key)
    if not validate_client_api_key(client_id, api_key, _client_auth_config()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API key for X-Client-Id",
        )
    return client_id


@app.get("/health")
async def health():
    database_ok = service.database is None or await asyncio.to_thread(service.database.ping)
    if not database_ok:
        raise HTTPException(status_code=503, detail="processing database unavailable")
    return {"status": "ok", "database": "ok" if service.database is not None else "memory"}


@app.post("/processing-jobs", response_model=CreateJobOut)
async def create_processing_job(
    manifest: ResourceManifest,
    client_id: str = Depends(require_client_id),
):
    try:
        created = await service.create_job(client_id=client_id, manifest=manifest)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if settings.process_inline:
        await service.run_job(created.job_id)
    return created


@app.post("/processing-jobs/batch", response_model=CreateBatchOut)
async def create_processing_job_batch(
    batch: ResourceBatchManifest,
    client_id: str = Depends(require_client_id),
):
    try:
        created = await service.create_batch(client_id=client_id, manifests=batch.manifests)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if settings.process_inline:
        for item in created.jobs:
            await service.run_job(item.job_id)
    return created


@app.post("/processing-jobs/status", response_model=JobStatusBatchOut)
async def get_processing_job_statuses(
    request: JobStatusBatchIn,
    client_id: str = Depends(require_client_id),
):
    jobs, missing_job_ids = await service.get_job_statuses(request.job_ids, client_id=client_id)
    return JobStatusBatchOut(jobs=jobs, missing_job_ids=missing_job_ids)


@app.get("/processing-jobs/{job_id}", response_model=JobOut)
async def get_processing_job(
    job_id: str,
    client_id: str = Depends(require_client_id),
):
    job = await service.get_job(job_id, client_id=client_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.post("/processing-jobs/{job_id}/retry", response_model=CreateJobOut)
async def retry_processing_job(
    job_id: str,
    client_id: str = Depends(require_client_id),
):
    created = await service.retry_job(job_id, client_id=client_id)
    if created is None:
        raise HTTPException(status_code=404, detail="job not found")
    if settings.process_inline:
        await service.run_job(created.job_id)
    return created


@app.post("/processed-resources/delete", response_model=DeleteProcessedResourceOut)
async def delete_processed_resource(
    request: DeleteProcessedResourceIn,
    client_id: str = Depends(require_client_id),
):
    return await service.delete_processed_resource(client_id=client_id, request=request)


@app.post("/processed-resource-snapshots/{client_resource_id}/replay", response_model=ReplaySnapshotOut)
async def replay_processed_resource_snapshot(
    client_resource_id: str,
    client_id: str = Depends(require_client_id),
):
    result = await service.replay_snapshot(client_id=client_id, client_resource_id=client_resource_id)
    if result is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    return result


@app.post("/processed-resource-snapshots/replay", response_model=ReplaySnapshotsOut)
async def replay_processed_resource_snapshots(
    client_id: str = Depends(require_client_id),
    search_upsert_state: str = Query(default=""),
    limit: int | None = Query(default=None, ge=1),
):
    items = await service.replay_snapshots(
        client_id=client_id,
        search_upsert_state=search_upsert_state,
        limit=limit,
    )
    return ReplaySnapshotsOut(total=len(items), items=items)
