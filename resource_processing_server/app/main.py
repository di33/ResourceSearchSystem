from __future__ import annotations

import logging

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, status

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

app = FastAPI(
    title="资源加工服务器",
    description="Consumes object-storage resource manifests, generates previews/descriptions, and upserts to search server.",
    version="0.1.0",
    debug=settings.debug,
)

service = ProcessingService()


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
    return {"status": "ok"}


@app.post("/processing-jobs", response_model=CreateJobOut)
async def create_processing_job(
    manifest: ResourceManifest,
    background_tasks: BackgroundTasks,
    client_id: str = Depends(require_client_id),
):
    created = await service.create_job(client_id=client_id, manifest=manifest)
    if settings.process_inline:
        await service.run_job(created.job_id)
    else:
        background_tasks.add_task(service.run_job, created.job_id)
    return created


@app.post("/processing-jobs/batch", response_model=CreateBatchOut)
async def create_processing_job_batch(
    batch: ResourceBatchManifest,
    background_tasks: BackgroundTasks,
    client_id: str = Depends(require_client_id),
):
    created = await service.create_batch(client_id=client_id, manifests=batch.manifests)
    if settings.process_inline:
        for item in created.jobs:
            await service.run_job(item.job_id)
    else:
        for item in created.jobs:
            background_tasks.add_task(service.run_job, item.job_id)
    return created


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
    background_tasks: BackgroundTasks,
    client_id: str = Depends(require_client_id),
):
    created = await service.retry_job(job_id, client_id=client_id)
    if created is None:
        raise HTTPException(status_code=404, detail="job not found")
    if settings.process_inline:
        await service.run_job(created.job_id)
    else:
        background_tasks.add_task(service.run_job, created.job_id)
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
