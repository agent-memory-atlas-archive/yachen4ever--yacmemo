"""Hash helpers (carried over from v1 fs_utils; the rest of v1 CRUD was retired)."""

from __future__ import annotations

import hashlib


def content_hash(content: str) -> str:
    """SHA256 hash of string content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def file_hash(path: str) -> str:
    """SHA256 hash of file content."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()
