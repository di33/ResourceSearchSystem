"""DashScope (通义千问) text embedding provider.

Requires:
    pip install dashscope
    export DASHSCOPE_API_KEY=sk-xxxxxxxx   # 阿里云百炼 API Key
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import List

from ResourceProcessor.embedding.embedding_generator import (
    BaseEmbeddingProvider,
    EmbeddingFactory,
)

logger = logging.getLogger(__name__)


class DashScopeEmbeddingProvider(BaseEmbeddingProvider):
    """通义千问 text-embedding 向量生成。

    Parameters
    ----------
    model : str
        模型名称，默认 ``"text-embedding-v3"``。
        可选 ``"text-embedding-v4"``（更高精度、支持 2048 维）。
    dimension : int
        输出向量维度。text-embedding-v3 支持 1024/768/512/256/128/64，默认 1024。
    api_key : str | None
        DashScope API Key。为 None 时从环境变量 ``DASHSCOPE_API_KEY`` 读取。
    """

    def __init__(
        self,
        model: str = "text-embedding-v3",
        dimension: int = 1024,
        api_key: str | None = None,
    ):
        self._model = model
        self._dimension = dimension
        self._api_key = api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "DashScope API Key 未设置。请设置环境变量 DASHSCOPE_API_KEY "
                "或在构造时传入 api_key 参数。"
            )

    def _call_sync(self, text: str) -> List[float]:
        from http import HTTPStatus

        import dashscope
        from dashscope import TextEmbedding

        dashscope.api_key = self._api_key

        response = TextEmbedding.call(
            model=self._model,
            input=text,
            dimension=self._dimension,
        )

        if response.status_code != HTTPStatus.OK:
            raise RuntimeError(
                f"DashScope Embedding API 调用失败: code={response.status_code}, "
                f"message={response.message}"
            )

        return response.output["embeddings"][0]["embedding"]

    async def generate_embedding(self, text: str) -> List[float]:
        return await asyncio.to_thread(self._call_sync, text)

    def expected_dimension(self) -> int:
        return self._dimension

    def model_version(self) -> str:
        return self._model


EmbeddingFactory.register("dashscope", DashScopeEmbeddingProvider)
EmbeddingFactory.register("text-embedding-v3", DashScopeEmbeddingProvider)
