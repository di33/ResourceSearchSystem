"""智谱 AI (ZhipuAI) text embedding provider.

Requires:
    pip install zhipuai
    export ZHIPUAI_API_KEY=xxxxxxxx   # 智谱开放平台 API Key
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


class ZhipuEmbeddingProvider(BaseEmbeddingProvider):
    """智谱 AI embedding-3 向量生成。

    Parameters
    ----------
    model : str
        模型名称，默认 ``"embedding-3"``。
    dimension : int
        输出向量维度。embedding-3 支持 2048（默认）/1024/512/256。
    api_key : str | None
        智谱 API Key。为 None 时从环境变量 ``ZHIPUAI_API_KEY`` 读取。
    """

    def __init__(
        self,
        model: str = "embedding-3",
        dimension: int = 2048,
        api_key: str | None = None,
    ):
        self._model = model
        self._dimension = dimension
        self._api_key = api_key or os.environ.get("ZHIPUAI_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "智谱 API Key 未设置。请设置环境变量 ZHIPUAI_API_KEY "
                "或在构造时传入 api_key 参数。"
            )

    def _call_sync(self, text: str) -> List[float]:
        from zhipuai import ZhipuAI

        client = ZhipuAI(api_key=self._api_key)

        response = client.embeddings.create(
            model=self._model,
            input=text,
            dimensions=self._dimension,
        )

        return response.data[0].embedding

    async def generate_embedding(self, text: str) -> List[float]:
        return await asyncio.to_thread(self._call_sync, text)

    def expected_dimension(self) -> int:
        return self._dimension

    def model_version(self) -> str:
        return self._model


EmbeddingFactory.register("zhipu", ZhipuEmbeddingProvider)
EmbeddingFactory.register("embedding-3", ZhipuEmbeddingProvider)
