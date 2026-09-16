"""WebUI API tests: notes CRUD, search, audit, collision resolve, usage log."""

from __future__ import annotations

import asyncio
import sqlite3

import httpx


def test_webui_index_served(http_server):
    """已构建 → /ui/ 200；未构建 → 503 带构建指引（服务本身不崩）。"""
    from yacmemo.webui.app import STATIC_DIR

    r = httpx.get(f"http://127.0.0.1:{http_server}/ui/", timeout=5)
    if (STATIC_DIR / "index.html").is_file():
        assert r.status_code == 200
        assert "yacmemo" in r.text
    else:
        assert r.status_code == 503
        assert "build_webui" in r.text


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


def test_config_get_save_roundtrip(http_server, tmp_path):
    # GET: 内容含用户段，path 指向 tmp 配置
    r = httpx.get(f"http://127.0.0.1:{http_server}/api/config", timeout=5).json()
    assert r["ok"] is True and "[[users]]" in r["content"]
    assert r["path"].endswith("config.toml")

    # POST 合法修改 → 保存 + 备份
    new_content = r["content"] + "\n# edited-by-test\n"
    r = httpx.post(f"http://127.0.0.1:{http_server}/api/config",
                   json={"content": new_content}, timeout=5).json()
    assert r["ok"] is True and r["backup"]
    assert (tmp_path / "config.toml").read_text(encoding="utf-8").endswith("# edited-by-test\n")
    backups = list((tmp_path).glob("config.toml.bak-*"))
    assert backups, "backup file should exist"

    # POST 非法 TOML → 拒绝
    r = httpx.post(f"http://127.0.0.1:{http_server}/api/config",
                   json={"content": "not [ valid toml"}, timeout=5).json()
    assert r["ok"] is False and "TOML" in r["error"]

    # POST 合法 TOML 但非法用户 id → 结构校验拒绝
    bad = httpx.get(f"http://127.0.0.1:{http_server}/api/config", timeout=5).json()["content"]
    bad = bad.replace('id = "bob"', 'id = "bad id!"')
    r = httpx.post(f"http://127.0.0.1:{http_server}/api/config",
                   json={"content": bad}, timeout=5).json()
    assert r["ok"] is False and "校验失败" in r["error"]


def test_proposals_list_empty(http_server):
    r = httpx.get(f"http://127.0.0.1:{http_server}/api/alice/proposals", timeout=5).json()
    assert r["ok"] is True and r["proposals"] == []


def test_curator_run_requires_config(http_server):
    r = httpx.post(f"http://127.0.0.1:{http_server}/api/alice/curator", timeout=5).json()
    assert r["ok"] is False and "未配置" in r["error"]
