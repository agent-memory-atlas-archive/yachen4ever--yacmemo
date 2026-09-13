"""Tests for embedding.py: EmbeddingClient with mocked httpx."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from yacmemo.embedding import EmbeddingClient


def _mock_embedding_response(vectors: list[list[float]]) -> MagicMock:
    """Create a mock httpx.Response for embeddings endpoint."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "data": [
            {"index": i, "embedding": v}
            for i, v in enumerate(vectors)
        ]
    }
    resp.raise_for_status = MagicMock()
    return resp


class TestEmbed:
    def test_batch_embed(self):
        emb = EmbeddingClient("http://localhost:11235/v1", "sk-test", "model")
        vectors = [[0.1] * 1024, [0.2] * 1024]
        with patch("httpx.post", return_value=_mock_embedding_response(vectors)):
            result = emb.embed(["text1", "text2"])
            assert len(result) == 2
            assert result[0] == vectors[0]
            assert result[1] == vectors[1]

    def test_empty_batch(self):
        emb = EmbeddingClient("http://localhost:11235/v1", "sk-test", "model")
        assert emb.embed([]) == []

    def test_preserves_order(self):
        emb = EmbeddingClient("http://localhost:11235/v1", "sk-test", "model")
        # Response with shuffled indices
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "data": [
                {"index": 1, "embedding": [0.2] * 1024},
                {"index": 0, "embedding": [0.1] * 1024},
            ]
        }
        resp.raise_for_status = MagicMock()
        with patch("httpx.post", return_value=resp):
            result = emb.embed(["first", "second"])
            assert result[0] == [0.1] * 1024  # index 0
            assert result[1] == [0.2] * 1024  # index 1


class TestEmbedOne:
    def test_single_embed(self):
        emb = EmbeddingClient("http://localhost:11235/v1", "sk-test", "model")
        with patch("httpx.post", return_value=_mock_embedding_response([[0.5] * 1024])):
            result = emb.embed_one("test text")
            assert len(result) == 1024
            assert result[0] == 0.5

    def test_sends_dimensions(self):
        emb = EmbeddingClient("http://localhost:11235/v1", "sk-test", "model",
                               dimensions=512)
        with patch("httpx.post", return_value=_mock_embedding_response([[0.5] * 512])) as mock_post:
            emb.embed_one("text")
            payload = mock_post.call_args.kwargs["json"]
            assert payload["dimensions"] == 512


class TestBaseUrl:
    def test_trailing_slash_stripped(self):
        emb = EmbeddingClient("http://localhost:11235/v1/", "sk-test", "model")
        assert emb.base_url == "http://localhost:11235/v1"
