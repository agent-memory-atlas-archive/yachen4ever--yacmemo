"""Filesystem utilities for memory-enhancer."""

from __future__ import annotations

import hashlib
import os


def content_hash(content: str) -> str:
    """SHA256 hash of string content."""
    return hashlib.sha256(content.encode()).hexdigest()


def file_hash(path: str) -> str:
    """SHA256 hash of file content."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def get_split_dir(md_path: str, memory_root: str) -> str:
    """Get the split directory path for a given .md file.

    Example: memory_root/resources/projects/01-数据门户.md
          -> memory_root/resources/projects/01-数据门户/
    """
    rel = os.path.relpath(md_path, memory_root)
    split_rel = rel[:-3] if rel.endswith(".md") else rel  # Remove .md suffix
    return os.path.join(memory_root, split_rel)


def is_in_split_dir(path: str, memory_root: str) -> bool:
    """Check if a path is inside a split directory.

    A split directory is a directory that has a sibling .md file with the same name.
    Example: .../01-数据门户/feat-a.md is in split dir if .../01-数据门户.md exists.
    """
    path = os.path.realpath(path)
    memory_root = os.path.realpath(memory_root)
    parent = os.path.dirname(path)
    os.path.basename(path)
    # Check if parent dir has a sibling .md file
    parent_name = os.path.basename(parent)
    sibling_md = os.path.join(os.path.dirname(parent), parent_name + ".md")
    return os.path.isfile(sibling_md)


def list_md_files(memory_root: str) -> list[str]:
    """Recursively list all .md files under memory_root, excluding split directories."""
    result = []
    memory_root = os.path.realpath(memory_root)

    for dirpath, dirnames, filenames in os.walk(memory_root):
        # Skip hidden directories (.git, .index, etc.)
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for f in filenames:
            if not f.endswith(".md"):
                continue

            full_path = os.path.join(dirpath, f)

            # Skip if this file is inside a split directory
            if is_in_split_dir(full_path, memory_root):
                continue

            result.append(full_path)

    return sorted(result)


def safe_write(path: str, content: str, memory_root: str, allow_split: bool = False):
    """Write file with path safety checks.

    - Resolves path to absolute
    - Prevents path traversal outside memory_root
    - Blocks writes to split directories unless allow_split=True
    """
    path = os.path.realpath(path)
    memory_root = os.path.realpath(memory_root)

    # Path traversal check
    if not path.startswith(memory_root):
        raise ValueError(f"Path outside memory_root: {path}")

    # Split directory check
    if not allow_split and is_in_split_dir(path, memory_root):
        raise ValueError(
            f"Cannot write to split directory (managed by memory-enhancer): {path}"
        )

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def safe_edit(path: str, old_string: str, new_string: str,
              memory_root: str, allow_split: bool = False):
    """Edit file with path safety checks. Same restrictions as safe_write."""
    path = os.path.realpath(path)
    memory_root = os.path.realpath(memory_root)

    if not path.startswith(memory_root):
        raise ValueError(f"Path outside memory_root: {path}")

    if not allow_split and is_in_split_dir(path, memory_root):
        raise ValueError(
            f"Cannot edit split directory file (managed by memory-enhancer): {path}"
        )

    with open(path, encoding="utf-8") as f:
        content = f.read()

    if old_string not in content:
        raise ValueError(f"old_string not found in {path}")

    new_content = content.replace(old_string, new_string, 1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)


def to_rel_path(abs_path: str, memory_root: str) -> str:
    """Convert absolute path to relative path (from memory_root)."""
    return os.path.relpath(abs_path, os.path.realpath(memory_root))


def list_split_files_in_dir(split_dir: str) -> list[str]:
    """List all .md files in a split directory (non-recursive).

    Excludes .conflict backup files and hidden files.
    """
    if not os.path.isdir(split_dir):
        return []
    result = []
    for f in sorted(os.listdir(split_dir)):
        if f.startswith("."):
            continue
        if ".conflict." in f:
            continue
        if f.endswith(".md"):
            result.append(os.path.join(split_dir, f))
    return result
