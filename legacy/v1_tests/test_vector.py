"""Tests for vector.py: LanceDB vector store operations.

Uses real LanceDB with temp directories (no mocking needed).
"""

from __future__ import annotations

import os

import pytest

from yacmemo.vector import VectorStore


@pytest.fixture
def vector_store(tmp_path):
    """Create a VectorStore in a temp directory."""
    path = str(tmp_path / "lancedb")
    os.makedirs(path, exist_ok=True)
    store = VectorStore(path, dimensions=1024)
    return store


def _make_embedding(seed: float = 0.1) -> list[float]:
    """Create a simple 1024-dim embedding vector."""
    return [seed] * 1024


class TestVectorStoreCreation:
    def test_creates_tables(self, vector_store):
        # Tables should exist after creation
        for name in ["node_vectors", "event_vectors", "edge_vectors"]:
            tbl = vector_store.db.open_table(name)
            assert tbl is not None


class TestNodeVectors:
    def test_upsert_and_search(self, vector_store):
        emb = _make_embedding(0.5)
        vector_store.upsert_node_vector("node1", "test entity", emb, "source.md")

        results = vector_store.search_nodes(emb, limit=1)
        assert len(results) >= 1
        assert results[0]["id"] == "node1"
        assert results[0]["text"] == "test entity"

    def test_upsert_replaces_existing(self, vector_store):
        emb = _make_embedding(0.5)
        vector_store.upsert_node_vector("node1", "old text", emb, "src.md")
        vector_store.upsert_node_vector("node1", "new text", emb, "src.md")

        results = vector_store.search_nodes(emb, limit=10)
        # Should only have one record for node1
        node1_results = [r for r in results if r["id"] == "node1"]
        assert len(node1_results) == 1
        assert node1_results[0]["text"] == "new text"


class TestEventVectors:
    def test_upsert_event(self, vector_store):
        emb = _make_embedding(0.3)
        vector_store.upsert_event_vector("evt1", "event text", emb, "src.md")
        # Search across all tables
        results = vector_store.search_all(emb, limit=1)
        assert len(results) >= 1
        assert any(r["id"] == "evt1" for r in results)


class TestEdgeVectors:
    def test_upsert_edge(self, vector_store):
        emb = _make_embedding(0.7)
        vector_store.upsert_edge_vector("edge1", "edge text", emb, "src.md")
        results = vector_store.search_all(emb, limit=1)
        assert len(results) >= 1
        assert any(r["id"] == "edge1" for r in results)


class TestSearchAll:
    def test_merges_results(self, vector_store):
        emb_n = _make_embedding(0.1)
        emb_e = _make_embedding(0.9)

        vector_store.upsert_node_vector("n1", "node", emb_n, "src.md")
        vector_store.upsert_event_vector("e1", "event", emb_e, "src.md")

        # Search with node embedding
        results = vector_store.search_all(emb_n, limit=10)
        ids = {r["id"] for r in results}
        assert "n1" in ids

    def test_returns_kind(self, vector_store):
        emb = _make_embedding(0.5)
        vector_store.upsert_node_vector("n1", "text", emb, "src.md")
        vector_store.upsert_event_vector("e1", "text", emb, "src.md")

        results = vector_store.search_all(emb, limit=10)
        kinds = {r.get("kind") for r in results}
        assert "node" in kinds
        assert "event" in kinds

    def test_limit(self, vector_store):
        emb = _make_embedding(0.5)
        for i in range(20):
            vector_store.upsert_node_vector(f"n{i}", f"text{i}", emb, "src.md")

        results = vector_store.search_all(emb, limit=5)
        assert len(results) == 5


class TestDeleteBySource:
    def test_deletes_by_source(self, vector_store):
        emb = _make_embedding(0.5)
        vector_store.upsert_node_vector("n1", "text", emb, "file1.md")
        vector_store.upsert_node_vector("n2", "text", emb, "file2.md")

        vector_store.delete_by_source("file1.md")

        results = vector_store.search_nodes(emb, limit=100)
        ids = {r["id"] for r in results}
        assert "n1" not in ids
        assert "n2" in ids

    def test_delete_nonexistent_source_no_error(self, vector_store):
        vector_store.delete_by_source("nonexistent.md")
