"""Shared test fixtures for yacmemo test suite."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from yacmemo.config import Config, ConsistencyConfig, EmbeddingConfig, LLMConfig, UserConfig
from yacmemo.db import MemoryDB

# ---- Path fixtures ----

@pytest.fixture
def tmp_dir(tmp_path):
    """Provide a clean temp directory."""
    return str(tmp_path)


@pytest.fixture
def memory_root(tmp_path):
    """Create a memory root directory with sample structure."""
    root = tmp_path / "memory"
    root.mkdir()
    return str(root)


# ---- Database fixtures ----

@pytest.fixture
def db(tmp_path):
    """Create a system-level MemoryDB with two test users pre-loaded."""
    db_path = str(tmp_path / "system.db")
    db = MemoryDB(db_path)

    db.add_user("alice", "Alice", str(tmp_path / "alice" / "memory"))
    db.add_user("bob", "Bob", str(tmp_path / "bob" / "memory"))

    yield db
    db.close()


# ---- Config fixtures ----

@pytest.fixture
def mock_config(tmp_path):
    """Build a Config object with temp paths, no TOML file needed."""
    memory_root = tmp_path / "memory"
    memory_root.mkdir()

    config = Config(
        llm=LLMConfig(
            base_url="http://localhost:11234/v1",
            api_key="sk-test",
            model="test-model",
        ),
        embedding=EmbeddingConfig(
            base_url="http://localhost:11235/v1",
            api_key="sk-test",
            model="test-embed",
        ),
        storage={"sqlite_path": "data/system.db", "lancedb_path": "data/lancedb/"},
        consistency=ConsistencyConfig(similarity_threshold=0.85, confidence_threshold=0.8, auto_invalidate=True),
        users=[
            UserConfig(id="alice", display_name="Alice", memory_root=str(memory_root)),
            UserConfig(id="bob", display_name="Bob", memory_root=str(tmp_path / "bob_memory")),
        ],
    )

    # Override paths to use tmp_path
    os.environ["YACMEMO_HOME"] = str(tmp_path)
    yield config
    os.environ.pop("YACMEMO_HOME", None)


@pytest.fixture
def config_with_db(mock_config, tmp_path):
    """Config + DB with users synced."""
    db_path = str(tmp_path / "data" / "system.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    db = MemoryDB(db_path)
    mock_config.users = mock_config.sync_users_to_db(db)
    yield mock_config, db
    db.close()


# ---- Mock LLM ----

@pytest.fixture
def mock_llm():
    """Mock LLMClient with configurable responses."""
    llm = MagicMock()
    llm.chat_json.return_value = {
        "files": [
            {
                "name": "test-split",
                "title": "测试拆分",
                "content": "这是测试内容。",
                "entities": [
                    {"name": "实体A", "type": "概念", "summary": "实体A描述"},
                    {"name": "实体B", "type": "工具", "summary": "实体B描述"},
                ],
                "events": [
                    {"date": "2026-01-01", "type": "部署", "description": "部署完成"},
                ],
            }
        ]
    }
    return llm


# ---- Mock Embedding ----

@pytest.fixture
def mock_embedding():
    """Mock EmbeddingClient that returns deterministic 1024-dim vectors."""
    emb = MagicMock()

    def _fake_embed_one(text: str) -> list[float]:
        # Deterministic pseudo-embedding based on text hash
        h = hash(text) & 0xFFFF
        return [((h >> (i % 16)) & 1) * 0.1 for i in range(1024)]

    emb.embed_one.side_effect = _fake_embed_one
    return emb


# ---- Sample .md content ----

@pytest.fixture
def sample_md_content():
    """Sample .md file content for extraction tests."""
    return """# yacmemo 部署记录

今天在 debsvc 上部署了 yacmemo 记忆层系统。
使用了 Ling-3.0-tiny 模型进行实体提取。
通过 mlx-serve 部署在 m2ultra 上。
"""
