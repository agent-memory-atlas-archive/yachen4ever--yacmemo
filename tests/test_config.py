"""Tests for config.py: TOML loading, user resolution, sync_users_to_db."""

from __future__ import annotations

import os

import pytest

from yacmemo.config import Config, EmbeddingConfig, LLMConfig, UserConfig, load_config
from yacmemo.db import MemoryDB


class TestLoadConfig:
    def test_load_from_file(self, tmp_path):
        toml_content = """
[llm]
base_url = "http://localhost:11234/v1"
api_key = "sk-test"
model = "test-model"
enable_thinking = false

[embedding]
base_url = "http://localhost:11235/v1"
api_key = "sk-test"
model = "test-embed"
dimensions = 1024

[storage]
sqlite_path = "data/system.db"
lancedb_path = "data/lancedb/"

[server]
host = "0.0.0.0"
port = 9721

[[users]]
id = "alice"
display_name = "Alice"
memory_root = "./data/alice/memory"

[[users]]
id = "bob"
display_name = "Bob"
memory_root = "./data/bob/memory"
llm_api_key = "sk-bob-key"
"""
        config_file = tmp_path / "config.toml"
        config_file.write_text(toml_content)

        os.environ["YACMEMO_HOME"] = str(tmp_path)
        config = load_config(str(config_file))
        os.environ.pop("YACMEMO_HOME", None)

        assert config.llm.base_url == "http://localhost:11234/v1"
        assert config.llm.api_key == "sk-test"
        assert config.llm.model == "test-model"
        assert config.llm.enable_thinking is False

        assert config.embedding.dimensions == 1024
        assert config.embedding.model == "test-embed"

        assert config.storage["sqlite_path"] == "data/system.db"

        assert len(config.users) == 2
        assert config.users[0].id == "alice"
        assert config.users[1].id == "bob"
        assert config.users[1].llm_api_key == "sk-bob-key"

    def test_get_user(self, mock_config):
        user = mock_config.get_user("alice")
        assert user.id == "alice"
        assert user.display_name == "Alice"

    def test_get_user_not_found(self, mock_config):
        with pytest.raises(ValueError, match="User not found"):
            mock_config.get_user("nonexistent")


class TestResolveLLM:
    def test_inherits_global(self, mock_config):
        user = mock_config.get_user("alice")
        llm = mock_config.resolve_llm(user)
        assert llm.api_key == "sk-test"  # global key
        assert llm.model == "test-model"

    def test_override_api_key(self, mock_config):
        user = UserConfig(id="bob", display_name="Bob",
                          memory_root="/tmp", llm_api_key="sk-custom")
        llm = mock_config.resolve_llm(user)
        assert llm.api_key == "sk-custom"
        assert llm.model == "test-model"  # still inherits model


class TestResolveEmbedding:
    def test_inherits_global(self, mock_config):
        user = mock_config.get_user("alice")
        emb = mock_config.resolve_embedding(user)
        assert emb.api_key == "sk-test"
        assert emb.model == "test-embed"

    def test_override_api_key(self, mock_config):
        user = UserConfig(id="bob", display_name="Bob",
                          memory_root="/tmp", embedding_api_key="sk-emb-custom")
        emb = mock_config.resolve_embedding(user)
        assert emb.api_key == "sk-emb-custom"


class TestPathResolution:
    def test_sqlite_abs(self, mock_config):
        path = mock_config.sqlite_abs
        assert path.endswith("data/system.db")
        assert os.path.isabs(path)

    def test_user_memory_root_abs(self, mock_config):
        user = mock_config.get_user("alice")
        path = mock_config.user_memory_root_abs(user)
        assert os.path.isabs(path)
        assert "alice" in path or "memory" in path

    def test_user_lancedb_abs(self, mock_config):
        user = mock_config.get_user("alice")
        path = mock_config.user_lancedb_abs(user)
        assert os.path.isabs(path)
        assert path.endswith("alice")  # data/lancedb/alice/

    def test_absolute_memory_root(self, tmp_path):
        abs_root = str(tmp_path / "abs_memory")
        os.makedirs(abs_root)
        config = Config(
            llm=LLMConfig(base_url="", api_key="", model=""),
            embedding=EmbeddingConfig(base_url="", api_key="", model=""),
            storage={},
            users=[UserConfig(id="test", memory_root=abs_root)],
        )
        user = config.get_user("test")
        assert config.user_memory_root_abs(user) == abs_root


class TestSyncUsersToDB:
    def test_sync_new_users(self, mock_config, tmp_path):
        db_path = str(tmp_path / "system.db")
        db = MemoryDB(db_path)

        users = mock_config.sync_users_to_db(db)
        assert len(users) == 2  # alice + bob
        assert db.get_user("alice") is not None
        assert db.get_user("bob") is not None
        db.close()

    def test_sync_updates_existing(self, mock_config, tmp_path):
        db_path = str(tmp_path / "system.db")
        db = MemoryDB(db_path)

        # First sync
        mock_config.sync_users_to_db(db)
        # Update alice's display_name in config
        mock_config.users[0].display_name = "Alice Updated"
        # Second sync
        mock_config.sync_users_to_db(db)
        alice = db.get_user("alice")
        assert alice["display_name"] == "Alice Updated"
        db.close()

    def test_sync_preserves_runtime_users(self, mock_config, tmp_path):
        db_path = str(tmp_path / "system.db")
        db = MemoryDB(db_path)

        # First sync (alice + bob)
        mock_config.sync_users_to_db(db)

        # Add a runtime user via DB
        db.add_user("runtime_user", "Runtime", "/tmp/runtime")

        # Second sync should preserve runtime_user
        users = mock_config.sync_users_to_db(db)
        ids = {u.id for u in users}
        assert "runtime_user" in ids
        assert "alice" in ids
        assert "bob" in ids
        db.close()
