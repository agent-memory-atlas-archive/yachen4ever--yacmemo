"""yacmemo v2 MCP server: lean memory layer, 8 tools, stdio transport.

One process per user/memory-root:
    yacmemo-mcp --root /srv/yacmemo/yachen/memory [--config config.toml]

The directory IS the boundary: two users = two roots = two processes.
"""

from __future__ import annotations

import argparse
import logging

from mcp.server.fastmcp import FastMCP

from yacmemo.config import load_config
from yacmemo.embedding import EmbeddingClient
from yacmemo.index_db import IndexDB
from yacmemo.search import Searcher
from yacmemo.store import Store
from yacmemo.vector import VectorStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("yacmemo.mcp")

store: Store | None = None
searcher: Searcher | None = None

mcp = FastMCP("yacmemo")


def _init(root: str | None, config_path: str | None):
    global store, searcher
    config = load_config(config_path)
    if root:
        config.memory.root = root

    db = IndexDB(config.sqlite_path)
    emb = None
    vectors = None
    if config.embedding.base_url and config.embedding.model:
        emb = EmbeddingClient(
            base_url=config.embedding.base_url, api_key=config.embedding.api_key,
            model=config.embedding.model, dimensions=config.embedding.dimensions,
            timeout=config.embedding.timeout,
        )
        vectors = VectorStore(config.lancedb_path, config.embedding.dimensions)
    else:
        logger.warning("Embedding endpoint not configured — running FTS-only.")

    store = Store(config, db, emb, vectors)
    searcher = Searcher(config, db, emb, vectors)
    logger.info("yacmemo ready (root=%s)", config.root_abs)


def _fmt_search(results: list[dict]) -> str:
    if not results:
        return "未找到相关笔记。"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']} (score {r.get('score', 0):.4f}, "
                     f"{'+'.join(r.get('channels', []))})")
        lines.append(f"   path: {r['path']}")
        for w in r.get("warnings", []):
            lines.append(f"   {w}")
    return "\n".join(lines)


@mcp.tool()
def memory_search(query: str, limit: int = 10, kind: str = "hybrid") -> str:
    """混合检索记忆（FTS + 语义向量）。结果带 ⚠ 标注表示存在疑似重复/矛盾，先合并再回答。

    Args:
        query: 查询文本（中文/英文均可）
        limit: 返回数量上限
        kind: "hybrid"（默认）/"fts"/"vector"
    """
    try:
        results = searcher.search(query, limit=limit, kind=kind)
        return _fmt_search(results)
    except Exception as e:
        return f"搜索失败: {e}"


@mcp.tool()
def memory_read(path_or_title: str) -> str:
    """读取笔记全文，附相关笔记（wiki-links + 语义近邻）。

    Args:
        path_or_title: 相对路径或笔记标题
    """
    try:
        r = store.read(path_or_title)
    except Exception as e:
        return f"读取失败: {e}"

    lines = [f"# {r['title']}", f"(path: {r['path']})", "", r["content"]]
    if r["related"]:
        lines += ["", "## 相关笔记"]
        for rel in r["related"]:
            if rel.get("missing"):
                lines.append(f"- [[{rel['title']}]]（目标不存在，可考虑创建或清理该链接）")
            else:
                note = f" — {rel['note']}" if rel.get("note") else ""
                lines.append(f"- [[{rel['title']}]] ({rel['via']}){note}")
    return "\n".join(lines)


@mcp.tool()
def memory_write(title: str, content: str, force: bool = False) -> str:
    """新建笔记（一篇一主题，标题即主题名）。近似标题会被拒绝；更新已有笔记请用 memory_edit。

    Args:
        title: 笔记标题，可含目录前缀（如 "projects/yacmemo部署配置"）
        content: markdown 正文（首行建议 "# 标题"；事实行用 "- [类别] 内容"）
        force: 明确越过近似标题守卫（会被记录为违约指标，慎用）
    """
    try:
        r = store.write(title, content, force=force)
    except Exception as e:
        return f"{e}"
    note = "（注意：本次为 force 越过近似标题守卫，已记录）" if r["forced"] else ""
    return f"已写入并索引: {r['path']}{note}"


@mcp.tool()
def memory_edit(path: str, old_string: str, new_string: str) -> str:
    """就地修改笔记（唯一文本锚点替换）。这是更新事实的正确方式，不要新建重复笔记。

    Args:
        path: 笔记路径或标题
        old_string: 要替换的原文（必须在笔记中唯一）
        new_string: 替换后的文本
    """
    try:
        r = store.edit(path, old_string, new_string)
        return f"已修改并重新索引: {r['path']}"
    except Exception as e:
        return f"{e}"


@mcp.tool()
def memory_edit_section(path: str, heading: str, new_content: str) -> str:
    """按 "## 标题" 替换整个小节（P2 提供）。

    Args:
        path: 笔记路径或标题
        heading: 小节标题
        new_content: 新小节内容
    """
    try:
        store.edit_section(path, heading, new_content)
        return "ok"
    except Exception as e:
        return f"{e}"


@mcp.tool()
def memory_move(path: str, new_path: str) -> str:
    """移动笔记到新路径（标题不变，[[链接]] 按标题解析不受影响）。

    Args:
        path: 现有路径或标题
        new_path: 新路径（相对 memory_root）
    """
    try:
        r = store.move(path, new_path)
        return f"已移动: {r['old_path']} → {r['new_path']}"
    except Exception as e:
        return f"{e}"


@mcp.tool()
def memory_audit() -> str:
    """全量一致性审计：标题重复、语义撞车、悬空链接、守卫统计。"""
    try:
        r = store.audit()
    except Exception as e:
        return f"审计失败: {e}"

    lines = []
    d1 = r["title_duplicates"]
    lines.append(f"== 标题重复（{len(d1)}）==")
    for c in d1[:10]:
        lines.append(f"- [[{c['a_title']}]] ↔ [[{c['b_title']}]] (score {c['score']})")
    col = r["collisions"]
    lines.append(f"== 语义撞车（{len(col)}）==")
    for c in col[:10]:
        lines.append(f"- {c['a_path']} ↔ {c['b_path']} (score {c['score']})")
        lines.append(f"  A: {c['a_text'][:60]}")
        lines.append(f"  B: {c['b_text'][:60]}")
    dangling = r["dangling_links"]
    lines.append(f"== 悬空链接（{len(dangling)}）==")
    for d in dangling[:10]:
        lines.append(f"- {d['path']}: [[{d['link']}]]")
    g = r["guard_stats"]
    lines.append(f"== 守卫统计 == 拒绝 {g['refused']} 次，force 越过 {g['forced']} 次")
    return "\n".join(lines)


@mcp.tool()
def memory_list(path: str = "", sort: str = "name") -> str:
    """列出笔记目录树。

    Args:
        path: 子目录（空 = 根目录）
        sort: "name" 或 "mtime"（最近变更优先）
    """
    try:
        entries = store.list_notes(path, sort=sort)
    except Exception as e:
        return f"{e}"
    return "\n".join(entries) if entries else "（空）"


def main():
    parser = argparse.ArgumentParser(description="yacmemo lean memory MCP server")
    parser.add_argument("--config", default=None, help="Path to config.toml")
    parser.add_argument("--root", default=None,
                        help="Memory root directory (overrides config)")
    args = parser.parse_args()

    _init(args.root, args.config)
    logger.info("yacmemo MCP server starting (stdio transport)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
