"""Health-check and stats endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db, get_milvus, get_reranker
from app.config import settings
from app.models.tables import ResourceEmbedding, ResourceTask

router = APIRouter(tags=["health"])


class ComponentHealth(BaseModel):
    status: str
    detail: str = ""


class HealthOut(BaseModel):
    status: str
    postgres: ComponentHealth
    milvus: ComponentHealth
    reranker: ComponentHealth


@router.get("/health", response_model=HealthOut)
async def health_check(session: AsyncSession = Depends(get_db)):
    pg = ComponentHealth(status="ok")
    mv = ComponentHealth(status="ok")
    rk = ComponentHealth(status="ok")

    try:
        await session.execute(text("SELECT 1"))
    except Exception as exc:
        pg = ComponentHealth(status="error", detail=str(exc))

    try:
        client = get_milvus()
        client.list_collections()
    except Exception as exc:
        mv = ComponentHealth(status="error", detail=str(exc))

    try:
        reranker = get_reranker()
        rh = await reranker.health()
        if rh.get("status") != "ok":
            rk = ComponentHealth(status=rh.get("status", "degraded"), detail=rh.get("detail", ""))
    except Exception as exc:
        rk = ComponentHealth(status="error", detail=str(exc))

    # Reranker failure only causes "degraded", not "error"
    core_ok = all(c.status == "ok" for c in [pg, mv])
    overall = "ok" if core_ok and rk.status == "ok" else ("degraded" if core_ok else "error")
    return HealthOut(status=overall, postgres=pg, milvus=mv, reranker=rk)


class StatsOut(BaseModel):
    db_resource_count: int = 0
    db_state_counts: dict = {}
    db_embedding_count: int = 0
    milvus_vector_count: int = 0
    milvus_collection: str = ""


@router.get("/stats", response_model=StatsOut)
async def server_stats(session: AsyncSession = Depends(get_db)):
    """Aggregate counts across DB and Milvus."""
    result = StatsOut()

    try:
        total = (await session.execute(select(func.count()).select_from(ResourceTask))).scalar() or 0
        result.db_resource_count = total

        state_rows = (
            await session.execute(
                select(ResourceTask.process_state, func.count())
                .group_by(ResourceTask.process_state)
            )
        ).all()
        result.db_state_counts = {row[0]: row[1] for row in state_rows}

        result.db_embedding_count = (
            await session.execute(select(func.count()).select_from(ResourceEmbedding))
        ).scalar() or 0
    except Exception:
        pass

    try:
        client = get_milvus()
        coll_name = settings.milvus_collection
        result.milvus_collection = coll_name
        if client.has_collection(coll_name):
            stats = client.get_collection_stats(coll_name)
            result.milvus_vector_count = int(stats.get("row_count", 0))
    except Exception:
        pass

    # Milvus row_count can lag behind recent inserts until a flush, while the
    # embedding row is only committed after the Milvus insert succeeds.
    result.milvus_vector_count = max(result.milvus_vector_count, result.db_embedding_count)

    return result
