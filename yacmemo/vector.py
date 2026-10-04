"""LanceDB vector store for yacmemo v2: note-level and observation-level vectors.

Same shape as v1's three tables, collapsed to two (note_vectors / obs_vectors).
Vectors are 1024-dim (Qwen3-Embedding-0.6B via omlx). Everything here is
rebuildable from markdown + vec_cache. All operations take an instance lock —
the HTTP server runs tools in a threadpool.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import threading

import lancedb
import pyarrow as pa
from lancedb.expr import col, lit

logger = logging.getLogger(__name__)

_VECTOR_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 1024)),
    pa.field("text", pa.string()),
    pa.field("source_path", pa.string()),
])

_TABLES = ("note_vectors", "obs_vectors")


def _log_safe(value: object) -> str:
    """日志用值：把换行折成字面量（可诊断性不丢，仍能看出原值）。

    笔记路径由用户/agent 决定，`notes/a\nERROR forged line.md` 这样一个
    路径就能在日志里伪造出第二行——伪造的 ERROR、伪造的处置痕迹都从这
    里来。异常消息同样可能回显路径，一起过这道。
    """
    return str(value).replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


def obs_id(path: str, text: str) -> str:
    """Stable id for an observation vector: content-addressed per note."""
    return hashlib.sha256(f"{path}\x00{text}".encode()).hexdigest()[:32]


class VectorStore:
    """LanceDB wrapper. `id` of a note vector IS its memory-root-relative path."""

    def __init__(self, lancedb_path: str, dimensions: int = 1024):
        self.db = lancedb.connect(lancedb_path)
        self.dimensions = dimensions
        self._lock = threading.Lock()
        for name in _TABLES:
            try:
                self.db.open_table(name)
            except Exception:
                self.db.create_table(name, schema=_VECTOR_SCHEMA)
                logger.info("Created LanceDB table: %s", name)

    def _upsert(self, table: str, item_id: str, text: str,
                embedding: list[float], source_path: str):
        with self._lock:
            tbl = self.db.open_table(table)
            # 谓词用 lancedb.expr 构造，不做字符串插值：id 是笔记路径 /
            # obs_id 派生值，含单引号就会拼出非法 SQL（实测 Bob's规则.md
            # → "Unterminated string literal"），而 delete 又是 best-effort
            # ——旧行不删、add 照跑，同 id 重复累积，RRF 逐条累加让重复
            # 路径得分虚高。反过来，形如 x' OR '1'='1 的 id 插值进去更是
            # 直接删表。expr 由库自己处理字面量，整类问题都不存在。
            try:
                tbl.delete(col("id") == lit(item_id))  # not present yet
            except Exception as e:
                # 吞掉仍要留痕：这里的失败正是旧行残留的根因，静默会让
                # 索引悄悄劣化（对照 delete_by_path 的同一处理）
                logger.warning("Vector pre-delete for %s in %s failed: %s",
                               _log_safe(item_id), table, _log_safe(e))
            tbl.add([{"id": item_id, "vector": embedding, "text": text,
                      "source_path": source_path}])

    def upsert_note_vector(self, path: str, text: str, embedding: list[float]):
        self._upsert("note_vectors", path, text, embedding, path)

    def upsert_obs_vector(self, path: str, text: str, embedding: list[float]):
        self._upsert("obs_vectors", obs_id(path, text), text, embedding, path)

    def search_note_vectors(self, embedding: list[float], limit: int = 10) -> list[dict]:
        with self._lock:
            tbl = self.db.open_table("note_vectors")
            return tbl.search(embedding).limit(limit).to_list()

    def search_obs_vectors(self, embedding: list[float], limit: int = 10) -> list[dict]:
        with self._lock:
            tbl = self.db.open_table("obs_vectors")
            return tbl.search(embedding).limit(limit).to_list()

    def delete_by_path(self, path: str):
        """Remove all vectors (note + observations) belonging to a note."""
        with self._lock:
            for name in _TABLES:
                try:
                    tbl = self.db.open_table(name)
                    tbl.delete(col("source_path") == lit(path))
                except Exception as e:
                    logger.warning("Delete from %s failed: %s", name, _log_safe(e))

    def wipe(self):
        """Drop and recreate both tables (full rebuild path)."""
        with self._lock:
            for name in _TABLES:
                with contextlib.suppress(Exception):
                    self.db.drop_table(name)
            for name in _TABLES:
                self.db.create_table(name, schema=_VECTOR_SCHEMA)
