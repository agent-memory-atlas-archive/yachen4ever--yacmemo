"""HTTP server tests: multi-user mounting + real MCP client round-trip + isolation."""

from __future__ import annotations

import asyncio
import socket

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from yacmemo.agent_changes import AGENT_CONTRACT_VERSION


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _roundtrip(port: int):

    async with (streamablehttp_client(f"http://127.0.0.1:{port}/alice/mcp") as (r, w, _),
                ClientSession(r, w) as s):
            await s.initialize()
            tools = await s.list_tools()
            names = {t.name for t in tools.tools}
            assert {"memory_search", "memory_read", "memory_write", "memory_edit",
                    "memory_edit_section", "memory_move", "memory_audit",
                    "memory_list", "memory_context", "integration_check"} <= names

            res = await s.call_tool("memory_write", {
                "title": "notes/端口配置", "content": "# 端口配置\n\n服务端口为 9721\n"})
            assert "已写入并索引" in res.content[0].text

            res = await s.call_tool("memory_search", {"query": "服务端口"})
            assert "端口配置" in res.content[0].text

            res = await s.call_tool("memory_edit", {
                "path": "端口配置", "old_string": "9721", "new_string": "8080"})
            assert "已修改" in res.content[0].text

            res = await s.call_tool("memory_read", {"path_or_title": "端口配置"})
            assert "8080" in res.content[0].text

            # 接入契约：memory_context 带版本头；integration_check 报告增量
            res = await s.call_tool("memory_context", {})
            assert f"接入契约 v{AGENT_CONTRACT_VERSION}" in res.content[0].text

            res = await s.call_tool("integration_check",
                                    {"onboarded_version": "0.0.9"})
            assert "有更新" in res.content[0].text
            assert "topics/<主题>/<笔记名>" in res.content[0].text  # 写入约定速览

            res = await s.call_tool("integration_check",
                                    {"onboarded_version": AGENT_CONTRACT_VERSION})
            assert "版本一致" in res.content[0].text

            # topic_register 返回可复制的写入模板（缺前缀事故的约定修复）
            res = await s.call_tool("topic_register",
                                    {"title": "契约测试主题", "description": "d"})
            text = res.content[0].text
            assert 'title="topics/契约测试主题/<笔记名>"' in text
            assert "abstract 是摘要卡" in text

    async with (streamablehttp_client(f"http://127.0.0.1:{port}/bob/mcp") as (r, w, _),
                ClientSession(r, w) as s):
            await s.initialize()
            res = await s.call_tool("memory_search", {"query": "服务端口"})
            assert "未找到相关笔记" in res.content[0].text  # isolation

            res = await s.call_tool("memory_write", {
                "title": "notes/端口配置", "content": "# 端口配置\n\nbob 的端口是 1234\n"})
            assert "已写入并索引" in res.content[0].text  # same title, other user: fine

    # alice's content unchanged by bob's write
    async with (streamablehttp_client(f"http://127.0.0.1:{port}/alice/mcp") as (r, w, _),
                ClientSession(r, w) as s):
            await s.initialize()
            res = await s.call_tool("memory_read", {"path_or_title": "端口配置"})
            text = res.content[0].text
            assert isinstance(text, str) and "8080" in text
            assert "1234" not in text

            # read 输出正文边界：正文块与附加信息不混淆（edit 锚点防呆）
            assert "[正文开始 |" in text and "[正文结束" in text
            assert "非文件内容" in text

            # edit 未命中时拒绝消息必须可自纠（诊断出幽灵标记）
            res = await s.call_tool("memory_edit", {
                "path": "端口配置", "old_string": "## 相关笔记\n- [[x]] (vector)",
                "new_string": "y"})
            assert "不是文件内容" in res.content[0].text

            # audit 输出必须包含游离文件与快照路径（agent 侧唯一发现渠道）
            res = await s.call_tool("memory_audit", {})
            text = res.content[0].text
            assert "游离文件" in text and "审计快照" in text
            assert "悬空主题卡" in text


def test_http_multi_user_roundtrip(http_server):
    asyncio.run(_roundtrip(http_server))


def test_health_endpoint(http_server):
    import httpx

    r = httpx.get(f"http://127.0.0.1:{http_server}/health", timeout=5)
    assert r.status_code == 200
    assert sorted(r.json()["users"]) == ["alice", "bob"]
