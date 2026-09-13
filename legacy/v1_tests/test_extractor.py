"""Tests for extractor.py: Layer 2 extraction pipeline with mocked LLM + embedding."""

from __future__ import annotations

import os

import pytest

from yacmemo.extractor import Extractor
from yacmemo.vector import VectorStore


@pytest.fixture
def extractor_setup(tmp_path, mock_llm, mock_embedding):
    """Create an Extractor with mocked LLM/embedding and temp paths."""
    memory_root = str(tmp_path / "memory")
    os.makedirs(memory_root, exist_ok=True)

    lancedb_path = str(tmp_path / "lancedb")
    os.makedirs(lancedb_path, exist_ok=True)
    vector = VectorStore(lancedb_path, dimensions=1024)

    from yacmemo.db import MemoryDB
    db = MemoryDB(str(tmp_path / "system.db"))
    db.add_user("alice", "Alice", memory_root)

    # Build a config-like object with the attributes Extractor needs
    from dataclasses import dataclass

    @dataclass
    class TestLLMCfg:
        extract_max_tokens: int = 2048
        extract_timeout: int = 60

    @dataclass
    class TestConfig:
        memory_root_abs: str
        llm: TestLLMCfg

    config = TestConfig(memory_root_abs=memory_root, llm=TestLLMCfg())

    extractor = Extractor(config, db, vector, mock_llm, mock_embedding)

    yield extractor, db, vector, memory_root

    db.close()


class TestProcessFile:
    def test_successful_extraction(self, extractor_setup):
        extractor, db, vector, memory_root = extractor_setup

        md_path = os.path.join(memory_root, "test.md")
        with open(md_path, "w") as f:
            f.write("# Test\nSome content about yacmemo deployment.")

        result = extractor.process_file("alice", md_path)

        assert result.status == "success"
        assert result.split_count == 1
        assert result.entity_count == 2
        assert result.event_count == 1

        # Verify DB has the data
        nodes = db.get_all_valid_nodes("alice")
        assert len(nodes) == 2

        events = db.conn.execute("SELECT COUNT(*) FROM events WHERE user_id='alice'").fetchone()[0]
        assert events == 1

        # Verify processed_files
        pf = db.get_processed_file("alice", "test.md")
        assert pf is not None
        assert pf["status"] == "success"
        assert pf["split_file_count"] == 1

    def test_skip_already_processed(self, extractor_setup):
        extractor, db, vector, memory_root = extractor_setup

        md_path = os.path.join(memory_root, "test.md")
        with open(md_path, "w") as f:
            f.write("# Test\nContent that doesn't change.")

        # First extraction
        result1 = extractor.process_file("alice", md_path)
        assert result1.status == "success"

        # Second extraction with same content → skip
        result2 = extractor.process_file("alice", md_path)
        assert result2.status == "skipped"

    def test_re_extract_on_change(self, extractor_setup):
        extractor, db, vector, memory_root = extractor_setup

        md_path = os.path.join(memory_root, "test.md")
        with open(md_path, "w") as f:
            f.write("# Test\nOriginal content.")

        result1 = extractor.process_file("alice", md_path)
        assert result1.status == "success"

        # Change content
        with open(md_path, "w") as f:
            f.write("# Test\nModified content.")

        result2 = extractor.process_file("alice", md_path)
        assert result2.status == "incremental"

    def test_failed_extraction(self, extractor_setup):
        extractor, db, vector, memory_root = extractor_setup

        # Make LLM raise an error
        from yacmemo.llm import LLMJSONError
        extractor.llm.chat_json.side_effect = LLMJSONError("bad json", "raw")

        md_path = os.path.join(memory_root, "test.md")
        with open(md_path, "w") as f:
            f.write("# Test\nContent.")

        result = extractor.process_file("alice", md_path)
        assert result.status == "failed"
        assert len(result.errors) > 0

        # processed_files should record failure
        pf = db.get_processed_file("alice", "test.md")
        assert pf["status"] == "failed"

    def test_file_not_found(self, extractor_setup):
        extractor, db, vector, memory_root = extractor_setup

        result = extractor.process_file("alice", "/nonexistent/file.md")
        assert result.status == "failed"

    def test_empty_extraction(self, extractor_setup):
        extractor, db, vector, memory_root = extractor_setup

        # LLM returns no files
        extractor.llm.chat_json.return_value = {"files": []}

        md_path = os.path.join(memory_root, "empty.md")
        with open(md_path, "w") as f:
            f.write("# Empty\nNothing to extract.")

        result = extractor.process_file("alice", md_path)
        assert result.status == "success"
        assert result.split_count == 0
        assert result.entity_count == 0

    def test_writes_split_files(self, extractor_setup):
        extractor, db, vector, memory_root = extractor_setup

        md_path = os.path.join(memory_root, "source.md")
        with open(md_path, "w") as f:
            f.write("# Source\nContent for splitting.")

        extractor.process_file("alice", md_path)

        # Split dir should exist with split file
        split_dir = os.path.join(memory_root, "source")
        assert os.path.isdir(split_dir)
        assert os.path.isfile(os.path.join(split_dir, "test-split.md"))

    def test_user_isolation(self, extractor_setup):
        """Data extracted for alice should not be visible to bob."""
        extractor, db, vector, memory_root = extractor_setup

        db.add_user("bob", "Bob", memory_root)

        md_path = os.path.join(memory_root, "test.md")
        with open(md_path, "w") as f:
            f.write("# Test\nContent.")

        extractor.process_file("alice", md_path)

        alice_nodes = db.get_all_valid_nodes("alice")
        bob_nodes = db.get_all_valid_nodes("bob")
        assert len(alice_nodes) == 2
        assert len(bob_nodes) == 0
