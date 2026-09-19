"""UsageDB tests: before_hash column (fresh schema + old-DB migration)."""

from __future__ import annotations

import sqlite3

from yacmemo.usage import UsageDB


def test_call_log_before_hash_roundtrip(tmp_path):
    db = UsageDB(str(tmp_path / "usage.db"))
    db.log_call("u", "memory_write", "title=x")
    db.log_call("u", "memory_edit", "path=x", before_hash="a" * 64)
    by_tool = {r["tool"]: r for r in db.recent()}  # 同秒写入，ts 排序不稳定
    assert by_tool["memory_write"]["before_hash"] == ""  # 新建写入无变更前状态
    assert by_tool["memory_edit"]["before_hash"] == "a" * 64
    db.close()


def test_call_log_before_hash_migrates_old_db(tmp_path):
    """0.2.0 及更早的 usage.db 无 before_hash 列——打开时自动 ALTER 补列。"""
    p = tmp_path / "usage.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE call_log ("
        "id TEXT PRIMARY KEY, ts TEXT NOT NULL, user_id TEXT NOT NULL, "
        "client TEXT NOT NULL DEFAULT '', ip TEXT NOT NULL DEFAULT '', "
        "tool TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', "
        "duration_ms INTEGER NOT NULL DEFAULT 0, ok INTEGER NOT NULL DEFAULT 1, "
        "error TEXT NOT NULL DEFAULT '')"
    )
    conn.commit()
    conn.close()

    db = UsageDB(str(p))
    db.log_call("u", "memory_delete", "path=x", before_hash="b" * 64)
    rows = db.recent()
    assert rows[0]["before_hash"] == "b" * 64
    db.close()
