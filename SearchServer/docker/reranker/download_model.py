"""Download the reranker model.

Default strategy:
1. Try Hugging Face first.
2. Fall back to ModelScope if Hugging Face is unavailable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from huggingface_hub import snapshot_download as hf_snapshot_download
from modelscope import snapshot_download as modelscope_snapshot_download


MODEL_ID = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
MODELSCOPE_MODEL_ID = os.getenv("MODELSCOPE_MODEL", MODEL_ID)
DOWNLOAD_SOURCE = os.getenv("RERANKER_DOWNLOAD_SOURCE", "auto").strip().lower()

HF_CACHE_DIR = os.getenv("HF_HOME", "")
MODELSCOPE_CACHE_DIR = Path(os.getenv("MODELSCOPE_CACHE", "/root/.cache/huggingface/modelscope"))


def _download_huggingface() -> str:
    kwargs = {}
    if HF_CACHE_DIR:
        kwargs["cache_dir"] = HF_CACHE_DIR
    return hf_snapshot_download(MODEL_ID, **kwargs)


def _download_modelscope() -> str:
    MODELSCOPE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return modelscope_snapshot_download(MODELSCOPE_MODEL_ID, cache_dir=str(MODELSCOPE_CACHE_DIR))


def main() -> None:
    if DOWNLOAD_SOURCE not in {"auto", "huggingface", "modelscope"}:
        raise ValueError("RERANKER_DOWNLOAD_SOURCE must be auto, huggingface, or modelscope")

    if DOWNLOAD_SOURCE in {"auto", "huggingface"}:
        try:
            model_path = _download_huggingface()
            print(model_path)
            return
        except Exception as exc:
            if DOWNLOAD_SOURCE == "huggingface":
                raise
            print(f"Hugging Face download failed, falling back to ModelScope: {exc}", file=sys.stderr)

    model_path = _download_modelscope()
    print(model_path)


if __name__ == "__main__":
    main()
