"""WebUI API tests: notes CRUD, search, audit, collision resolve, usage log."""

from __future__ import annotations

import asyncio
import sqlite3

import httpx


def test_webui_index_served(http_server):
    r = httpx.get(f"http://127.0.0.1:{http_server}/ui/", timeout=5)
    assert r.status_code == 200
    assert "yacmemo" in r.text
    r = httpx.get(f"http://127.0.0.1:{http_server}/ui/static/app.js", timeout=5)
    assert r.status_code == 200
    r = httpx.get(f"http://127.0.0.1:{http_server}/ui/static/marked.min.js", timeout=5)
    assert r.status_code == 200


def test_webui_api_notes_crud(http_server):
    base = f"http://127.0.0.1:{http_server}/api/alice"
    # create (via REST, same guard path as MCP)
    r = httpx.post(f"{base}/notes", json={
        "title": "端口配置", "content": "# 端口配置\n\n服务端口为 9721\n"})
    assert r.json()["ok"] is True

    # near-duplicate refused
    r = httpx.post(f"{base}/notes", json={
        "title": "端口配置-2", "content": "x", "force": False})
    body = r.json()
    assert body["ok"] is False and "近似标题" in body["error"]

    # force bypasses (human clicks in WebUI = human confirmation)
    r = httpx.post(f"{base}/notes", json={
        "title": "端口配置-2", "content": "# 端口配置-2\nx", "force": True,
        "force_confirm": True})
    assert r.json()["ok"] is True

    # list
    r = httpx.get(f"{base}/notes")
    paths = [n["path"] for n in r.json()["notes"]]
    assert "端口配置.md" in paths and "端口配置-2.md" in paths

    # read
    r = httpx.get(f"{base}/note", params={"path": "端口配置"})
    assert "9721" in r.json()["content"]

    # save (full-content edit)
    r = httpx.put(f"{base}/note", json={
        "path": "端口配置", "content": "# 端口配置\n\n服务端口为 8080\n"})
    assert r.json()["ok"] is True
    r = httpx.get(f"{base}/note", params={"path": "端口配置"})
    assert "8080" in r.json()["content"]

    # search via API
    r = httpx.get(f"{base}/search", params={"q": "服务端口"})
    assert any(n["title"] == "端口配置" for n in r.json()["results"])

    # audit
    r = httpx.post(f"{base}/audit")
    a = r.json()["audit"]
    assert "title_duplicates" in a and "guard_stats" in a

    # delete
    r = httpx.delete(f"{base}/note", params={"path": "端口配置-2"})
    assert r.json()["ok"] is True
    r = httpx.get(f"{base}/notes")
    assert "端口配置-2.md" not in [n["path"] for n in r.json()["notes"]]


def test_webui_collision_resolve(http_server, tmp_path):
    """Insert a collision row directly, then resolve it via the WebUI API."""
    db_path = tmp_path / "alice" / ".index" / "index.db"
    conn = sqlite3.connect(db_path)
    cid = "test-collision-1"
    conn.execute(
        "INSERT INTO collisions (id, kind, a_path, b_path, a_text, b_text, score, "
        "detected_at, status) VALUES (?,?,?,?,?,?,?,?, 'open')",
        (cid, "obs", "a.md", "b.md", "A 文本", "B 文本", 0.9, "2026-09-14T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    base = f"http://127.0.0.1:{http_server}/api/alice"
    r = httpx.post(f"{base}/audit")
    a = r.json()["audit"]
    # a.md / b.md notes don't exist → stale pruning removes it; insert again
    # against real notes instead
    if not any(c["id"] == cid for c in a["collisions"]):
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM collisions WHERE id=?", (cid,))
        conn.execute(
            "INSERT INTO notes (path, title, content_hash, updated_at) "
            "VALUES ('a.md','A','x','2026-09-14'), ('b.md','B','y','2026-09-14')")
        conn.execute(
            "INSERT INTO collisions (id, kind, a_path, b_path, a_text, b_text, score, "
            "detected_at, status) VALUES (?,?,?,?,?,?,?,?, 'open')",
            (cid, "obs", "a.md", "b.md", "A 文本", "B 文本", 0.9,
             "2026-09-14T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()

    r = httpx.post(f"{base}/collision",
                   json={"id": cid, "status": "dismissed"})
    assert r.json()["ok"] is True

    r = httpx.post(f"{base}/audit")
    a = r.json()["audit"]
    row = next((c for c in a["collisions"] if c["id"] == cid), None)
    if row is not None:  # dismissed rows only show with status filter; open list must not contain it
        assert row["status"] != "open"


def test_usage_logged_for_mcp_calls(http_server, tmp_path):
    """MCP tool calls land in the server-level usage log with client info."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def call():
        async with (streamablehttp_client(
                f"http://127.0.0.1:{http_server}/alice/mcp") as (r, w, _),
                ClientSession(r, w) as s):
            await s.initialize()
            await s.call_tool("memory_write", {
                "title": "调用日志测试", "content": "# 调用日志测试\n内容\n"})
            await s.call_tool("memory_search", {"query": "内容"})

    asyncio.run(call())

    usage_db = tmp_path / "server-data" / "usage.db"
    assert usage_db.is_file()
    conn = sqlite3.connect(usage_db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM call_log WHERE user_id='alice' ORDER BY ts")]
    tools = [r["tool"] for r in rows]
    assert "memory_write" in tools and "memory_search" in tools
    write_row = next(r for r in rows if r["tool"] == "memory_write")
    assert "调用日志测试" in write_row["summary"]
    assert write_row["ok"] == 1
    conn.close()


def test_webui_overview(http_server):
    r = httpx.get(f"http://127.0.0.1:{http_server}/api/overview")
    data = r.json()
    assert data["ok"] is True
    ids = {u["id"] for u in data["users"]}
    assert ids == {"alice", "bob"}
    assert "embedding" in data and data["embedding"]["configured"] is False
