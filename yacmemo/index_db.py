"""SQLite index for yacmemo v2 — fully derived from markdown, rebuildable at any time.

Tables:
- notes:        one row per markdown note (path, title, content_hash, updated_at)
- fts:          FTS5 trigram index over title+body (Chinese-friendly substring search)
- collisions:   cross-note ambiguity candidates (D2 observation collisions; D1 audit output)
- guard_events: write-guard refusals and force bypasses — the violation-rate metric source
- vec_cache:    content-hash keyed embedding cache (survives note churn)

Schema deviation from docs/06: `guard_events` was added beyond the 4 documented tables
to make force-usage countable (P4 metric). Doc updated in P2 polish.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import UTC, datetime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    path         TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    title, body, path UNINDEXED, tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS collisions (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,             -- obs / title
    a_path      TEXT NOT NULL,
    b_path      TEXT NOT NULL,
    a_text      TEXT NOT NULL DEFAULT '',
    b_text      TEXT NOT NULL DEFAULT '',
    score       REAL NOT NULL DEFAULT 0,
    detected_at TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'  -- open / resolved / dismissed
);
CREATE TABLE IF NOT EXISTS guard_events (
    id              TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- refused / forced
    attempted_title TEXT NOT NULL,
    matched_path    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS vec_cache (
    content_hash TEXT PRIMARY KEY,
    vector       BLOB NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _uuid() -> str:
    return uuid.uuid4().hex


class IndexDB:
    """Thin sqlite wrapper. All paths are memory_root-relative posix paths."""

    def __init__(self, db_path: str):
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ---- notes ----

    def upsert_note(self, path: str, title: str, content_hash: str):
        self.conn.execute(
            "INSERT INTO notes (path, title, content_hash, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET title=excluded.title, "
            "content_hash=excluded.content_hash, updated_at=excluded.updated_at",
            (path, title, content_hash, _now()),
        )
        self.conn.commit()

    def remove_note(self, path: str):
        self.conn.execute("DELETE FROM notes WHERE path=?", (path,))
        self.fts_remove(path)
        self.conn.commit()

    def get_note(self, path: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM notes WHERE path=?", (path,)).fetchone()
        return dict(row) if row else None

    def get_note_by_title(self, title: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM notes WHERE title=?", (title,)).fetchone()
        return dict(row) if row else None

    def all_titles(self) -> list[dict]:
        rows = self.conn.execute("SELECT path, title FROM notes").fetchall()
        return [dict(r) for r in rows]

    def list_notes(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM notes ORDER BY path").fetchall()
        return [dict(r) for r in rows]

    def move_note(self, old_path: str, new_path: str):
        """Update path in notes and fts (content/title unchanged)."""
        self.conn.execute("UPDATE notes SET path=?, updated_at=? WHERE path=?",
                          (new_path, _now(), old_path))
        self.fts_move(old_path, new_path)
        self.conn.commit()

    def clear_all(self):
        """Wipe derived content state (keep guard_events history and vec_cache)."""
        self.conn.execute("DELETE FROM notes")
        self.conn.execute("DELETE FROM fts")
        self.conn.execute("DELETE FROM collisions")
        self.conn.commit()

    # ---- fts ----

    def fts_replace(self, path: str, title: str, body: str):
        self.fts_remove(path)
        self.conn.execute("INSERT INTO fts (title, body, path) VALUES (?,?,?)",
                          (title, body, path))
        self.conn.commit()

    def fts_remove(self, path: str):
        self.conn.execute("DELETE FROM fts WHERE path=?", (path,))

    def fts_move(self, old_path: str, new_path: str):
        self.conn.execute("UPDATE fts SET path=? WHERE path=?", (new_path, old_path))

    @staticmethod
    def fts_query_expr(query: str) -> str | None:
        """Build an FTS5 MATCH expression (ANDed quoted phrases).

        Trigram tokenizer can only match tokens of >= 3 characters; shorter
        tokens are dropped (the vector channel covers short queries).
        Returns None when nothing searchable remains.
        """
        parts = []
        for tok in query.split():
            tok = tok.strip()
            if len(tok) >= 3:
                parts.append('"' + tok.replace('"', '""') + '"')
        return " ".join(parts) if parts else None

    def fts_search(self, query: str, limit: int = 10) -> list[dict]:
        expr = self.fts_query_expr(query)
        if not expr:
            return []
        rows = self.conn.execute(
            "SELECT path, title, bm25(fts) AS bm25 FROM fts WHERE fts MATCH ? "
            "ORDER BY bm25 LIMIT ?",
            (expr, limit),
        ).fetchall()
        results = []
        for i, r in enumerate(rows):
            results.append({"path": r["path"], "title": r["title"], "rank": i + 1,
                            "bm25": r["bm25"]})
        return results

    # ---- guard events ----

    def add_guard_event(self, kind: str, attempted_title: str, matched_path: str,
                        forced: bool):
        self.conn.execute(
            "INSERT INTO guard_events (id, ts, kind, attempted_title, matched_path) "
            "VALUES (?,?,?,?,?)",
            (_uuid(), _now(), "forced" if forced else "refused",
             attempted_title, matched_path),
        )
        self.conn.commit()

    def guard_stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM guard_events GROUP BY kind"
        ).fetchall()
        stats = {r["kind"]: r["n"] for r in rows}
        return {"refused": stats.get("refused", 0),
                "forced": stats.get("forced", 0)}

    def count_forced_since(self, hours: int = 24) -> int:
        """Forced bypasses within a rolling window (force-confirmation ladder)."""
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(
            timespec="seconds")
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM guard_events WHERE kind='forced' AND ts >= ?",
            (cutoff,),
        ).fetchone()
        return row["n"]

    # ---- collisions ----

    def add_collision(self, kind: str, a_path: str, b_path: str,
                      a_text: str = "", b_text: str = "", score: float = 0.0):
        self.conn.execute(
            "INSERT INTO collisions (id, kind, a_path, b_path, a_text, b_text, score, "
            "detected_at, status) VALUES (?,?,?,?,?,?,?,?, 'open')",
            (_uuid(), kind, a_path, b_path, a_text, b_text, score, _now()),
        )
        self.conn.commit()

    def collisions_for(self, path: str, status: str = "open") -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM collisions WHERE status=? AND (a_path=? OR b_path=?)",
            (status, path, path),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_collisions(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM collisions WHERE status=? ORDER BY score DESC", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM collisions ORDER BY score DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def remove_collisions_involving(self, path: str):
        self.conn.execute(
            "DELETE FROM collisions WHERE a_path=? OR b_path=?", (path, path)
        )
        self.conn.commit()

    def prune_stale_collisions(self) -> int:
        """Drop open collisions whose notes no longer exist (deleted or never re-found)."""
        rows = self.conn.execute(
            "SELECT c.id FROM collisions c LEFT JOIN notes a ON c.a_path=a.path "
            "LEFT JOIN notes b ON c.b_path=b.path "
            "WHERE c.status='open' AND (a.path IS NULL OR b.path IS NULL)"
        ).fetchall()
        for r in rows:
            self.conn.execute("DELETE FROM collisions WHERE id=?", (r["id"],))
        self.conn.commit()
        return len(rows)

    # ---- vector cache ----

    def get_cached_vector(self, content_hash: str) -> list[float] | None:
        import numpy as np

        row = self.conn.execute(
            "SELECT vector FROM vec_cache WHERE content_hash=?", (content_hash,)
        ).fetchone()
        if not row:
            return None
        return np.frombuffer(row["vector"], dtype=np.float32).tolist()

    def put_cached_vector(self, content_hash: str, vector: list[float]):
        import numpy as np

        blob = np.asarray(vector, dtype=np.float32).tobytes()
        self.conn.execute(
            "INSERT OR REPLACE INTO vec_cache (content_hash, vector) VALUES (?,?)",
            (content_hash, blob),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
