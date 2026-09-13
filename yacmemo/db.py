"""SQLite storage layer for memory-enhancer."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex


_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    summary     TEXT,
    source_path TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    original_path TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    valid       INTEGER DEFAULT 1,
    invalid_at  TEXT,
    invalid_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_valid ON nodes(valid);
CREATE INDEX IF NOT EXISTS idx_nodes_source ON nodes(source_path);

CREATE TABLE IF NOT EXISTS edges (
    id            TEXT PRIMARY KEY,
    source_node   TEXT NOT NULL,
    target_node   TEXT NOT NULL,
    relation      TEXT NOT NULL,
    summary       TEXT,
    source_path   TEXT NOT NULL,
    source_hash   TEXT NOT NULL,
    original_path TEXT,
    valid_from    TEXT NOT NULL,
    invalid_at    TEXT,
    invalid_reason TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (source_node) REFERENCES nodes(id),
    FOREIGN KEY (target_node) REFERENCES nodes(id)
);
CREATE INDEX IF NOT EXISTS idx_edges_source_node ON edges(source_node);
CREATE INDEX IF NOT EXISTS idx_edges_target_node ON edges(target_node);

CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    date          TEXT,
    type          TEXT,
    summary       TEXT NOT NULL,
    details       TEXT,
    source_path   TEXT NOT NULL,
    source_hash   TEXT NOT NULL,
    original_path TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_path);

CREATE TABLE IF NOT EXISTS processed_files (
    path          TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    split_dir     TEXT,
    processed_at  TEXT NOT NULL,
    status        TEXT NOT NULL,
    error_msg     TEXT,
    split_file_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS consistency_log (
    id              TEXT PRIMARY KEY,
    old_node_id     TEXT,
    new_node_id     TEXT,
    old_source_path TEXT,
    new_source_path TEXT,
    reason          TEXT NOT NULL,
    confidence      REAL,
    checked_at      TEXT NOT NULL,
    auto_invalidated INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_consistency_status ON consistency_log(status);
"""


class MemoryDB:
    """SQLite wrapper for memory-enhancer."""

    def __init__(self, sqlite_path: str):
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(sqlite_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ---- Nodes ----

    def upsert_node(self, name: str, type_: str, summary: str,
                    source_path: str, source_hash: str, original_path: str) -> str:
        """Insert or update a node (same name+source_path = update)."""
        now = _now()
        cur = self.conn.execute(
            "SELECT id FROM nodes WHERE name=? AND source_path=? AND valid=1",
            (name, source_path),
        )
        row = cur.fetchone()
        if row:
            node_id = row["id"]
            self.conn.execute(
                """UPDATE nodes SET type=?, summary=?, source_hash=?, updated_at=?, valid=1,
                   invalid_at=NULL, invalid_reason=NULL WHERE id=?""",
                (type_, summary, source_hash, now, node_id),
            )
        else:
            node_id = _uuid()
            self.conn.execute(
                """INSERT INTO nodes (id, name, type, summary, source_path, source_hash,
                   original_path, created_at, updated_at, valid)
                   VALUES (?,?,?,?,?,?,?,?,?,1)""",
                (node_id, name, type_, summary, source_path, source_hash,
                 original_path, now, now),
            )
        self.conn.commit()
        return node_id

    def invalidate_node(self, node_id: str, reason: str):
        """Mark a node as invalid (superseded by a newer fact)."""
        self.conn.execute(
            "UPDATE nodes SET valid=0, invalid_at=?, invalid_reason=? WHERE id=?",
            (_now(), reason, node_id),
        )
        self.conn.commit()

    def get_node(self, node_id: str) -> dict | None:
        cur = self.conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_nodes_by_source(self, source_path: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM nodes WHERE source_path=? AND valid=1", (source_path,)
        )
        return [dict(r) for r in cur.fetchall()]

    def get_node_history(self, name: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM nodes WHERE name=? ORDER BY created_at", (name,)
        )
        return [dict(r) for r in cur.fetchall()]

    def get_all_valid_nodes(self) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM nodes WHERE valid=1")
        return [dict(r) for r in cur.fetchall()]

    def delete_nodes_by_source(self, source_path: str):
        """Delete all nodes from a given split file (before re-extraction)."""
        self.conn.execute("DELETE FROM nodes WHERE source_path=?", (source_path,))
        self.conn.commit()

    # ---- Edges ----

    def upsert_edge(self, source_node: str, target_node: str, relation: str,
                    summary: str, source_path: str, source_hash: str,
                    original_path: str) -> str:
        now = _now()
        edge_id = _uuid()
        self.conn.execute(
            """INSERT INTO edges (id, source_node, target_node, relation, summary,
               source_path, source_hash, original_path, valid_from, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (edge_id, source_node, target_node, relation, summary,
             source_path, source_hash, original_path, now, now),
        )
        self.conn.commit()
        return edge_id

    def delete_edges_by_source(self, source_path: str):
        self.conn.execute("DELETE FROM edges WHERE source_path=?", (source_path,))
        self.conn.commit()

    # ---- Events ----

    def upsert_event(self, date: str, type_: str, summary: str, details: str,
                     source_path: str, source_hash: str, original_path: str) -> str:
        event_id = _uuid()
        self.conn.execute(
            """INSERT INTO events (id, date, type, summary, details, source_path,
               source_hash, original_path, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (event_id, date, type_, summary, details, source_path,
             source_hash, original_path, _now()),
        )
        self.conn.commit()
        return event_id

    def get_events_by_source(self, source_path: str) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM events WHERE source_path=?", (source_path,))
        return [dict(r) for r in cur.fetchall()]

    def delete_events_by_source(self, source_path: str):
        self.conn.execute("DELETE FROM events WHERE source_path=?", (source_path,))
        self.conn.commit()

    # ---- Processed files ----

    def record_processed_file(self, path: str, content_hash: str, split_dir: str,
                              status: str, error_msg: str | None = None,
                              split_file_count: int = 0):
        now = _now()
        self.conn.execute(
            """INSERT OR REPLACE INTO processed_files
               (path, content_hash, split_dir, processed_at, status, error_msg, split_file_count)
               VALUES (?,?,?,?,?,?,?)""",
            (path, content_hash, split_dir, now, status, error_msg, split_file_count),
        )
        self.conn.commit()

    def get_processed_file(self, path: str) -> dict | None:
        cur = self.conn.execute("SELECT * FROM processed_files WHERE path=?", (path,))
        row = cur.fetchone()
        return dict(row) if row else None

    # ---- Consistency log ----

    def add_consistency_log(self, old_node_id: str, new_node_id: str,
                           old_source_path: str, new_source_path: str,
                           reason: str, confidence: float,
                           auto_invalidated: bool) -> str:
        log_id = _uuid()
        self.conn.execute(
            """INSERT INTO consistency_log
               (id, old_node_id, new_node_id, old_source_path, new_source_path,
                reason, confidence, checked_at, auto_invalidated, status)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (log_id, old_node_id, new_node_id, old_source_path, new_source_path,
             reason, confidence, _now(), 1 if auto_invalidated else 0,
             "auto_invalidated" if auto_invalidated else "pending"),
        )
        self.conn.commit()
        return log_id

    def get_pending_consistency(self) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM consistency_log WHERE status='pending' ORDER BY checked_at"
        )
        return [dict(r) for r in cur.fetchall()]

    def resolve_consistency(self, log_id: str, status: str):
        """Manually confirm or dismiss a pending consistency log entry."""
        self.conn.execute(
            "UPDATE consistency_log SET status=? WHERE id=?", (status, log_id)
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
