"""BGE-Reranker-v2-m3 FastAPI service.

Exposes POST /rerank for cross-encoder reranking.
Uses sentence-transformers CrossEncoder for broad compatibility.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
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
    model_path: str = ""
    model_source: str = ""
    model_source_type: str = ""
    device: str = ""
    use_fp16: bool = False
    detail: str = ""


# ---------------------------------------------------------------------------
# Model singleton
# ---------------------------------------------------------------------------

_reranker = None
_device: str = "cpu"
_use_fp16: bool = False
_model_name: str = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
_explicit_model_path: str = os.getenv("RERANKER_MODEL_PATH", "")
_modelscope_cache: str = os.getenv("MODELSCOPE_CACHE", "/root/.cache/huggingface/modelscope")


def _default_modelscope_path(model_name: str) -> str:
    if "/" not in model_name:
        return f"{_modelscope_cache}/{model_name}"
    owner, name = model_name.split("/", 1)
    return f"{_modelscope_cache}/models/{owner}--{name}/snapshots/master"


_modelscope_model_path: str = os.getenv("MODELSCOPE_MODEL_PATH", _default_modelscope_path(_model_name))
_model_source: str = ""
_model_source_type: str = ""
_load_error: str = ""
_last_load_attempt: float = 0.0
_load_lock = threading.Lock()

_load_on_startup = os.getenv("RERANKER_LOAD_ON_STARTUP", "true").strip().lower() not in {"0", "false", "no"}
_auto_download = os.getenv("RERANKER_AUTO_DOWNLOAD", "false").strip().lower() in {"1", "true", "yes"}
_retry_seconds = float(os.getenv("RERANKER_RETRY_SECONDS", "300"))


def _candidate_sources() -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    if _explicit_model_path:
        sources.append(("local", _explicit_model_path))
    sources.append(("huggingface", _model_name))
    if Path(_modelscope_model_path).is_dir():
        sources.append(("modelscope", _modelscope_model_path))
    return sources


def _download_modelscope_model() -> str:
    from modelscope import snapshot_download

    Path(_modelscope_cache).mkdir(parents=True, exist_ok=True)
    return snapshot_download(_model_name, cache_dir=_modelscope_cache)


def _load_model() -> bool:
    global _reranker, _device, _use_fp16, _model_source, _model_source_type, _load_error, _last_load_attempt

    if _reranker is not None:
        return True

    with _load_lock:
        if _reranker is not None:
            return True

        _last_load_attempt = time.monotonic()
        _load_error = ""

        from sentence_transformers import CrossEncoder

        if torch.cuda.is_available():
            _device = "cuda"
            _use_fp16 = True
        else:
            _device = "cpu"
            _use_fp16 = False

        errors: list[str] = []
        for source_type, source in _candidate_sources():
            try:
                logger.info(
                    "Loading reranker model from %s source %s on %s (fp16=%s)",
                    source_type,
                    source,
                    _device,
                    _use_fp16,
                )
                _reranker = CrossEncoder(
                    source,
                    device=_device,
                    model_kwargs={"torch_dtype": torch.float16 if _use_fp16 else torch.float32},
                )
                _model_source = source
                _model_source_type = source_type
                logger.info("Reranker model loaded successfully from %s.", source_type)
                return True
            except Exception as exc:
                detail = f"{source_type} {source}: {type(exc).__name__}: {exc}"
                errors.append(detail)
                logger.warning("Reranker load attempt failed: %s", detail, exc_info=True)

        if _auto_download and not Path(_modelscope_model_path).is_dir():
            try:
                downloaded_path = _download_modelscope_model()
                logger.info("Downloaded reranker model from ModelScope to %s", downloaded_path)
                _reranker = CrossEncoder(
                    downloaded_path,
                    device=_device,
                    model_kwargs={"torch_dtype": torch.float16 if _use_fp16 else torch.float32},
                )
                _model_source = downloaded_path
                _model_source_type = "modelscope"
                logger.info("Reranker model loaded successfully from downloaded ModelScope cache.")
                return True
            except Exception as exc:
                errors.append(f"modelscope download: {type(exc).__name__}: {exc}")
                logger.warning("ModelScope fallback download failed.", exc_info=True)

        _load_error = " | ".join(errors)
        logger.error(
            "Reranker model is unavailable. Service will stay up and return empty rerank results until the model can be loaded."
        )
        return False


def _should_retry_load() -> bool:
    if _reranker is not None:
        return False
    if _last_load_attempt <= 0:
        return True
    return (time.monotonic() - _last_load_attempt) >= _retry_seconds


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    if _load_on_startup:
        _load_model()
    yield


app = FastAPI(title="BGE-Reranker Service", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health():
    loaded = _reranker is not None
    status = "ok" if loaded else ("degraded" if _load_error else "loading")
    return HealthResponse(
        status=status,
        model=_model_name,
        model_path=_explicit_model_path or _modelscope_model_path,
        model_source=_model_source,
        model_source_type=_model_source_type,
        device=_device,
        use_fp16=_use_fp16,
        detail="" if loaded else (_load_error or "model is not loaded yet"),
    )


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest):
    if _reranker is None:
        if _should_retry_load():
            _load_model()

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
