from typing import List

import httpx
import numpy as np

import config

# 本地模型名（与之前下载并缓存的一致）
LOCAL_MODEL_NAME = "BAAI/bge-small-zh-v1.5"


class EmbeddingModel:
    """最小的 embedding 封装：输入文本列表，返回 (n, dim) 的向量矩阵。"""

    def __init__(self):
        self.source = (config.EMBEDDING_MODEL_SOURCE or "LOCAL").upper()

        if self.source == "LOCAL":
            # 延迟导入：导入本模块时不会加载模型
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=LOCAL_MODEL_NAME)
        elif self.source == "API":
            if not config.EMBEDDING_MODEL_URL:
                raise ValueError(
                    "EMBEDDING_MODEL_SOURCE=API，但未配置 EMBEDDING_MODEL_URL"
                )
            self._url = config.EMBEDDING_MODEL_URL
            self._api_key = config.EMBEDDING_MODEL_API_KEY
            self._model_name = config.EMBEDDING_MODEL_NAME
        else:
            raise ValueError(
                f"未知的 EMBEDDING_MODEL_SOURCE: {self.source}（应为 LOCAL 或 API）"
            )

    def embed(self, texts: List[str]) -> np.ndarray:
        """把一批文本转成向量矩阵，形状 (len(texts), dim)。"""
        if not texts:
            return np.zeros((0, 0), dtype=float)

        if self.source == "LOCAL":
            return np.array(list(self._model.embed(texts)), dtype=float)

        # API：OpenAI 兼容的 /embeddings 接口
        response = httpx.post(
            self._url,  # type: ignore[arg-type]
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"model": self._model_name, "input": texts},
            timeout=config.TIMEOUT,
        )
        response.raise_for_status()
        data = sorted(response.json()["data"], key=lambda item: item["index"])
        return np.array([item["embedding"] for item in data], dtype=float)


_embedding_model: EmbeddingModel | None = None


def get_embedding_model() -> EmbeddingModel:
    """惰性单例：模型只初始化一次，避免每次检索都重新加载。"""
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = EmbeddingModel()
    return _embedding_model
