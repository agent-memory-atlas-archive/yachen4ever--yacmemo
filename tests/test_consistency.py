"""Tests for consistency.py: Layer 3 contradiction detection."""

from __future__ import annotations

import os
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from yacmemo.consistency import ConsistencyChecker
from yacmemo.db import MemoryDB
from yacmemo.vector import VectorStore


@pytest.fixture
def consistency_setup(tmp_path):
    """Create ConsistencyChecker with mocked LLM/embedding and temp DB.

    Uses a mock embedding that returns the same vector for all inputs,
    so vector search always finds candidates (distance ≈ 0 < threshold).
    """
    memory_root = str(tmp_path / "memory")
    os.makedirs(memory_root, exist_ok=True)

    lancedb_path = str(tmp_path / "lancedb")
    os.makedirs(lancedb_path, exist_ok=True)
    vector = VectorStore(lancedb_path, dimensions=1024)

    db = MemoryDB(str(tmp_path / "system.db"))
    db.add_user("alice", "Alice", memory_root)

    mock_llm = MagicMock()

    # Mock embedding: always returns the same vector so distance ≈ 0
    mock_emb = MagicMock()
    fixed_vector = [0.1] * 1024
    mock_emb.embed_one.return_value = fixed_vector

    @dataclass
    class TestConsistencyCfg:
        similarity_threshold: float = 0.85
        confidence_threshold: float = 0.8
        auto_invalidate: bool = True

    @dataclass
    class TestLLMCfg:
        consistency_max_tokens: int = 512
        consistency_timeout: int = 30

    @dataclass
    class TestConfig:
        consistency: TestConsistencyCfg
        llm: TestLLMCfg

    config = TestConfig(
        consistency=TestConsistencyCfg(),
        llm=TestLLMCfg(),
    )

    checker = ConsistencyChecker(config, db, vector, mock_llm, mock_emb)

    yield checker, db, vector, mock_llm

    db.close()


class TestCheckNewNode:
    def test_no_contradiction(self, consistency_setup):
        checker, db, vector, mock_llm = consistency_setup

        # Add a node
        node_id = db.upsert_node("alice", "EntityA", "concept", "some summary",
                                 "src.md", "h", "orig.md")

        # LLM says no contradiction
        mock_llm.chat_json.return_value = {
            "contradictory": False,
            "confidence": 0.3,
            "reason": "no conflict",
        }

        # Should not raise, should not invalidate
        checker.check_new_node("alice", node_id)

        node = db.get_node("alice", node_id)
        assert node["valid"] == 1  # still valid

    def test_contradiction_auto_invalidate(self, consistency_setup):
        checker, db, vector, mock_llm = consistency_setup

        # Add old node first
        old_id = db.upsert_node("alice", "Config", "setting", "port=8080",
                                "old.md", "h1", "orig.md")
        # Index old node vector (same embedding for all → distance ≈ 0)
        vector.upsert_node_vector(old_id, "Config: port=8080", [0.1] * 1024, "old.md")

        # Add new node (similar name, different value)
        new_id = db.upsert_node("alice", "Config", "setting", "port=9721",
                                "new.md", "h2", "orig.md")
        vector.upsert_node_vector(new_id, "Config: port=9721", [0.1] * 1024, "new.md")

        # LLM says contradiction with high confidence
        mock_llm.chat_json.return_value = {
            "contradictory": True,
            "current": "B",
            "reason": "port changed from 8080 to 9721",
            "confidence": 0.95,
        }

        checker.check_new_node("alice", new_id)

        # Old node should be auto-invalidated
        old_node = db.get_node("alice", old_id)
        assert old_node["valid"] == 0
        assert "superseded" in old_node["invalid_reason"]

    def test_contradiction_low_confidence_no_invalidate(self, consistency_setup):
        checker, db, vector, mock_llm = consistency_setup

        old_id = db.upsert_node("alice", "X", "type", "old value",
                                "old.md", "h", "orig.md")
        vector.upsert_node_vector(old_id, "X: old value", [0.1] * 1024, "old.md")

        new_id = db.upsert_node("alice", "X", "type", "new value",
                                "new.md", "h", "orig.md")
        vector.upsert_node_vector(new_id, "X: new value", [0.1] * 1024, "new.md")

        # Low confidence → should log but not auto-invalidate
        mock_llm.chat_json.return_value = {
            "contradictory": True,
            "confidence": 0.5,  # below 0.8 threshold
            "reason": "maybe contradictory",
        }

        checker.check_new_node("alice", new_id)

        # Old node still valid (low confidence)
        old_node = db.get_node("alice", old_id)
        assert old_node["valid"] == 1

        # But consistency log should have a pending entry
        pending = db.get_pending_consistency("alice")
        assert len(pending) == 1


class TestCheckAll:
    def test_scans_all_nodes(self, consistency_setup):
        checker, db, vector, mock_llm = consistency_setup

        # Add multiple nodes with vectors
        for i in range(5):
            node_id = db.upsert_node("alice", f"Entity{i}", "type", f"summary{i}",
                           f"src{i}.md", f"h{i}", "orig.md")
            vector.upsert_node_vector(node_id, f"Entity{i}: summary{i}", [0.1] * 1024, f"src{i}.md")

        mock_llm.chat_json.return_value = {
            "contradictory": False, "confidence": 0.1, "reason": "no conflict",
        }

        checker.check_all("alice")

        # LLM should have been called for each node pair
        assert mock_llm.chat_json.call_count > 0

    def test_no_nodes_no_calls(self, consistency_setup):
        checker, db, vector, mock_llm = consistency_setup

        checker.check_all("alice")
        assert mock_llm.chat_json.call_count == 0

    def test_user_isolation(self, consistency_setup):
        checker, db, vector, mock_llm = consistency_setup

        db.add_user("bob", "Bob", "/tmp/bob")

        # Add 2 nodes for alice with vectors (need ≥2 for comparison)
        a1 = db.upsert_node("alice", "A1", "type", "alice1", "src1.md", "h", "orig.md")
        vector.upsert_node_vector(a1, "A1: alice1", [0.1] * 1024, "src1.md")
        a2 = db.upsert_node("alice", "A2", "type", "alice2", "src2.md", "h", "orig.md")
        vector.upsert_node_vector(a2, "A2: alice2", [0.1] * 1024, "src2.md")

        # Add 2 nodes for bob with vectors
        b1 = db.upsert_node("bob", "B1", "type", "bob1", "src1.md", "h", "orig.md")
        vector.upsert_node_vector(b1, "B1: bob1", [0.1] * 1024, "src1.md")
        b2 = db.upsert_node("bob", "B2", "type", "bob2", "src2.md", "h", "orig.md")
        vector.upsert_node_vector(b2, "B2: bob2", [0.1] * 1024, "src2.md")

        mock_llm.chat_json.return_value = {
            "contradictory": False, "confidence": 0.1, "reason": "ok",
        }

        checker.check_all("alice")
        alice_calls = mock_llm.chat_json.call_count

        checker.check_all("bob")
        bob_calls = mock_llm.chat_json.call_count - alice_calls

        # Each user's nodes should be checked separately
        assert alice_calls > 0, "Alice should have LLM calls"
        assert bob_calls > 0, "Bob should have LLM calls"


class TestConsistencyLog:
    def test_log_recorded_on_contradiction(self, consistency_setup):
        checker, db, vector, mock_llm = consistency_setup

        old_id = db.upsert_node("alice", "X", "t", "old", "old.md", "h", "orig.md")
        vector.upsert_node_vector(old_id, "X: old", [0.1] * 1024, "old.md")

        new_id = db.upsert_node("alice", "X", "t", "new", "new.md", "h", "orig.md")
        vector.upsert_node_vector(new_id, "X: new", [0.1] * 1024, "new.md")

        mock_llm.chat_json.return_value = {
            "contradictory": True,
            "confidence": 0.9,
            "reason": "changed",
        }

        checker.check_new_node("alice", new_id)

        # Check consistency log
        logs = db.conn.execute(
            "SELECT * FROM consistency_log WHERE user_id='alice'"
        ).fetchall()
        assert len(logs) >= 1
        assert logs[0]["reason"] == "changed"
