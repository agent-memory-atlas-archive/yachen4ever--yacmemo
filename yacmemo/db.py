"""SQLite storage layer for yacmemo — system-level database with user isolation.

All business tables (nodes, edges, events, etc.) carry a user_id column.
The users table stores user configuration; config.toml [[users]] is used
for initial import only, runtime reads come from the database.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _uuid() -> str:
    return uuid.uuid4().hex


_SCHEMA = """
-- Users table (system-level, manages all users)
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL DEFAULT '',
    memory_root     TEXT NOT NULL,
    llm_api_key     TEXT DEFAULT '',
    embedding_api_key TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Nodes (entities) — per-user isolation via user_id
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
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
    invalid_reason TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_nodes_user_name ON nodes(user_id, name);
CREATE INDEX IF NOT EXISTS idx_nodes_user_valid ON nodes(user_id, valid);
CREATE INDEX IF NOT EXISTS idx_nodes_source ON nodes(source_path);

-- Edges
CREATE TABLE IF NOT EXISTS edges (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
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
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_edges_user_source ON edges(user_id, source_node);
CREATE INDEX IF NOT EXISTS idx_edges_user_target ON edges(user_id, target_node);

-- Events
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    date          TEXT,
    type          TEXT,
    summary       TEXT NOT NULL,
    details       TEXT,
    source_path   TEXT NOT NULL,
    source_hash   TEXT NOT NULL,
    original_path TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_events_user_date ON events(user_id, date);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_path);

-- Processed files
CREATE TABLE IF NOT EXISTS processed_files (
    path          TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    split_dir     TEXT,
    processed_at  TEXT NOT NULL,
    status        TEXT NOT NULL,
    error_msg     TEXT,
    split_file_count INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, path),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Consistency log
CREATE TABLE IF NOT EXISTS consistency_log (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    old_node_id     TEXT,
    new_node_id     TEXT,
    old_source_path TEXT,
    new_source_path TEXT,
    reason          TEXT NOT NULL,
    confidence      REAL,
    checked_at      TEXT NOT NULL,
    auto_invalidated INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'pending',
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_consistency_user_status ON consistency_log(user_id, status);

-- Split files integrity tracking
CREATE TABLE IF NOT EXISTS split_files (
    user_id     TEXT NOT NULL,
    path        TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    written_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    status      TEXT DEFAULT 'active',
    PRIMARY KEY (user_id, path),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_split_files_user_status ON split_files(user_id, status);
"""


class MemoryDB:
    """System-level SQLite with user isolation."""

    def __init__(self, sqlite_path: str):
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(sqlite_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ---- Users (system-level) ----

    def add_user(self, id: str, display_name: str, memory_root: str,
                 llm_api_key: str = "", embedding_api_key: str = "") -> str:
        """Add a new user. Raises if user_id already exists."""
        now = _now()
        self.conn.execute(
            "INSERT INTO users (id, display_name, memory_root, llm_api_key, embedding_api_key, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (id, display_name, memory_root, llm_api_key, embedding_api_key, now, now),
        )
        self.conn.commit()
        return id

    def remove_user(self, user_id: str):
        """Delete a user and all their data."""
        with self.conn:
            self.conn.execute("DELETE FROM nodes WHERE user_id=?", (user_id,))
            self.conn.execute("DELETE FROM edges WHERE user_id=?", (user_id,))
            self.conn.execute("DELETE FROM events WHERE user_id=?", (user_id,))
            self.conn.execute("DELETE FROM processed_files WHERE user_id=?", (user_id,))
            self.conn.execute("DELETE FROM consistency_log WHERE user_id=?", (user_id,))
            self.conn.execute("DELETE FROM users WHERE id=?", (user_id,))

    def get_user(self, user_id: str) -> dict | None:
        cur = self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def list_users(self) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM users ORDER BY created_at")
        return [dict(r) for r in cur.fetchall()]

    def update_user(self, user_id: str, **fields):
        """Update user fields.

        Allowed: display_name, memory_root, llm_api_key, embedding_api_key.
        """
        allowed = {"display_name", "memory_root", "llm_api_key", "embedding_api_key"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        sets = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [_now(), user_id]
        self.conn.execute(f"UPDATE users SET {sets}, updated_at=? WHERE id=?", vals)
        self.conn.commit()

    # ---- Nodes (per-user) ----

    def upsert_node(self, user_id: str, name: str, type_: str, summary: str,
                    source_path: str, source_hash: str, original_path: str) -> str:
        now = _now()
        cur = self.conn.execute(
            "SELECT id FROM nodes WHERE user_id=? AND name=? AND source_path=? AND valid=1",
            (user_id, name, source_path),
        )
        row = cur.fetchone()
        if row:
            node_id = row["id"]
            self.conn.execute(
                "UPDATE nodes SET type=?, summary=?, source_hash=?, updated_at=?, valid=1, "
                "invalid_at=NULL, invalid_reason=NULL WHERE id=?",
                (type_, summary, source_hash, now, node_id),
            )
        else:
            node_id = _uuid()
            self.conn.execute(
                "INSERT INTO nodes (id, user_id, name, type, summary, source_path, source_hash, "
                "original_path, created_at, updated_at, valid) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (node_id, user_id, name, type_, summary, source_path, source_hash,
                 original_path, now, now, 1),
            )
        self.conn.commit()
        return node_id

    def invalidate_node(self, user_id: str, node_id: str, reason: str):
        self.conn.execute(
            "UPDATE nodes SET valid=0, invalid_at=?, invalid_reason=? WHERE id=? AND user_id=?",
            (_now(), reason, node_id, user_id),
        )
        self.conn.commit()

    def get_node(self, user_id: str, node_id: str) -> dict | None:
        cur = self.conn.execute("SELECT * FROM nodes WHERE id=? AND user_id=?", (node_id, user_id))
        row = cur.fetchone()
        return dict(row) if row else None

    def get_nodes_by_source(self, user_id: str, source_path: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM nodes WHERE user_id=? AND source_path=? AND valid=1",
            (user_id, source_path),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_node_history(self, user_id: str, name: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM nodes WHERE user_id=? AND name=? ORDER BY created_at",
            (user_id, name),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_all_valid_nodes(self, user_id: str) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM nodes WHERE user_id=? AND valid=1", (user_id,))
        return [dict(r) for r in cur.fetchall()]

    def delete_nodes_by_source(self, user_id: str, source_path: str):
        self.conn.execute("DELETE FROM nodes WHERE user_id=? AND source_path=?",
                          (user_id, source_path))
        self.conn.commit()

    # ---- Edges ----

    def upsert_edge(self, user_id: str, source_node: str, target_node: str,
                    relation: str, summary: str, source_path: str, source_hash: str,
                    original_path: str) -> str:
        edge_id = _uuid()
        self.conn.execute(
            "INSERT INTO edges (id, user_id, source_node, target_node, relation, summary, "
            "source_path, source_hash, original_path, valid_from, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (edge_id, user_id, source_node, target_node, relation, summary,
             source_path, source_hash, original_path, _now(), _now()),
        )
        self.conn.commit()
        return edge_id

    def delete_edges_by_source(self, user_id: str, source_path: str):
        self.conn.execute("DELETE FROM edges WHERE user_id=? AND source_path=?",
                          (user_id, source_path))
        self.conn.commit()

    # ---- Events ----

    def upsert_event(self, user_id: str, date: str, type_: str, summary: str,
                     details: str, source_path: str, source_hash: str,
                     original_path: str) -> str:
        event_id = _uuid()
        self.conn.execute(
            "INSERT INTO events (id, user_id, date, type, summary, details, source_path, "
            "source_hash, original_path, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (event_id, user_id, date, type_, summary, details, source_path,
             source_hash, original_path, _now()),
        )
        self.conn.commit()
        return event_id

    def get_events_by_source(self, user_id: str, source_path: str) -> list[dict]:
        cur = self.conn.execute("SELECT * FROM events WHERE user_id=? AND source_path=?",
                                (user_id, source_path))
        return [dict(r) for r in cur.fetchall()]

    def delete_events_by_source(self, user_id: str, source_path: str):
        self.conn.execute("DELETE FROM events WHERE user_id=? AND source_path=?",
                          (user_id, source_path))
        self.conn.commit()

    # ---- Processed files ----

    def record_processed_file(self, user_id: str, path: str, content_hash: str,
                              split_dir: str, status: str, error_msg: str | None = None,
                              split_file_count: int = 0):
        self.conn.execute(
            "INSERT OR REPLACE INTO processed_files (user_id, path, content_hash, split_dir, "
            "processed_at, status, error_msg, split_file_count) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, path, content_hash, split_dir, _now(), status, error_msg, split_file_count),
        )
        self.conn.commit()

    def get_processed_file(self, user_id: str, path: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT * FROM processed_files WHERE user_id=? AND path=?", (user_id, path))
        row = cur.fetchone()
        return dict(row) if row else None

    # ---- Consistency log ----

    def add_consistency_log(self, user_id: str, old_node_id: str, new_node_id: str,
                           old_source_path: str, new_source_path: str,
                           reason: str, confidence: float,
                           auto_invalidated: bool) -> str:
        log_id = _uuid()
        self.conn.execute(
            "INSERT INTO consistency_log (id, user_id, old_node_id, new_node_id, old_source_path, "
            "new_source_path, reason, confidence, checked_at, auto_invalidated, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (log_id, user_id, old_node_id, new_node_id, old_source_path, new_source_path,
             reason, confidence, _now(), 1 if auto_invalidated else 0,
             "auto_invalidated" if auto_invalidated else "pending"),
        )
        self.conn.commit()
        return log_id

    def get_pending_consistency(self, user_id: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT * FROM consistency_log WHERE user_id=? AND status='pending' "
            "ORDER BY checked_at", (user_id,))
        return [dict(r) for r in cur.fetchall()]

    def resolve_consistency(self, user_id: str, log_id: str, status: str):
        self.conn.execute(
            "UPDATE consistency_log SET status=? WHERE id=? AND user_id=?",
            (status, log_id, user_id))
        self.conn.commit()

    # ---- Split files integrity ----

    def record_split_file(self, user_id: str, path: str, content_hash: str,
                          written_by: str = "extractor"):
        """Record or update a split file's hash after writing it."""
        now = _now()
        self.conn.execute(
            "INSERT INTO split_files (user_id, path, content_hash, written_by, "
            "created_at, updated_at, status) VALUES (?,?,?,?,?,?, 'active') "
            "ON CONFLICT(user_id, path) DO UPDATE SET "
            "content_hash=excluded.content_hash, written_by=excluded.written_by, "
            "updated_at=excluded.updated_at, status='active'",
            (user_id, path, content_hash, written_by, now, now),
        )
        self.conn.commit()

    def get_split_file(self, user_id: str, path: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT * FROM split_files WHERE user_id=? AND path=?",
            (user_id, path),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def list_split_files(self, user_id: str, status: str | None = None) -> list[dict]:
        if status:
            cur = self.conn.execute(
                "SELECT * FROM split_files WHERE user_id=? AND status=? ORDER BY path",
                (user_id, status),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM split_files WHERE user_id=? ORDER BY path",
                (user_id,),
            )
        return [dict(r) for r in cur.fetchall()]

    def update_split_file_status(self, user_id: str, path: str, status: str):
        self.conn.execute(
            "UPDATE split_files SET status=?, updated_at=? "
            "WHERE user_id=? AND path=?",
            (status, _now(), user_id, path),
        )
        self.conn.commit()

    def delete_split_file(self, user_id: str, path: str):
        self.conn.execute(
            "DELETE FROM split_files WHERE user_id=? AND path=?",
            (user_id, path),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
