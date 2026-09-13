"""HTTP server tests: multi-user mounting + real MCP client round-trip + isolation."""

from __future__ import annotations

import asyncio
import socket
import threading

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from yacmemo.server import create_app


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def http_server(tmp_path):
    """Two-user HTTP server (FTS-only: no embedding endpoint configured)."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
[embedding]
base_url = ""
model = ""

[[users]]
id = "alice"
root = "{(tmp_path / "alice").as_posix()}"

[[users]]
id = "bob"
root = "{(tmp_path / "bob").as_posix()}"
""",
        encoding="utf-8",
    )
    from yacmemo.config import load_config

    config = load_config(str(config_file))
    app = create_app(config)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        import time

        time.sleep(0.1)
    assert server.started, "uvicorn did not start"

    yield port

    server.should_exit = True
    thread.join(timeout=5)


async def _roundtrip(port: int):

    async with (streamablehttp_client(f"http://127.0.0.1:{port}/alice/mcp") as (r, w, _),
                ClientSession(r, w) as s):
            await s.initialize()
            tools = await s.list_tools()
            names = {t.name for t in tools.tools}
            assert {"memory_search", "memory_read", "memory_write", "memory_edit",
                    "memory_edit_section", "memory_move", "memory_audit",
                    "memory_list"} <= names

            res = await s.call_tool("memory_write", {
                "title": "端口配置", "content": "# 端口配置\n\n服务端口为 9721\n"})
            assert "已写入并索引" in res.content[0].text

            res = await s.call_tool("memory_search", {"query": "服务端口"})
            assert "端口配置" in res.content[0].text

            res = await s.call_tool("memory_edit", {
                "path": "端口配置", "old_string": "9721", "new_string": "8080"})
            assert "已修改" in res.content[0].text

            res = await s.call_tool("memory_read", {"path_or_title": "端口配置"})
            assert "8080" in res.content[0].text

    async with (streamablehttp_client(f"http://127.0.0.1:{port}/bob/mcp") as (r, w, _),
                ClientSession(r, w) as s):
            await s.initialize()
            res = await s.call_tool("memory_search", {"query": "服务端口"})
            assert "未找到相关笔记" in res.content[0].text  # isolation

            res = await s.call_tool("memory_write", {
                "title": "端口配置", "content": "# 端口配置\n\nbob 的端口是 1234\n"})
            assert "已写入并索引" in res.content[0].text  # same title, other user: fine

    # alice's content unchanged by bob's write
    async with (streamablehttp_client(f"http://127.0.0.1:{port}/alice/mcp") as (r, w, _),
                ClientSession(r, w) as s):
            await s.initialize()
            res = await s.call_tool("memory_read", {"path_or_title": "端口配置"})
            text = res.content[0].text
            assert isinstance(text, str) and "8080" in text
            assert "1234" not in text


def test_http_multi_user_roundtrip(http_server):
    asyncio.run(_roundtrip(http_server))


def test_health_endpoint(http_server):
    import httpx

    r = httpx.get(f"http://127.0.0.1:{http_server}/health", timeout=5)
    assert r.status_code == 200
    assert sorted(r.json()["users"]) == ["alice", "bob"]
