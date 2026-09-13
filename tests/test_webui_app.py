"""Tests for webui/app.py: WebUI JSON API + SPA serving with FastAPI TestClient."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from yacmemo.config import Config, EmbeddingConfig, LLMConfig, UserConfig, WebUIConfig
from yacmemo.db import MemoryDB
from yacmemo.webui.app import create_webui_app


@pytest.fixture
def webui_client(tmp_path):
    """Create a WebUI TestClient with temp DB and two users."""
    memory_root_a = tmp_path / "alice" / "memory"
    memory_root_a.mkdir(parents=True)
    memory_root_b = tmp_path / "bob" / "memory"
    memory_root_b.mkdir(parents=True)

    os.environ["YACMEMO_HOME"] = str(tmp_path)

    config = Config(
        llm=LLMConfig(
            base_url="http://localhost:11234/v1", api_key="sk-test", model="m"
        ),
        embedding=EmbeddingConfig(
            base_url="http://localhost:11235/v1",
            api_key="sk-test",
            model="m",
        ),
        storage={"sqlite_path": "data/system.db", "lancedb_path": "data/lancedb/"},
        webui=WebUIConfig(enabled=True, admin_token=""),
        users=[
            UserConfig(
                id="alice", display_name="Alice", memory_root=str(memory_root_a)
            ),
            UserConfig(
                id="bob", display_name="Bob", memory_root=str(memory_root_b)
            ),
        ],
    )

    db_path = str(tmp_path / "data" / "system.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    db = MemoryDB(db_path)
    config.users = config.sync_users_to_db(db)

    # Add some test data
    db.upsert_node(
        "alice", "EntityA", "concept", "summary A", "src.md", "h", "orig.md"
    )
    db.upsert_event(
        "alice", "2026-01-01", "test", "event", "", "src.md", "h", "orig.md"
    )

    app = create_webui_app(config, db)
    client = TestClient(app)

    yield client, config, db

    db.close()
    os.environ.pop("YACMEMO_HOME", None)


class TestUsersAPI:
    def test_list_users(self, webui_client):
        client, _, _ = webui_client
        resp = client.get("/api/users")
        assert resp.status_code == 200
        data = resp.json()
        assert "users" in data
        assert len(data["users"]) == 2
        ids = {u["id"] for u in data["users"]}
        assert ids == {"alice", "bob"}

    def test_create_user_json(self, webui_client):
        client, config, db = webui_client
        resp = client.post("/api/users", json={
            "id": "charlie",
            "display_name": "Charlie",
            "memory_root": "./data/charlie/memory",
        })
        assert resp.status_code == 200
        assert resp.json()["user"]["id"] == "charlie"
        assert db.get_user("charlie") is not None

    def test_create_duplicate_user(self, webui_client):
        client, _, _ = webui_client
        resp = client.post("/api/users", json={
            "id": "alice",
            "display_name": "Alice2",
            "memory_root": "./data/alice2/memory",
        })
        assert resp.status_code == 409

    def test_create_user_missing_id(self, webui_client):
        client, _, _ = webui_client
        resp = client.post("/api/users", json={
            "memory_root": "./data/x/memory",
        })
        assert resp.status_code == 400

    def test_create_user_missing_memory_root(self, webui_client):
        client, _, _ = webui_client
        resp = client.post("/api/users", json={
            "id": "testuser",
            "display_name": "Test",
        })
        assert resp.status_code == 400

    def test_delete_user(self, webui_client):
        client, config, db = webui_client
        resp = client.delete("/api/users/bob")
        assert resp.status_code == 200
        assert db.get_user("bob") is None

    def test_delete_nonexistent_user(self, webui_client):
        client, _, _ = webui_client
        resp = client.delete("/api/users/nonexistent")
        assert resp.status_code == 404


class TestMemoryAPI:
    def test_get_memory(self, webui_client):
        client, _, _ = webui_client
        resp = client.get("/api/memory/alice")
        assert resp.status_code == 200
        data = resp.json()
        assert "file_tree" in data
        assert "nodes" in data
        assert "events" in data
        assert len(data["nodes"]) >= 1

    def test_get_memory_not_found(self, webui_client):
        client, _, _ = webui_client
        resp = client.get("/api/memory/nonexistent")
        assert resp.status_code == 404

    def test_entity_history(self, webui_client):
        client, _, _ = webui_client
        resp = client.get("/api/memory/alice/history/EntityA")
        assert resp.status_code == 200
        data = resp.json()
        assert "history" in data
        assert len(data["history"]) >= 1

    def test_entity_history_not_found(self, webui_client):
        client, _, _ = webui_client
        resp = client.get("/api/memory/nonexistent/history/Foo")
        assert resp.status_code == 404


class TestConsistencyAPI:
    def test_get_consistency(self, webui_client):
        client, _, _ = webui_client
        resp = client.get("/api/consistency/alice")
        assert resp.status_code == 200
        data = resp.json()
        assert "pending" in data
        assert "auto_logs" in data
        assert "invalidated" in data

    def test_get_consistency_not_found(self, webui_client):
        client, _, _ = webui_client
        resp = client.get("/api/consistency/nonexistent")
        assert resp.status_code == 404


class TestStatusAPI:
    def test_status_endpoint(self, webui_client):
        client, _, _ = webui_client
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "llm_ok" in data
        assert "emb_ok" in data
        assert "user_indexes" in data
        assert "extract_cron" in data
        assert "consistency_cron" in data

    def test_health_endpoint(self, webui_client):
        client, _, _ = webui_client
        resp = client.get("/api/status/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
