"""Tests for fs_utils: hashing, path safety, split directory detection."""

from __future__ import annotations

import os

import pytest

from yacmemo.fs_utils import (
    content_hash,
    file_hash,
    get_split_dir,
    is_in_split_dir,
    list_md_files,
    safe_edit,
    safe_write,
    to_rel_path,
)


class TestContentHash:
    def test_deterministic(self):
        assert content_hash("hello") == content_hash("hello")

    def test_different_input_different_hash(self):
        assert content_hash("hello") != content_hash("world")

    def test_empty_string(self):
        assert isinstance(content_hash(""), str)
        assert len(content_hash("")) == 64


class TestFileHash:
    def test_matches_content(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        assert file_hash(str(f)) == content_hash("hello world")

    def test_different_files(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("aaa")
        f2.write_text("bbb")
        assert file_hash(str(f1)) != file_hash(str(f2))


class TestGetSplitDir:
    def test_strips_md_suffix(self, memory_root):
        md_path = os.path.join(memory_root, "notes", "01-test.md")
        expected = os.path.join(memory_root, "notes", "01-test")
        assert get_split_dir(md_path, memory_root) == expected

    def test_no_md_suffix(self, memory_root):
        md_path = os.path.join(memory_root, "notes", "readme")
        expected = os.path.join(memory_root, "notes", "readme")
        assert get_split_dir(md_path, memory_root) == expected


class TestIsInSplitDir:
    def test_in_split_dir(self, memory_root):
        # Create a .md file and its split dir
        md_path = os.path.join(memory_root, "notes", "01-test.md")
        split_dir = os.path.join(memory_root, "notes", "01-test")
        os.makedirs(split_dir)
        with open(md_path, "w") as f:
            f.write("content")

        split_file = os.path.join(split_dir, "split-item.md")
        assert is_in_split_dir(split_file, memory_root) is True

    def test_not_in_split_dir(self, memory_root):
        os.makedirs(os.path.join(memory_root, "notes"))
        md_path = os.path.join(memory_root, "notes", "01-test.md")
        with open(md_path, "w") as f:
            f.write("content")

        # A different .md file at the same level is NOT in a split dir
        other = os.path.join(memory_root, "notes", "02-other.md")
        assert is_in_split_dir(other, memory_root) is False

    def test_root_level_file(self, memory_root):
        md_path = os.path.join(memory_root, "root-level.md")
        with open(md_path, "w") as f:
            f.write("content")
        assert is_in_split_dir(md_path, memory_root) is False


class TestListMdFiles:
    def test_lists_all_md(self, memory_root):
        for name in ["a.md", "b.md", "sub/c.md"]:
            path = os.path.join(memory_root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("content")
        result = list_md_files(memory_root)
        assert len(result) == 3
        assert all(r.endswith(".md") for r in result)

    def test_excludes_hidden_dirs(self, memory_root):
        with open(os.path.join(memory_root, "visible.md"), "w") as f:
            f.write("content")
        os.makedirs(os.path.join(memory_root, ".hidden"))
        with open(os.path.join(memory_root, ".hidden", "secret.md"), "w") as f:
            f.write("content")
        result = list_md_files(memory_root)
        assert len(result) == 1
        assert "visible.md" in result[0]

    def test_excludes_split_dirs(self, memory_root):
        # Create source .md and split dir
        md_path = os.path.join(memory_root, "source.md")
        split_dir = os.path.join(memory_root, "source")
        os.makedirs(split_dir)
        with open(md_path, "w") as f:
            f.write("content")
        with open(os.path.join(split_dir, "split1.md"), "w") as f:
            f.write("split content")

        result = list_md_files(memory_root)
        assert len(result) == 1
        assert result[0].endswith("source.md")


class TestSafeWrite:
    def test_normal_write(self, memory_root):
        path = os.path.join(memory_root, "new.md")
        safe_write(path, "hello", memory_root)
        with open(path) as f:
            assert f.read() == "hello"

    def test_creates_parent_dirs(self, memory_root):
        path = os.path.join(memory_root, "sub", "dir", "file.md")
        safe_write(path, "content", memory_root)
        assert os.path.isfile(path)

    def test_blocks_path_traversal(self, memory_root, tmp_path):
        outside = str(tmp_path / "outside.md")
        with pytest.raises(ValueError, match="outside memory_root"):
            safe_write(outside, "content", memory_root)

    def test_blocks_split_dir(self, memory_root):
        md_path = os.path.join(memory_root, "source.md")
        split_dir = os.path.join(memory_root, "source")
        os.makedirs(split_dir)
        with open(md_path, "w") as f:
            f.write("content")

        split_file = os.path.join(split_dir, "split1.md")
        with pytest.raises(ValueError, match="split directory"):
            safe_write(split_file, "content", memory_root)

    def test_allow_split(self, memory_root):
        md_path = os.path.join(memory_root, "source.md")
        split_dir = os.path.join(memory_root, "source")
        os.makedirs(split_dir)
        with open(md_path, "w") as f:
            f.write("content")

        split_file = os.path.join(split_dir, "split1.md")
        safe_write(split_file, "content", memory_root, allow_split=True)
        assert os.path.isfile(split_file)


class TestSafeEdit:
    def test_normal_edit(self, memory_root):
        path = os.path.join(memory_root, "file.md")
        safe_write(path, "old text here", memory_root)
        safe_edit(path, "old text", "new text", memory_root)
        with open(path) as f:
            assert f.read() == "new text here"

    def test_blocks_path_traversal(self, memory_root, tmp_path):
        outside = str(tmp_path / "outside.md")
        with open(outside, "w") as f:
            f.write("content")
        with pytest.raises(ValueError, match="outside memory_root"):
            safe_edit(outside, "content", "new", memory_root)

    def test_old_string_not_found(self, memory_root):
        path = os.path.join(memory_root, "file.md")
        safe_write(path, "hello world", memory_root)
        with pytest.raises(ValueError, match="old_string not found"):
            safe_edit(path, "nonexistent", "replacement", memory_root)

    def test_replaces_only_first(self, memory_root):
        path = os.path.join(memory_root, "file.md")
        safe_write(path, "aaa aaa aaa", memory_root)
        safe_edit(path, "aaa", "bbb", memory_root)
        with open(path) as f:
            assert f.read() == "bbb aaa aaa"


class TestToRelPath:
    def test_convert(self, memory_root):
        abs_path = os.path.join(memory_root, "sub", "file.md")
        rel = to_rel_path(abs_path, memory_root)
        assert rel == os.path.join("sub", "file.md")

    def test_root_file(self, memory_root):
        abs_path = os.path.join(memory_root, "root.md")
        rel = to_rel_path(abs_path, memory_root)
        assert rel == "root.md"
