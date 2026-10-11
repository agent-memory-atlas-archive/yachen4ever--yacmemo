"""MCP payload shape: every tool must return its body exactly once.

2026-10-03 审查 §8.2：工具标注 `-> str` 时 FastMCP 会同时产出 text content
和 structuredContent{"result": ...}，两份内容一模一样——实测每个工具的返回体
被完整塞两遍（waste 2.07x），memory_context 这种大 payload 每次冷启动白烧
一半 token。客户端读的本来就是 content[0].text，structuredContent 是纯冗余。

修法是逐工具 structured_output=False（见 tools.register_tools 的 `tool`）。
mcp 1.30 的 FastMCP.__init__ 不接受这个参数，只能逐工具给。

工具面**不在本文件里写死数量**：数量真值由 test_doc_truth.py 从真实握手
现取、再去对文档。本文件关心的是"结构化输出有没有被重新打开"，所以期望值
从 tools.py 的源码现场推导（ast，见 _declared_tool_names）——写死的 `22`
只会变成第 4 处要手工同步的数，工具 23 落地时它给的是一句无上下文的
`assert 22 == 23`，既不指出是哪个工具、也不指向该改哪里。
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import (
    create_connected_server_and_client_session as connected,
)

from yacmemo.search import Searcher
from yacmemo.store import Store
from yacmemo.tools import register_tools


def _declared_tool_names() -> set[str]:
    """tools.py 里被 `@tool()` 装饰的函数名——用 ast 扫，不用 grep。

    grep `@tool()` 会把注释和文档字符串里的写法一起数进来：tools.py 的
    register_tools 里就有一行注释「新加工具写 `@tool()` 就自动带上」，实测
    naive grep 数出 23，真实工具只有 22 个——比写死的 22 更糟的真值。
    ast 只认 FunctionDef 上的装饰器调用，注释与字符串天然进不来。
    """
    src = inspect.getsourcefile(register_tools)
    assert src and Path(src).is_file(), \
        f"拿不到 tools.py 源码（inspect.getsourcefile -> {src!r}），本文件的推导失效"
    tree = ast.parse(Path(src).read_text(encoding="utf-8"))
    reg = next((n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "register_tools"),
               None)
    assert reg is not None, "tools.py 里找不到 register_tools——本文件的前提失效了"
    return {n.name for n in reg.body
            if isinstance(n, ast.FunctionDef)
            and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Name)
                    and d.func.id == "tool" for d in n.decorator_list)}


def _probe(store: Store, searcher: Searcher, tool: str, args: dict):
    """在进程内跑一次真实的 MCP 往返，返回 (工具清单, 调用结果)。"""
    mcp = FastMCP("payload-test")
    register_tools(mcp, store, searcher)

    async def run():
        async with connected(mcp._mcp_server) as c:
            tools = (await c.list_tools()).tools
            return tools, await c.call_tool(tool, args)

    return asyncio.run(run())


def test_no_tool_advertises_structured_output(store: Store, searcher: Searcher):
    """新加工具若漏写 @tool()，outputSchema 会重新冒出来——这条是防漏的哨兵。

    对账而不是数数：MCP 握手暴露的工具名必须与 tools.py 里 @tool() 声明的
    函数名**一一对应**。少一个（漏挂局部 `tool` partial → 又开回双份返回体）
    或多一个（走别的入口注册的裸 @mcp.tool）都会在这里点名到具体工具名。
    """
    tools, _ = _probe(store, searcher, "memory_list", {"path": ""})
    declared = _declared_tool_names()
    assert declared, "ast 一个 @tool() 都没扫到，后面的对账会空对空"

    registered = [t.name for t in tools]
    assert set(registered) == declared and len(registered) == len(declared), (
        "MCP 暴露的工具面与 tools.py 的 @tool() 声明对不上"
        f"（握手 {len(registered)} 个 / 源码 {len(declared)} 个）:\n"
        f"  只在握手里（源码里没挂局部 tool partial）: "
        f"{sorted(set(registered) - declared)}\n"
        f"  只在源码里（没注册上）: {sorted(declared - set(registered))}")
    assert [t.name for t in tools if t.outputSchema] == [], (
        "有工具又开回 structured output 了——返回体会被塞两遍")


def test_tool_result_is_not_duplicated(store: Store, searcher: Searcher):
    _, r = _probe(store, searcher, "memory_list", {"path": ""})
    assert r.structuredContent is None, "structuredContent 应为空，不再重复返回体"
    text = r.content[0].text
    assert "notes/a.md" in text, "text content 必须原样保留"
    assert len(r.content) == 1


def test_memory_list_annotates_settled_proposals(store: Store, searcher: Searcher):
    """0.3.16：curator/ 已结案提案在 memory_list 输出里就地标注「（已结案）」
    ——agent 找待办提案只读未标注的行，不必全量列出后逐份 memory_read
    确认结案状态（2026-10-07 审计会话实爆的浪费模式）。"""
    from yacmemo.store import PROPOSAL_SETTLED_MARKER

    settled = "curator/提案-20260925已结.md"
    pending = "curator/提案-20261007待裁.md"
    store.save(settled, f"# 提案A\n\n{PROPOSAL_SETTLED_MARKER}（2026-09-25）\n")
    store.save(pending, "# 提案B\n\n**状态：待裁决**\n")
    # 标注只对 curator/ 生效：库内其他位置出现同字样（如演示文档）不标注
    quoted = "notes/引用结案标记.md"
    store.save(quoted, f"# 笔记\n提到 {PROPOSAL_SETTLED_MARKER} 只是引用。\n")

    _, r = _probe(store, searcher, "memory_list", {"path": "curator"})
    text = r.content[0].text
    assert f"{settled}（已结案）" in text
    assert f"{pending}（已结案）" not in text and pending in text, \
        "未结案提案必须保持裸路径（逐字节不变），让 agent 知道它要处理"

    _, r = _probe(store, searcher, "memory_list", {"path": ""})
    text = r.content[0].text
    assert f"{settled}（已结案）" in text, "根目录递归列出同样要标注"
    assert f"{quoted}（已结案）" not in text

    # 判定实时读盘不缓存：撤标（复审追加新条目的 curator 行为）后标注即刻消失
    store.save(settled, "# 提案A\n\n**状态：待裁决**\n")
    _, r = _probe(store, searcher, "memory_list", {"path": "curator"})
    assert f"{settled}（已结案）" not in r.content[0].text


def test_memory_search_hides_archive(store: Store, searcher: Searcher):
    """0.3.17：归档笔记（archive/）对 MCP 客户端不可见——检索不再出现。"""
    store.write("notes/live.md", "# live\n\n- [配置] 服务端口为 8080\n")
    store.write("notes/old.md", "# old\n\n- [配置] 服务端口为 9090\n")
    ar = store.note_archive("notes/old.md", reason="旧")

    _, r = _probe(store, searcher, "memory_search",
                  {"query": "服务端口", "kind": "fts"})
    text = r.content[0].text
    assert ar["to"] not in text and "notes/live.md" in text
    assert "归档" in text, "隐去条数要对 agent 留痕（提示条）"


def test_memory_list_hides_archive(store: Store, searcher: Searcher):
    """0.3.17：memory_list 不列 archive/；显式列归档子目录说明不可见。"""
    store.write("notes/gone.md", "# gone\n\n- [配置] x\n")
    ar = store.note_archive("notes/gone.md", reason="旧")

    _, r = _probe(store, searcher, "memory_list", {"path": ""})
    text = r.content[0].text
    assert ar["to"] not in text, "归档笔记的路径不得出现在列表里"
    assert "归档笔记对客户端不可见" in text, "根列出时对隐去条数留痕"

    _, r = _probe(store, searcher, "memory_list", {"path": "archive"})
    text = r.content[0].text
    assert ar["to"] not in text and "不可见" in text


def test_memory_read_hides_archive_related_and_title(store: Store,
                                                     searcher: Searcher):
    """0.3.17：相关笔记不含归档目标（wiki-link 与向量两条路都隐）；
    按标题解析归档笔记给出归档提示；按归档路径显式读取仍可。"""
    store.write("notes/hub.md", "# hub\n\n- 见 [[target-note]]\n")
    store.write("notes/target-note.md",
                "# target-note\n\n- [配置] 服务端口为 8080\n")
    ar = store.note_archive("notes/target-note.md", reason="旧")

    _, r = _probe(store, searcher, "memory_read", {"path_or_title": "notes/hub.md"})
    text = r.content[0].text
    # hub 正文里本身写着 [[target-note]]，只断言附加的"相关笔记"段
    _, _, related_section = text.partition("[正文结束")
    assert "target-note" not in related_section, "归档目标不得经相关笔记外泄"

    _, r = _probe(store, searcher, "memory_read",
                  {"path_or_title": "target-note"})
    text = r.content[0].text
    assert "已归档" in text and ar["to"] in text, "标题解析给归档位置与出口"

    _, r = _probe(store, searcher, "memory_read", {"path_or_title": ar["to"]})
    assert "[正文开始" in r.content[0].text, "按归档路径显式读取是明确查阅，仍可"
