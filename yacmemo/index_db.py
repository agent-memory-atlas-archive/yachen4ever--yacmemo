"""SQLite index for yacmemo v2 — fully derived from markdown, rebuildable at any time.

Tables:
- notes:        one row per markdown note (path, title, content_hash, updated_at)
- fts:          FTS5 trigram index over title+body (Chinese-friendly substring search)
- collisions:   cross-note ambiguity candidates (D2 observation collisions; D1 audit output)
- guard_events: write-guard refusals and force bypasses — the violation-rate metric source
- vec_cache:    content-hash keyed embedding cache (survives note churn)

Thread safety: the HTTP server runs sync MCP tools in a threadpool, so every
public method takes the instance lock (RLock — reentrant, methods call each
other). The connection is shared across threads with check_same_thread=False.
"""

from __future__ import annotations

import functools
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    path         TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    vector_ok    INTEGER NOT NULL DEFAULT 0
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
CREATE TABLE IF NOT EXISTS audit_actions (
    id       TEXT PRIMARY KEY,  -- D1:{a}|{b} / D2:{collision_id} / D3:{path}|{link} / D4:{path}
    kind     TEXT NOT NULL,                -- D1 / D2 / D3 / D4
    a_path   TEXT NOT NULL DEFAULT '',
    b_path   TEXT NOT NULL DEFAULT '',
    action   TEXT NOT NULL,                -- resolved / dismissed
    note     TEXT NOT NULL DEFAULT '',
    acted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_exec_events (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,           -- D1:..../D5:.... / P:<file>:<index>
    kind     TEXT NOT NULL,           -- D1 / D2 / ... / P
    event    TEXT NOT NULL,           -- executing / progress / executed / blocked
    note     TEXT NOT NULL DEFAULT '',
    identity TEXT NOT NULL DEFAULT '',-- 汇报方（agent+设备，匿名留空）
    ts       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exec_events_issue
    ON audit_exec_events(issue_id, seq);
CREATE TABLE IF NOT EXISTS vec_cache (
    content_hash TEXT PRIMARY KEY,
    vector       BLOB NOT NULL
);
"""

# Methods that must not interleave across threads (everything touching conn).
_LOCKED_METHODS = (
    "upsert_note", "remove_note", "get_note", "get_note_by_title", "all_titles",
    "list_notes", "move_note", "clear_all",
    "fts_replace", "fts_remove", "fts_move", "fts_search",
    "add_guard_event", "guard_stats", "count_forced_since",
    "add_collision", "collisions_for", "list_collisions",
    "remove_collisions_involving", "prune_stale_collisions",
    "record_audit_action", "list_audit_actions",
    "add_exec_event", "list_exec_events", "exec_last_status", "fts_body",
    "get_cached_vector", "put_cached_vector", "close",
)


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
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        # 存量库迁移：vector_ok 列（2026-09-18 增补，缺向量审计点名/自愈用）
        cols = {r["name"] for r in self.conn.execute(
            "PRAGMA table_info(notes)").fetchall()}
        if "vector_ok" not in cols:
            self.conn.execute(
                "ALTER TABLE notes ADD COLUMN vector_ok INTEGER NOT NULL DEFAULT 0")
        self.conn.commit()
        for name in _LOCKED_METHODS:
            fn = getattr(self, name)
            wrapped = lambda *a, _fn=fn, **kw: self._call(_fn, *a, **kw)  # noqa: E731
            setattr(self, name, functools.wraps(fn)(wrapped))

    def _call(self, fn, *args, **kwargs):
        with self._lock:
            return fn(*args, **kwargs)

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

    def fts_body(self, path: str) -> str | None:
        """Indexed FTS body for a path (kept in sync with disk by save/resync)."""
        row = self.conn.execute("SELECT body FROM fts WHERE path=?", (path,)).fetchone()
        return row["body"] if row else None

    def all_titles(self) -> list[dict]:
        rows = self.conn.execute("SELECT path, title FROM notes").fetchall()
        return [dict(r) for r in rows]

    def list_notes(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM notes ORDER BY path").fetchall()
        return [dict(r) for r in rows]

    def set_vector_ok(self, path: str, ok: bool):
        self.conn.execute("UPDATE notes SET vector_ok=? WHERE path=?",
                          (1 if ok else 0, path))
        self.conn.commit()

    def notes_missing_vectors(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT path, title FROM notes WHERE vector_ok=0 ORDER BY path"
        ).fetchall()
        return [dict(r) for r in rows]

    def like_search(self, query: str, limit: int) -> list[dict]:
        """Substring fallback for queries shorter than a trigram（<3 字）。

        FTS5 trigram 分词下短查询永不命中；小库直接对 fts 表 title/body 做
        LIKE 线性扫描（fts5 支持对列做 LIKE，走全表扫描），按路径序保稳定。"""
        q = query.strip()
        if not q:
            return []
        like = f"%{q}%"
        rows = self.conn.execute(
            "SELECT path, title FROM fts WHERE title LIKE ? OR body LIKE ? "
            "ORDER BY path LIMIT ?",
            (like, like, limit),
        ).fetchall()
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
                        forced: bool = False):
        self.conn.execute(
            "INSERT INTO guard_events (id, ts, kind, attempted_title, matched_path) "
            "VALUES (?,?,?,?,?)",
            (_uuid(), _now(), kind, attempted_title, matched_path),
        )
        self.conn.commit()

    def guard_stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM guard_events GROUP BY kind"
        ).fetchall()
        stats = {r["kind"]: r["n"] for r in rows}
        return {"refused": stats.get("refused", 0),
                "forced": stats.get("forced", 0),
                "uncovered": stats.get("uncovered", 0)}

    def count_forced_since(self, hours: int = 24) -> int:
        """Forced bypasses within a rolling window (force-confirmation ladder)."""
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

    def resolve_collision(self, collision_id: str, status: str):
        """Mark a collision resolved/dismissed (human decision via WebUI/audit)."""
        if status not in ("open", "resolved", "dismissed"):
            raise ValueError(f"非法状态: {status}")
        self.conn.execute("UPDATE collisions SET status=? WHERE id=?",
                          (status, collision_id))
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

    # ---- audit dispositions (human decisions, kept like guard_events) ----

    def record_audit_action(self, issue_id: str, kind: str, a_path: str,
                            b_path: str, action: str, note: str = "") -> None:
        """Idempotent human disposition for an audit issue (re-click updates)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO audit_actions "
            "(id, kind, a_path, b_path, action, note, acted_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (issue_id, kind, a_path, b_path, action, note, _now()))
        self.conn.commit()

    def list_audit_actions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_actions ORDER BY acted_at DESC").fetchall()
        return [dict(r) for r in rows]

    def prune_blank_audit_actions(self) -> int:
        """清理全空处置行（id/kind/action 全空——body 解析失败等事故产物，
        处置表没有删除接口，由审计自清。真实 id 形如 D1:/D2:/D4:/P:）。"""
        cur = self.conn.execute(
            "DELETE FROM audit_actions WHERE id='' AND kind='' AND action=''")
        self.conn.commit()
        return cur.rowcount

    # ---- agent execution events (append-only timeline per issue) ----

    def add_exec_event(self, issue_id: str, kind: str, event: str,
                       note: str = "", identity: str = "") -> dict:
        """Append one execution-progress event; the log is the authority."""
        ts = _now()
        cur = self.conn.execute(
            "INSERT INTO audit_exec_events (issue_id, kind, event, note, identity, ts) "
            "VALUES (?,?,?,?,?,?)", (issue_id, kind, event, note, identity, ts))
        self.conn.commit()
        return {"seq": cur.lastrowid, "issue_id": issue_id, "kind": kind,
                "event": event, "note": note, "identity": identity, "ts": ts}

    def list_exec_events(self, issue_id: str | None = None,
                         limit: int = 1000) -> list[dict]:
        """Timeline (newest first); scoped to one issue when given."""
        if issue_id:
            rows = self.conn.execute(
                "SELECT * FROM audit_exec_events WHERE issue_id=? "
                "ORDER BY seq DESC LIMIT ?", (issue_id, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM audit_exec_events "
                "ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def exec_last_status(self) -> dict[str, dict]:
        """Latest event per issue_id: {issue_id: {event, note, identity, ts, updates}}."""
        rows = self.conn.execute(
            "SELECT e.* FROM audit_exec_events e "
            "JOIN (SELECT issue_id, MAX(seq) AS mseq FROM audit_exec_events "
            "      GROUP BY issue_id) t ON e.issue_id=t.issue_id AND e.seq=t.mseq"
        ).fetchall()
        counts = {r["issue_id"]: r["n"] for r in self.conn.execute(
            "SELECT issue_id, COUNT(*) AS n FROM audit_exec_events GROUP BY issue_id"
        ).fetchall()}
        out = {}
        for r in rows:
            d = dict(r)
            d["updates"] = counts.get(d["issue_id"], 1)
            out[d["issue_id"]] = d
        return out

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
