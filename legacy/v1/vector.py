"""LanceDB vector store for memory-enhancer."""

from __future__ import annotations

import contextlib
import logging

import lancedb
import pyarrow as pa

logger = logging.getLogger(__name__)

_NODE_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 1024)),
    pa.field("text", pa.string()),
    pa.field("source_path", pa.string()),
])

_EVENT_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 1024)),
    pa.field("text", pa.string()),
    pa.field("source_path", pa.string()),
])

_EDGE_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 1024)),
    pa.field("text", pa.string()),
    pa.field("source_path", pa.string()),
])


class VectorStore:
    """LanceDB wrapper for node/event/edge vectors."""

    def __init__(self, lancedb_path: str, dimensions: int = 1024):
        self.db = lancedb.connect(lancedb_path)
        self.dimensions = dimensions

        # Ensure tables exist
        for name, schema in [
            ("node_vectors", _NODE_SCHEMA),
            ("event_vectors", _EVENT_SCHEMA),
            ("edge_vectors", _EDGE_SCHEMA),
        ]:
            try:
                self.db.open_table(name)
            except Exception:
                self.db.create_table(name, schema=schema)
                logger.info("Created LanceDB table: %s", name)

    def _upsert(self, table_name: str, item_id: str, text: str,
                embedding: list[float], source_path: str, schema):
        tbl = self.db.open_table(table_name)
        # Delete existing record with same id (LanceDB has no native upsert)
        # Use single quotes for string literals (double quotes = column refs in LanceDB SQL)
        with contextlib.suppress(Exception):
            tbl.delete(f"id = '{item_id}'")  # Record doesn't exist yet
        tbl.add([{
            "id": item_id,
            "vector": embedding,
            "text": text,
            "source_path": source_path,
        }])
        logger.debug("Upserted vector %s into %s", item_id, table_name)

    def upsert_node_vector(self, node_id: str, text: str,
                           embedding: list[float], source_path: str):
        self._upsert("node_vectors", node_id, text, embedding, source_path, _NODE_SCHEMA)

    def upsert_event_vector(self, event_id: str, text: str,
                            embedding: list[float], source_path: str):
        self._upsert("event_vectors", event_id, text, embedding, source_path, _EVENT_SCHEMA)

    def upsert_edge_vector(self, edge_id: str, text: str,
                           embedding: list[float], source_path: str):
        self._upsert("edge_vectors", edge_id, text, embedding, source_path, _EDGE_SCHEMA)

    def search_nodes(self, query_embedding: list[float], limit: int = 10) -> list[dict]:
        """Search node vectors. Returns [{id, text, source_path, _distance}]."""
        tbl = self.db.open_table("node_vectors")
        results = tbl.search(query_embedding).limit(limit).to_list()
        return results

    def search_all(self, query_embedding: list[float], limit: int = 10) -> list[dict]:
        """Search across all vector tables, merge and sort by distance."""
        all_results = []
        for table_name, kind in [
            ("node_vectors", "node"),
            ("event_vectors", "event"),
            ("edge_vectors", "edge"),
        ]:
            try:
                tbl = self.db.open_table(table_name)
                results = tbl.search(query_embedding).limit(limit).to_list()
                for r in results:
                    r["kind"] = kind
                all_results.extend(results)
            except Exception as e:
                logger.warning("Search %s failed: %s", table_name, e)

        # Sort by distance (ascending = most similar first)
        all_results.sort(key=lambda x: x.get("_distance", float("inf")))
        return all_results[:limit]

    def delete_by_source(self, source_path: str):
        """Delete all vectors from a given split file (before re-extraction)."""
        for table_name in ["node_vectors", "event_vectors", "edge_vectors"]:
            try:
                tbl = self.db.open_table(table_name)
                tbl.delete(f"source_path = '{source_path}'")
            except Exception as e:
                logger.warning("Delete from %s failed: %s", table_name, e)
