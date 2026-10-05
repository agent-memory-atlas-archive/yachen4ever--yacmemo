"""WebUI 二级路径（[server].base_path）测试。

前缀透传方案：base_path 设置时 WebUI 全部路由在根上**额外**挂一份
Mount(base)（nginx 不改写原样透传；直连 :9721 的无前缀旧入口向后兼容），
MCP 与 /health 始终留在根上。默认空 = 路由行为与改动前一致（回归由
test_server / test_webui 全量覆盖）。
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from starlette.routing import Mount, Route

from yacmemo.config import (Config, MemoryConfig, ServerConfig, UserEntry,
                            normalize_base_path)
from yacmemo.server import create_app
from yacmemo.webui.app import STATIC_DIR, create_webui_routes

REPO = Path(__file__).resolve().parents[1]


def _route_sig(r) -> tuple:
    """(kind, path, methods) 签名：同一批路由两次构建是不同闭包对象，不能按
    对象身份断言；按签名比多重集，同路径不同方法/静态挂载的条目也不会漏。"""
    return (type(r).__name__, r.path,
            tuple(sorted(getattr(r, "methods", None) or ())))


# ---------------------------------------------------------------- 规范化

def test_normalize_base_path_table():
    """归一化表：前导斜杠补齐、尾斜杠剥离、空白容忍、空值=禁用。"""
    assert normalize_base_path("") == ""
    assert normalize_base_path("/") == ""
    assert normalize_base_path("yacmemo") == "/yacmemo"
    assert normalize_base_path("/yacmemo/") == "/yacmemo"
    assert normalize_base_path("  /yacmemo/ ") == "/yacmemo"
    assert normalize_base_path("/a/b") == "/a/b"


def test_normalize_base_path_rejects_bad_segments():
    """空段 / 点段 / 非 ASCII / 含查询字符一律拒绝（不静默纠偏）。"""
    for bad in ("/yacmemo//sub", "/a/./b", "/a/../b", "/导致", "/a b", "/a?x"):
        with pytest.raises(ValueError):
            normalize_base_path(bad)


def test_normalize_base_path_rejects_conflicts():
    """首段撞保留路径或用户 MCP 挂载 → 拒绝，不做静默遮蔽。"""
    with pytest.raises(ValueError):
        normalize_base_path("/ui")
    with pytest.raises(ValueError):
        normalize_base_path("/api/sub")
    with pytest.raises(ValueError):
        normalize_base_path("/alice/sub", ["alice", "bob"])


# ------------------------------------------------- 可计数事实：路由挂载

def _make_config(tmp_path, base_path: str = "") -> Config:
    for d in ("memory", "alice", "server-data"):
        (tmp_path / d).mkdir()
    return Config(
        memory=MemoryConfig(root=str(tmp_path / "memory")),
        server=ServerConfig(data_dir=str(tmp_path / "server-data"),
                            base_path=base_path),
        users=[UserEntry(id="alice", root=str(tmp_path / "alice"))],
    )


def test_default_base_path_routes_at_root(tmp_path):
    """base_path 默认空：WebUI 路由全部在根上，无前缀 Mount（与改动前同形）。"""
    cfg = _make_config(tmp_path)
    app = create_app(cfg)
    webui = create_webui_routes(cfg, {})
    # 真值从代码派生：每一条 webui 路由的 (path, methods) 都必须出现在
    # 顶层路由表里（多重集包含）
    root_sigs = Counter(_route_sig(r) for r in app.routes)
    assert Counter(_route_sig(r) for r in webui) <= root_sigs
    assert not any(getattr(r, "name", "") == "webui" for r in app.routes)


def test_base_path_dual_registration(tmp_path):
    """base_path 设置：根上保留一份（直连兼容）+ 前缀 Mount 完整一份。

    计数断言从代码派生：Mount 内的路由条数必须等于 create_webui_routes
    的产出条数——挂载逻辑丢了路由，这里先红。
    """
    cfg = _make_config(tmp_path, base_path="/yacmemo")
    app = create_app(cfg)
    webui = create_webui_routes(cfg, {})

    mounts = [r for r in app.routes if isinstance(r, Mount) and r.path == "/yacmemo"]
    assert len(mounts) == 1, "前缀 Mount 必须恰好挂一份"
    inner = mounts[0].routes
    # 逐条签名核对（不只比条数：丢了 A 换成重复的 B 也能抓到）
    assert Counter(_route_sig(r) for r in inner) == Counter(_route_sig(r) for r in webui), (
        "前缀 Mount 内路由与 create_webui_routes 产出不一致——挂载不完整")

    # 向后兼容：同一批路由在根上仍有一份（按 (path, methods) 多重集核对）
    root_sigs = Counter(_route_sig(r) for r in app.routes)
    assert Counter(_route_sig(r) for r in webui) <= root_sigs

    # MCP 挂载与 /health 留在根上，不受前缀影响
    assert any(isinstance(r, Mount) and r.path == "/alice"
               for r in app.routes)
    assert any(getattr(r, "path", "") == "/health" for r in app.routes)


# --------------------------------------------------- 集成：前缀服务行为

def test_base_path_ui_prefix_and_backward_compat(http_server_base):
    """base_path=/yacmemo：前缀入口可用，直连 :9721/ui/ 依旧可用。"""
    has_dist = (STATIC_DIR / "index.html").is_file()
    url = f"http://127.0.0.1:{http_server_base}"

    r = httpx.get(f"{url}/yacmemo/ui/", timeout=5)
    if has_dist:
        assert r.status_code == 200
        html = r.text
        # 改写生效：资源引用整串换前缀，页面里不残留裸 "/ui/ 引用
        assert '"/yacmemo/ui/' in html
        assert '"/ui/' not in html
        assert 'window.__BASE_PATH__="/yacmemo"' in html
    else:
        assert r.status_code == 503 and "build_webui" in r.text

    # 向后兼容：无前缀直连依旧可用（同一份改写后的页面，资源落在同源
    # 前缀 Mount 上，两个入口都能活）
    r = httpx.get(f"{url}/ui/", timeout=5)
    assert (r.status_code == 200) if has_dist else (r.status_code == 503)


def test_base_path_redirects(http_server_base):
    """跳转链全部携带前缀：/ 与 /yacmemo 系列入口最终都落到 /yacmemo/ui/。"""
    url = f"http://127.0.0.1:{http_server_base}"
    no_follow = dict(follow_redirects=False, timeout=5)

    r = httpx.get(f"{url}/", **no_follow)
    assert r.status_code == 307 and r.headers["location"] == "/yacmemo/ui/"

    # 根上的 "/" 跳转目标本身可达（307 跟到下一跳仍是前缀内）
    r = httpx.get(f"{url}/yacmemo/", **no_follow)
    assert r.status_code == 307 and r.headers["location"] == "/yacmemo/ui/"

    # 裸前缀由 Starlette 自动补斜杠（nginx location /yacmemo/ 不路由裸前缀，
    # 此行为只服务直连场景）；Location 是带 scheme+host 的绝对 URL
    r = httpx.get(f"{url}/yacmemo", **no_follow)
    assert r.status_code == 307
    assert r.headers["location"] == f"{url}/yacmemo/"


def test_base_path_api_under_prefix(http_server_base, tmp_path):
    """API 在前缀下完整可用（走真实 Store 守卫路径）。"""
    base = f"http://127.0.0.1:{http_server_base}/yacmemo/api/alice"

    r = httpx.post(f"{base}/notes", json={
        "title": "notes/前缀测试", "content": "# 前缀测试\n\n内容\n"}, timeout=5)
    assert r.json()["ok"] is True

    r = httpx.get(f"{base}/notes", timeout=5)
    assert "notes/前缀测试.md" in [n["path"] for n in r.json()["notes"]]

    # 清理（测试动作不留痕）
    r = httpx.delete(f"{base}/note", params={"path": "notes/前缀测试.md"},
                     timeout=5)
    assert r.json()["ok"] is True


def test_base_path_mcp_unaffected(http_server_base):
    """MCP 挂载不随 base_path 前缀：/{uid}/mcp 原路径握手正常。"""

    async def run():
        async with (streamablehttp_client(
                f"http://127.0.0.1:{http_server_base}/alice/mcp") as (r, w, _),
                ClientSession(r, w) as s):
            await s.initialize()
            tools = await s.list_tools()
            return {t.name for t in tools.tools}

    names = asyncio.run(run())
    assert {"memory_search", "memory_write"} <= names


def test_base_path_health_at_root(http_server_base):
    """/health 是服务级端点，不随 WebUI 挂前缀（与 MCP 同理，直连监控用）。"""
    r = httpx.get(f"http://127.0.0.1:{http_server_base}/health", timeout=5)
    assert r.status_code == 200
    assert sorted(r.json()["users"]) == ["alice", "bob"]


def test_base_path_login_page_prefixed(http_server_base_auth):
    """登录页的 fetch 与登录流都带前缀（鉴权开启时才会回登录页）。"""
    url = f"http://127.0.0.1:{http_server_base_auth}"

    r = httpx.get(f"{url}/yacmemo/ui/", timeout=5)
    assert r.status_code == 200
    assert "fetch('/yacmemo/api/login'" in r.text
    assert "fetch('/api/login'" not in r.text

    r = httpx.post(f"{url}/yacmemo/api/login",
                   json={"password": "secret"}, timeout=5)
    assert r.json()["ok"] is True


def test_frontend_no_hardcoded_ui_absolute_paths():
    """前缀消费面守卫：前端源码禁止硬编码 /ui/ 绝对路径。

    编译进 JS chunk 的路径，服务端对 index.html 的改写够不到——SearchPage
    曾藏了一处 ``window.open('/ui/#note=…')``，base_path 模式下点搜索结果
    直接 404。组件内一律用 composables/api.js 导出的 BASE 拼前缀。
    """
    pattern = re.compile("""['"`]/ui/""")
    hits = []
    for p in sorted((REPO / "frontend" / "src").rglob("*")):
        if p.is_dir() or p.suffix not in (".vue", ".js"):
            continue
        rel = p.relative_to(REPO).as_posix()
        for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    assert not hits, "前端硬编码 /ui/ 绝对路径（改用 api.js 的 BASE 拼）：\n" + \
        "\n".join(hits)
