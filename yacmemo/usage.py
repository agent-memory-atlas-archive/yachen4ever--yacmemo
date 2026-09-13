"""Usage logging for yacmemo server: who called what, when, with what result.

One server-level SQLite (default `<data_dir>/usage.db`) shared by all users.
Written by the shared tool wrappers on every MCP tool call (both transports);
read by the WebUI's usage/health pages. Trimmed to the most recent rows so it
never grows unbounded.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS call_log (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    client      TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT '',
    tool        TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 1,
    error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_call_ts ON call_log(ts);
CREATE INDEX IF NOT EXISTS idx_call_user_tool ON call_log(user_id, tool);
"""

_MAX_ROWS = 20000


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class UsageDB:
    """Tiny append-mostly log. All methods take an RLock (HTTP threadpool)."""

    def __init__(self, db_path: str):
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def log_call(self, user_id: str, tool: str, summary: str = "",
                 duration_ms: int = 0, ok: bool = True, error: str = "",
                 client: str = "", ip: str = ""):
        with self._lock:
            self.conn.execute(
                "INSERT INTO call_log (id, ts, user_id, client, ip, tool, summary, "
                "duration_ms, ok, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, _now(), user_id, client[:120], ip, tool,
                 summary[:200], duration_ms, 1 if ok else 0, error[:200]),
            )
            self._trim()
            self.conn.commit()

    def _trim(self):
        self.conn.execute(
            "DELETE FROM call_log WHERE id NOT IN "
            "(SELECT id FROM call_log ORDER BY ts DESC LIMIT ?)", (_MAX_ROWS,)
        )

    def recent(self, limit: int = 100, user_id: str | None = None,
               tool: str | None = None) -> list[dict]:
        sql = "SELECT * FROM call_log WHERE 1=1"
        params: list = []
        if user_id:
            sql += " AND user_id=?"
            params.append(user_id)
        if tool:
            sql += " AND tool=?"
            params.append(tool)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def client_summary(self) -> list[dict]:
        """Distinct clients by user-agent prefix + ip, with call counts."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT client, ip, COUNT(*) AS calls, MAX(ts) AS last_seen "
                "FROM call_log GROUP BY client, ip ORDER BY calls DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]

    def day_counts(self, days: int = 7) -> list[dict]:
        cutoff = (datetime.now(UTC)).isoformat(timespec="seconds")[:10]
        with self._lock:
            rows = self.conn.execute(
                "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS calls, "
                "SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) AS errors "
                "FROM call_log WHERE substr(ts,1,10) >= ? "
                "GROUP BY day ORDER BY day DESC LIMIT ?",
                (cutoff, days),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()


def summarize_args(tool: str, kwargs: dict) -> str:
    """Human-readable one-liner of a tool call's arguments (for the log/UI)."""
    if tool == "memory_write":
        return f"title={kwargs.get('title', '')} force={kwargs.get('force', False)}"
    if tool in ("memory_edit", "memory_edit_section"):
        return f"path={kwargs.get('path', '')}"
    if tool == "memory_move":
        return f"{kwargs.get('path', '')} → {kwargs.get('new_path', '')}"
    if tool == "memory_read":
        return f"path={kwargs.get('path_or_title', '')}"
    if tool == "memory_search":
        q = kwargs.get("query", "")
        return f"query={q[:80]}"
    if tool == "memory_list":
        return f"path={kwargs.get('path', '')}"
    return ""
