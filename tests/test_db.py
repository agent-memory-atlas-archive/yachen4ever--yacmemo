"""Tests for db.py: system-level SQLite with user isolation.

Covers: user CRUD, node/edge/event/processed_files/consistency_log
operations, user_id isolation, cascading delete.
"""

from __future__ import annotations

import sqlite3

import pytest


class TestUserCRUD:
    def test_add_and_get_user(self, db):
        user = db.get_user("alice")
        assert user is not None
        assert user["display_name"] == "Alice"

    def test_get_nonexistent_user(self, db):
        assert db.get_user("nonexistent") is None

    def test_list_users(self, db):
        users = db.list_users()
        assert len(users) == 2
        ids = {u["id"] for u in users}
        assert ids == {"alice", "bob"}

    def test_add_duplicate_user_raises(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            db.add_user("alice", "Alice2", "/tmp")

    def test_update_user(self, db):
        db.update_user("alice", display_name="Alice Updated")
        user = db.get_user("alice")
        assert user["display_name"] == "Alice Updated"

    def test_update_user_ignores_unknown_fields(self, db):
        db.update_user("alice", display_name="Alice", unknown_field="ignore")
        user = db.get_user("alice")
        assert "unknown_field" not in user

    def test_remove_user_cascades(self, db):
        # Add data for alice
        db.upsert_node("alice", "Node1", "concept", "summary",
                       "source.md", "hash1", "orig.md")
        db.upsert_event("alice", "2026-01-01", "test", "event",
                        "details", "source.md", "hash1", "orig.md")
        db.record_processed_file("alice", "source.md", "hash1", "", "success")
        db.add_consistency_log("alice", "n1", "n2", "old.md", "new.md",
                               "test reason", 0.9, False)

        # Remove alice
        db.remove_user("alice")

        # All alice's data should be gone
        assert db.get_user("alice") is None
        assert db.get_all_valid_nodes("alice") == []
        assert db.get_pending_consistency("alice") == []
        assert db.get_processed_file("alice", "source.md") is None

        # Bob's data (if any) should be intact
        bob = db.get_user("bob")
        assert bob is not None

    def test_remove_nonexistent_user_no_error(self, db):
        # Should not raise even if user doesn't exist
        db.remove_user("nonexistent")


class TestNodeCRUD:
    def test_insert_node(self, db):
        node_id = db.upsert_node("alice", "EntityA", "concept", "a summary",
                                 "source.md", "hash1", "orig.md")
        assert node_id is not None
        node = db.get_node("alice", node_id)
        assert node["name"] == "EntityA"
        assert node["type"] == "concept"
        assert node["valid"] == 1

    def test_upsert_updates_existing(self, db):
        node_id = db.upsert_node("alice", "EntityA", "concept", "v1",
                                 "source.md", "hash1", "orig.md")
        db.upsert_node("alice", "EntityA", "concept", "v2",
                       "source.md", "hash2", "orig.md")
        node = db.get_node("alice", node_id)
        assert node["summary"] == "v2"
        assert node["source_hash"] == "hash2"

    def test_user_isolation_nodes(self, db):
        db.upsert_node("alice", "EntityA", "concept", "alice's entity",
                       "source.md", "hash1", "orig.md")
        db.upsert_node("bob", "EntityA", "concept", "bob's entity",
                       "source.md", "hash1", "orig.md")

        alice_nodes = db.get_all_valid_nodes("alice")
        bob_nodes = db.get_all_valid_nodes("bob")
        assert len(alice_nodes) == 1
        assert len(bob_nodes) == 1
        assert alice_nodes[0]["summary"] == "alice's entity"
        assert bob_nodes[0]["summary"] == "bob's entity"

    def test_invalidate_node(self, db):
        node_id = db.upsert_node("alice", "EntityA", "concept", "summary",
                                 "source.md", "hash1", "orig.md")
        db.invalidate_node("alice", node_id, "superseded")
        node = db.get_node("alice", node_id)
        assert node["valid"] == 0
        assert node["invalid_reason"] == "superseded"
        assert node["invalid_at"] is not None

    def test_get_nodes_by_source(self, db):
        db.upsert_node("alice", "A", "type", "s1", "file1.md", "h1", "orig.md")
        db.upsert_node("alice", "B", "type", "s2", "file1.md", "h1", "orig.md")
        db.upsert_node("alice", "C", "type", "s3", "file2.md", "h2", "orig.md")

        nodes = db.get_nodes_by_source("alice", "file1.md")
        assert len(nodes) == 2

    def test_get_node_history(self, db):
        # Create a node, invalidate it, create a new one with same name
        old_id = db.upsert_node("alice", "Entity", "type", "old",
                                "source.md", "h1", "orig.md")
        db.invalidate_node("alice", old_id, "reason")
        db.upsert_node("alice", "Entity", "type", "new",
                       "source.md", "h2", "orig.md")

        history = db.get_node_history("alice", "Entity")
        assert len(history) == 2

    def test_delete_nodes_by_source(self, db):
        db.upsert_node("alice", "A", "type", "s", "file.md", "h", "orig.md")
        db.upsert_node("alice", "B", "type", "s", "file.md", "h", "orig.md")
        db.delete_nodes_by_source("alice", "file.md")
        assert db.get_nodes_by_source("alice", "file.md") == []

    def test_get_node_cross_user_returns_none(self, db):
        node_id = db.upsert_node("alice", "Entity", "type", "s",
                                 "src.md", "h", "orig.md")
        # Bob should not see Alice's node
        assert db.get_node("bob", node_id) is None


class TestEdgeCRUD:
    def test_insert_edge(self, db):
        edge_id = db.upsert_edge("alice", "nodeA", "nodeB", "related_to",
                                 "test edge", "src.md", "h", "orig.md")
        assert edge_id is not None

    def test_delete_edges_by_source(self, db):
        db.upsert_edge("alice", "A", "B", "rel", "s", "file.md", "h", "orig.md")
        db.upsert_edge("alice", "C", "D", "rel", "s", "file.md", "h", "orig.md")
        db.upsert_edge("alice", "E", "F", "rel", "s", "other.md", "h", "orig.md")
        db.delete_edges_by_source("alice", "file.md")

        cur = db.conn.execute("SELECT COUNT(*) FROM edges WHERE user_id='alice' AND source_path='file.md'")
        assert cur.fetchone()[0] == 0

    def test_user_isolation_edges(self, db):
        db.upsert_edge("alice", "A", "B", "rel", "alice edge", "src.md", "h", "orig.md")
        db.upsert_edge("bob", "A", "B", "rel", "bob edge", "src.md", "h", "orig.md")

        cur_a = db.conn.execute("SELECT * FROM edges WHERE user_id='alice'")
        cur_b = db.conn.execute("SELECT * FROM edges WHERE user_id='bob'")
        assert len(cur_a.fetchall()) == 1
        assert len(cur_b.fetchall()) == 1


class TestEventCRUD:
    def test_insert_event(self, db):
        event_id = db.upsert_event("alice", "2026-01-01", "deploy",
                                   "deployed yacmemo", "details",
                                   "src.md", "h", "orig.md")
        assert event_id is not None

    def test_get_events_by_source(self, db):
        db.upsert_event("alice", "2026-01-01", "type", "e1", "", "f1.md", "h", "orig.md")
        db.upsert_event("alice", "2026-01-02", "type", "e2", "", "f1.md", "h", "orig.md")
        db.upsert_event("alice", "2026-01-03", "type", "e3", "", "f2.md", "h", "orig.md")

        events = db.get_events_by_source("alice", "f1.md")
        assert len(events) == 2

    def test_delete_events_by_source(self, db):
        db.upsert_event("alice", "2026-01-01", "type", "e", "", "f.md", "h", "orig.md")
        db.delete_events_by_source("alice", "f.md")
        assert db.get_events_by_source("alice", "f.md") == []

    def test_user_isolation_events(self, db):
        db.upsert_event("alice", "2026-01-01", "type", "alice event", "",
                        "src.md", "h", "orig.md")
        db.upsert_event("bob", "2026-01-01", "type", "bob event", "",
                        "src.md", "h", "orig.md")

        alice_events = db.get_events_by_source("alice", "src.md")
        bob_events = db.get_events_by_source("bob", "src.md")
        assert len(alice_events) == 1
        assert len(bob_events) == 1
        assert alice_events[0]["summary"] == "alice event"


class TestProcessedFiles:
    def test_record_and_get(self, db):
        db.record_processed_file("alice", "file.md", "hash123", "split_dir", "success")
        pf = db.get_processed_file("alice", "file.md")
        assert pf is not None
        assert pf["content_hash"] == "hash123"
        assert pf["status"] == "success"

    def test_record_replaces(self, db):
        db.record_processed_file("alice", "file.md", "h1", "", "success")
        db.record_processed_file("alice", "file.md", "h2", "", "failed", "error msg")
        pf = db.get_processed_file("alice", "file.md")
        assert pf["content_hash"] == "h2"
        assert pf["status"] == "failed"

    def test_user_isolation(self, db):
        db.record_processed_file("alice", "file.md", "h1", "", "success")
        db.record_processed_file("bob", "file.md", "h2", "", "success")
        alice_pf = db.get_processed_file("alice", "file.md")
        bob_pf = db.get_processed_file("bob", "file.md")
        assert alice_pf["content_hash"] == "h1"
        assert bob_pf["content_hash"] == "h2"

    def test_split_file_count(self, db):
        db.record_processed_file("alice", "f.md", "h", "", "success",
                                 split_file_count=5)
        pf = db.get_processed_file("alice", "f.md")
        assert pf["split_file_count"] == 5


class TestConsistencyLog:
    def test_add_and_get_pending(self, db):
        db.add_consistency_log("alice", "old1", "new1",
                                         "old.md", "new.md",
                                         "contradiction", 0.9, False)
        pending = db.get_pending_consistency("alice")
        assert len(pending) == 1
        assert pending[0]["reason"] == "contradiction"

    def test_auto_invalidated_not_pending(self, db):
        db.add_consistency_log("alice", "old1", "new1", "old.md", "new.md",
                               "auto", 0.95, True)
        pending = db.get_pending_consistency("alice")
        assert len(pending) == 0

    def test_resolve_consistency(self, db):
        log_id = db.add_consistency_log("alice", "old1", "new1",
                                         "old.md", "new.md",
                                         "reason", 0.8, False)
        db.resolve_consistency("alice", log_id, "manual_confirmed")
        pending = db.get_pending_consistency("alice")
        assert len(pending) == 0

    def test_user_isolation(self, db):
        db.add_consistency_log("alice", "o", "n", "a.md", "b.md",
                               "alice reason", 0.9, False)
        db.add_consistency_log("bob", "o", "n", "a.md", "b.md",
                               "bob reason", 0.9, False)
        alice_pending = db.get_pending_consistency("alice")
        bob_pending = db.get_pending_consistency("bob")
        assert len(alice_pending) == 1
        assert len(bob_pending) == 1
        assert alice_pending[0]["reason"] == "alice reason"
