"""Tests for split file protection: hash-based conflict detection and .conflict backup."""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from yacmemo.db import MemoryDB
from yacmemo.extractor import Extractor
from yacmemo.fs_utils import (
    list_split_files_in_dir,
)
from yacmemo.vector import VectorStore


@pytest.fixture
def extractor_setup(tmp_path, mock_llm, mock_embedding):
    """Create an Extractor with mocked LLM/embedding and temp paths."""
    memory_root = str(tmp_path / "memory")
    os.makedirs(memory_root, exist_ok=True)

    lancedb_path = str(tmp_path / "lancedb")
    os.makedirs(lancedb_path, exist_ok=True)
    vector = VectorStore(lancedb_path, dimensions=1024)

    db = MemoryDB(str(tmp_path / "system.db"))
    db.add_user("alice", "Alice", memory_root)

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


class TestSplitFilesTable:
    def test_record_and_get(self, db):
        db.record_split_file("alice", "dir/file.md", "hash123")
        rec = db.get_split_file("alice", "dir/file.md")
        assert rec is not None
        assert rec["content_hash"] == "hash123"
        assert rec["status"] == "active"
        assert rec["written_by"] == "extractor"

    def test_update_status(self, db):
        db.record_split_file("alice", "f.md", "h1")
        db.update_split_file_status("alice", "f.md", "user_edited")
        rec = db.get_split_file("alice", "f.md")
        assert rec["status"] == "user_edited"

    def test_list_by_status(self, db):
        db.record_split_file("alice", "a.md", "h1")
        db.record_split_file("alice", "b.md", "h2")
        db.update_split_file_status("alice", "b.md", "stale")
        active = db.list_split_files("alice", status="active")
        stale = db.list_split_files("alice", status="stale")
        assert len(active) == 1
        assert active[0]["path"] == "a.md"
        assert len(stale) == 1
        assert stale[0]["path"] == "b.md"

    def test_user_isolation(self, db):
        db.record_split_file("alice", "f.md", "h1")
        db.record_split_file("bob", "f.md", "h2")
        alice = db.get_split_file("alice", "f.md")
        bob = db.get_split_file("bob", "f.md")
        assert alice["content_hash"] == "h1"
        assert bob["content_hash"] == "h2"

    def test_delete(self, db):
        db.record_split_file("alice", "f.md", "h1")
        db.delete_split_file("alice", "f.md")
        assert db.get_split_file("alice", "f.md") is None


class TestListSplitFilesInDir:
    def test_lists_md_files(self, tmp_path):
        d = tmp_path / "split"
        d.mkdir()
        (d / "a.md").write_text("a")
        (d / "b.md").write_text("b")
        (d / ".hidden.md").write_text("hidden")
        result = list_split_files_in_dir(str(d))
        assert len(result) == 2

    def test_excludes_conflict_files(self, tmp_path):
        d = tmp_path / "split"
        d.mkdir()
        (d / "a.md").write_text("a")
        (d / "a.md.conflict.20260913.md").write_text("conflict")
        result = list_split_files_in_dir(str(d))
        assert len(result) == 1
        assert result[0].endswith("a.md")

    def test_nonexistent_dir(self):
        assert list_split_files_in_dir("/nonexistent") == []


class TestConflictDetection:
    def test_no_conflict_on_first_write(self, extractor_setup):
        """First extraction — no previous file, no conflict."""
        extractor, db, vector, memory_root = extractor_setup

        md_path = os.path.join(memory_root, "test.md")
        with open(md_path, "w") as f:
            f.write("# Test\nSome content.")

        result = extractor.process_file("alice", md_path)
        assert result.status == "success"

        # Split file should be recorded in split_files table
        split_dir = os.path.join(memory_root, "test")
        split_files = list_split_files_in_dir(split_dir)
        assert len(split_files) == 1

        rel_path = os.path.relpath(split_files[0], memory_root)
        rec = db.get_split_file("alice", rel_path)
        assert rec is not None
        assert rec["status"] == "active"

    def test_no_conflict_on_re_extraction_same_content(self, extractor_setup):
        """Re-extract with same original content — skipped, no conflict."""
        extractor, db, vector, memory_root = extractor_setup

        md_path = os.path.join(memory_root, "test.md")
        with open(md_path, "w") as f:
            f.write("# Test\nSome content.")

        # First extraction
        extractor.process_file("alice", md_path)

        # Second extraction — same content, should skip
        result = extractor.process_file("alice", md_path)
        assert result.status == "skipped"

    def test_conflict_on_user_edit(self, extractor_setup):
        """User manually edits a split file — next extraction preserves .conflict."""
        extractor, db, vector, memory_root = extractor_setup

        md_path = os.path.join(memory_root, "test.md")
        with open(md_path, "w") as f:
            f.write("# Test\nSome content.")

        # First extraction
        extractor.process_file("alice", md_path)

        # User manually edits the split file
        split_dir = os.path.join(memory_root, "test")
        split_file = os.path.join(split_dir, "test-split.md")
        assert os.path.isfile(split_file)

        with open(split_file, "w") as f:
            f.write("用户手动修改的内容，不同于提取管道生成的")

        # Now modify the original to trigger re-extraction
        with open(md_path, "w") as f:
            f.write("# Test\nModified content that will trigger re-extraction.")

        # Re-extract — should detect conflict and preserve .conflict file
        result = extractor.process_file("alice", md_path)
        assert result.status == "incremental"

        # .conflict file should exist
        conflict_files = [
            f for f in os.listdir(split_dir) if ".conflict." in f
        ]
        assert len(conflict_files) == 1, f"Expected 1 .conflict file, got {conflict_files}"

        # The split file should have new content (not user's edit)
        with open(split_file) as f:
            new_content = f.read()
        assert "用户手动修改" not in new_content

        # The .conflict file should have user's edit
        with open(os.path.join(split_dir, conflict_files[0])) as f:
            conflict_content = f.read()
        assert "用户手动修改" in conflict_content

    def test_no_conflict_on_re_extraction_without_user_edit(self, extractor_setup):
        """Re-extract after original change, but split file not manually edited — no conflict."""
        extractor, db, vector, memory_root = extractor_setup

        md_path = os.path.join(memory_root, "test.md")
        with open(md_path, "w") as f:
            f.write("# Test\nSome content.")

        # First extraction
        extractor.process_file("alice", md_path)

        # Modify original (not the split file)
        with open(md_path, "w") as f:
            f.write("# Test\nModified content for re-extraction.")

        # Re-extract
        extractor.process_file("alice", md_path)

        # No .conflict files should exist
        split_dir = os.path.join(memory_root, "test")
        conflict_files = [
            f for f in os.listdir(split_dir) if ".conflict." in f
        ]
        assert len(conflict_files) == 0


class TestWebhookProtection:
    """Test that webhook /trigger rejects split directory files."""

    def test_webhook_rejects_split_dir(self, tmp_path):
        """The is_in_split_dir check in webhook should reject split files."""
        from yacmemo.fs_utils import get_split_dir, is_in_split_dir

        memory_root = str(tmp_path / "memory")
        os.makedirs(memory_root)

        # Create source .md and split dir
        md_path = os.path.join(memory_root, "source.md")
        split_dir = get_split_dir(md_path, memory_root)
        os.makedirs(split_dir)
        with open(md_path, "w") as f:
            f.write("content")

        split_file = os.path.join(split_dir, "split1.md")
        with open(split_file, "w") as f:
            f.write("split content")

        assert is_in_split_dir(split_file, memory_root) is True
