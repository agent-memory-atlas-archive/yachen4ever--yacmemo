"""Embedding API client for omlx on m2ultra:11235."""

from __future__ import annotations

import httpx


class EmbeddingClient:
    """Client for OpenAI-compatible /v1/embeddings endpoint (omlx)."""

    def __init__(self, base_url: str, api_key: str, model: str,
                 dimensions: int = 1024, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions
        self.timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch embed texts. Returns list of embedding vectors.

        瞬时失败（网络抖动/端点重启）重试一次——写路径的向量失败会把笔记
        标成缺向量等审计补位，一次重试能省掉整轮延迟。"""
        if not texts:
            return []
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "input": texts,
            "dimensions": self.dimensions,
        }
        data = None
        for attempt in (1, 2):
            try:
                resp = httpx.post(
                    f"{self.base_url}/embeddings",
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception:
                if attempt == 2:
                    raise
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ValueError(f"embedding 端点返回异常：{str(data)[:120]}")
        # Sort by index to ensure order matches input
        items = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in items]

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text. Returns one embedding vector."""
        return self.embed([text])[0]
