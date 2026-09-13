"""LanceDB vector store for yacmemo v2: note-level and observation-level vectors.

Same shape as v1's three tables, collapsed to two (note_vectors / obs_vectors).
Vectors are 1024-dim (Qwen3-Embedding-0.6B via omlx). Everything here is
rebuildable from markdown + vec_cache.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging

import lancedb
import pyarrow as pa

logger = logging.getLogger(__name__)

_VECTOR_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 1024)),
    pa.field("text", pa.string()),
    pa.field("source_path", pa.string()),
])

_TABLES = ("note_vectors", "obs_vectors")


def obs_id(path: str, text: str) -> str:
    """Stable id for an observation vector: content-addressed per note."""
    return hashlib.sha256(f"{path}\x00{text}".encode()).hexdigest()[:32]


class VectorStore:
    """LanceDB wrapper. `id` of a note vector IS its memory-root-relative path."""

    def __init__(self, lancedb_path: str, dimensions: int = 1024):
        self.db = lancedb.connect(lancedb_path)
        self.dimensions = dimensions
        for name in _TABLES:
            try:
                self.db.open_table(name)
            except Exception:
                self.db.create_table(name, schema=_VECTOR_SCHEMA)
                logger.info("Created LanceDB table: %s", name)

    def _upsert(self, table: str, item_id: str, text: str,
                embedding: list[float], source_path: str):
        tbl = self.db.open_table(table)
        with contextlib.suppress(Exception):
            tbl.delete(f"id = '{item_id}'")  # not present yet
        tbl.add([{"id": item_id, "vector": embedding, "text": text,
                  "source_path": source_path}])

    def upsert_note_vector(self, path: str, text: str, embedding: list[float]):
        self._upsert("note_vectors", path, text, embedding, path)

    def upsert_obs_vector(self, path: str, text: str, embedding: list[float]):
        self._upsert("obs_vectors", obs_id(path, text), text, embedding, path)

    def search_note_vectors(self, embedding: list[float], limit: int = 10) -> list[dict]:
        tbl = self.db.open_table("note_vectors")
        return tbl.search(embedding).limit(limit).to_list()

    def search_obs_vectors(self, embedding: list[float], limit: int = 10) -> list[dict]:
        tbl = self.db.open_table("obs_vectors")
        return tbl.search(embedding).limit(limit).to_list()

    def delete_by_path(self, path: str):
        """Remove all vectors (note + observations) belonging to a note."""
        escaped = path.replace("'", "''")
        for name in _TABLES:
            try:
                tbl = self.db.open_table(name)
                tbl.delete(f"source_path = '{escaped}'")
            except Exception as e:
                logger.warning("Delete from %s failed: %s", name, e)

    def wipe(self):
        """Drop and recreate both tables (full rebuild path)."""
        for name in _TABLES:
            with contextlib.suppress(Exception):
                self.db.drop_table(name)
        for name in _TABLES:
            self.db.create_table(name, schema=_VECTOR_SCHEMA)
