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
        """Batch embed texts. Returns list of embedding vectors."""
        if not texts:
            return []
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = httpx.post(
            f"{self.base_url}/embeddings",
            headers=headers,
            json={
                "model": self.model,
                "input": texts,
                "dimensions": self.dimensions,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # Sort by index to ensure order matches input
        items = sorted(data["data"], key=lambda x: x["index"])
        return [item["embedding"] for item in items]

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text. Returns one embedding vector."""
        return self.embed([text])[0]
