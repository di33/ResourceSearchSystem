"""BGE-Reranker-v2-m3 FastAPI service.

Exposes POST /rerank for cross-encoder reranking.
Uses sentence-transformers CrossEncoder for broad compatibility.
"""

from __future__ import annotations

import logging
import math
import time
from contextlib import asynccontextmanager
from typing import List, Optional

import numpy as np
import torch
from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic contracts
# ---------------------------------------------------------------------------

class RerankRequest(BaseModel):
    query: str
    documents: List[str]
    top_k: Optional[int] = None


class RerankResultItem(BaseModel):
    index: int
    score: float


class RerankResponse(BaseModel):
    results: List[RerankResultItem]


class HealthResponse(BaseModel):
    status: str
    model: str = "BAAI/bge-reranker-v2-m3"
    device: str = ""
    use_fp16: bool = False


# ---------------------------------------------------------------------------
# Model singleton
# ---------------------------------------------------------------------------

_reranker = None
_device: str = "cpu"
_use_fp16: bool = False


def _load_model():
    global _reranker, _device, _use_fp16

    from sentence_transformers import CrossEncoder

    if torch.cuda.is_available():
        _device = "cuda"
        _use_fp16 = True
    else:
        _device = "cpu"
        _use_fp16 = False

    logger.info("Loading BGE-Reranker-v2-m3 on %s (fp16=%s) …", _device, _use_fp16)
    _reranker = CrossEncoder(
        "BAAI/bge-reranker-v2-m3",
        device=_device,
        automodel_args={"torch_dtype": torch.float16 if _use_fp16 else torch.float32},
    )
    logger.info("Model loaded successfully.")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_model()
    yield


app = FastAPI(title="BGE-Reranker Service", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health():
    loaded = _reranker is not None
    return HealthResponse(
        status="ok" if loaded else "loading",
        device=_device,
        use_fp16=_use_fp16,
    )


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest):
    if _reranker is None:
        return RerankResponse(results=[])

    if not req.documents:
        return RerankResponse(results=[])

    pairs = [[req.query, doc] for doc in req.documents]

    t0 = time.perf_counter()
    raw_scores = _reranker.predict(pairs, show_progress_bar=False)
    elapsed = (time.perf_counter() - t0) * 1000
    logger.debug("Reranked %d docs in %.1fms", len(pairs), elapsed)

    # CrossEncoder.predict returns ndarray of logits
    if isinstance(raw_scores, np.ndarray):
        logits = raw_scores.tolist()
    elif isinstance(raw_scores, (float, int)):
        logits = [float(raw_scores)]
    else:
        logits = [float(s) for s in raw_scores]

    # Apply sigmoid to normalize logits to [0, 1]
    scores = [1.0 / (1.0 + math.exp(-x)) for x in logits]

    items = [
        RerankResultItem(index=i, score=float(s))
        for i, s in enumerate(scores)
    ]
    items.sort(key=lambda x: x.score, reverse=True)

    if req.top_k is not None and req.top_k > 0:
        items = items[: req.top_k]

    return RerankResponse(results=items)
