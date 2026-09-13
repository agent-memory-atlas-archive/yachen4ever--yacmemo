"""yacmemo MCP server: exposes 9 memory tools via stdio transport.

Multi-user: start with --user <id> to select which user's memory to serve.
Each user has isolated memory directory, SQLite database, and LanceDB index.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading

from mcp.server.fastmcp import FastMCP

from yacmemo.config import load_config, Config, UserConfig
from yacmemo.db import MemoryDB
from yacmemo.vector import VectorStore
from yacmemo.embedding import EmbeddingClient
from yacmemo.fs_utils import (
    is_in_split_dir,
    safe_write,
    safe_edit,
    to_rel_path,
    list_md_files,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("yacmemo.mcp")

# ---- Per-user state (initialized in main with --user) ----
config: Config | None = None
user: UserConfig | None = None
db: MemoryDB | None = None
vector: VectorStore | None = None
emb: EmbeddingClient | None = None

mcp = FastMCP("yacmemo")


def _init_for_user(config_path: str | None, user_id: str):
    """Initialize all components for a specific user."""
    global config, user, db, vector, emb

    config = load_config(config_path)
    user = config.get_user(user_id)

    memory_root = config.user_memory_root_abs(user)
    sqlite_path = config.user_sqlite_abs(user)
    lancedb_path = config.user_lancedb_abs(user)

    os.makedirs(os.path.dirname(sqlite_path), exist_ok=True)
    os.makedirs(os.path.dirname(lancedb_path), exist_ok=True)

    db = MemoryDB(sqlite_path)
    vector = VectorStore(lancedb_path, config.embedding.dimensions)

    emb_cfg = config.resolve_embedding(user)
    emb = EmbeddingClient(
        base_url=emb_cfg.base_url, api_key=emb_cfg.api_key,
        model=emb_cfg.model, dimensions=emb_cfg.dimensions, timeout=emb_cfg.timeout,
    )

    logger.info("Initialized for user '%s' (memory: %s)", user.id, memory_root)


def _memory_root() -> str:
    return config.user_memory_root_abs(user)


def _resolve_path(path: str) -> str:
    """Resolve a relative path to absolute within this user's memory_root."""
    if os.path.isabs(path):
        return path
    return os.path.join(_memory_root(), path)


def _trigger_extract(path: str):
    """Async trigger extraction via webhook to enhancer."""
    try:
        import httpx
        rel = to_rel_path(_resolve_path(path), _memory_root())

        def _post():
            try:
                httpx.post(
                    f"http://{config.server.host}:{config.server.port}/trigger",
                    json={"action": "extract", "path": rel, "user_id": user.id},
                    timeout=5,
                )
            except Exception as e:
                logger.warning("Webhook trigger failed: %s", e)

        threading.Thread(target=_post, daemon=True).start()
    except Exception as e:
        logger.warning("Trigger extract failed: %s", e)


@mcp.tool()
def memory_search(query: str, limit: int = 10) -> str:
    """语义搜索记忆。返回实体/事件/关系的摘要和来源路径。

    Args:
        query: 搜索查询文本
        limit: 返回结果数量上限（默认10）
    """
    try:
        query_emb = emb.embed_one(query)
        results = vector.search_all(query_emb, limit=limit)

        if not results:
            return "未找到相关记忆。"

        lines = []
        for i, r in enumerate(results, 1):
            kind = r.get("kind", "?")
            text = r.get("text", "")
            source = r.get("source_path", "")
            dist = r.get("_distance", 0)
            lines.append(f"{i}. [{kind}] {text}")
            lines.append(f"   source: {source} (distance: {dist:.4f})")

        return "\n".join(lines)
    except Exception as e:
        return f"搜索失败: {e}"


@mcp.tool()
def memory_grep(pattern: str, path: str = "") -> str:
    """正则搜索 .md 原文文件。

    Args:
        pattern: 正则表达式
        path: 搜索范围（相对 memory_root 的目录路径，空=全部）
    """
    search_dir = _resolve_path(path) if path else _memory_root()

    try:
        result = subprocess.run(
            ["rg", "-n", "--no-heading", pattern, search_dir],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode not in (0, 1):
            return f"grep 错误: {result.stderr}"
        if not result.stdout:
            return "未找到匹配。"
        return result.stdout
    except FileNotFoundError:
        return "ripgrep (rg) 未安装"
    except subprocess.TimeoutExpired:
        return "搜索超时"


@mcp.tool()
def memory_read(path: str) -> str:
    """读取 .md 文件内容。

    Args:
        path: 文件路径（相对 memory_root 或绝对路径）
    """
    full_path = _resolve_path(path)

    if not os.path.isfile(full_path):
        return f"文件不存在: {path}"

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"读取失败: {e}"


@mcp.tool()
def memory_write(path: str, content: str) -> str:
    """写入 .md 文件。自动触发后台提取（索引更新）。不能写入拆分目录。

    Args:
        path: 文件路径（相对 memory_root 或绝对路径）
        content: 文件内容
    """
    memory_root = _memory_root()
    full_path = _resolve_path(path)

    try:
        safe_write(full_path, content, memory_root, allow_split=False)
    except ValueError as e:
        return f"写入被拒绝: {e}"

    _trigger_extract(path)
    return f"已写入 {path}，后台提取已触发。"


@mcp.tool()
def memory_edit(path: str, old_string: str, new_string: str) -> str:
    """替换 .md 文件中的字符串。自动触发后台提取。不能编辑拆分目录。

    Args:
        path: 文件路径
        old_string: 要替换的原文
        new_string: 替换为的新文本
    """
    memory_root = _memory_root()
    full_path = _resolve_path(path)

    try:
        safe_edit(full_path, old_string, new_string, memory_root, allow_split=False)
    except ValueError as e:
        return f"编辑被拒绝: {e}"

    _trigger_extract(path)
    return f"已编辑 {path}，后台提取已触发。"


@mcp.tool()
def memory_list(path: str = "") -> str:
    """列出目录树。

    Args:
        path: 目录路径（空=memory_root）
    """
    target = _resolve_path(path) if path else _memory_root()

    if not os.path.isdir(target):
        return f"目录不存在: {path}"

    lines = []
    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in sorted(filenames):
            if f.endswith(".md"):
                rel = os.path.relpath(os.path.join(dirpath, f), target)
                lines.append(rel)

    return "\n".join(lines) if lines else "（空）"


@mcp.tool()
def memory_history(entity_name: str) -> str:
    """查询实体的历史版本（含已失效的）。

    Args:
        entity_name: 实体名称
    """
    history = db.get_node_history(entity_name)

    if not history:
        return f"未找到实体: {entity_name}"

    lines = []
    for h in history:
        status = "有效" if h["valid"] else f"已失效({h.get('invalid_reason', '?')})"
        lines.append(
            f"- [{status}] {h['name']} ({h['type']}): {h.get('summary', '')}\n"
            f"  created: {h['created_at']}, source: {h['source_path']}"
        )

    return "\n".join(lines)


@mcp.tool()
def memory_consistency_status() -> str:
    """查看一致性校验状态（待人工确认的矛盾项列表）。"""
    pending = db.get_pending_consistency()

    if not pending:
        return "无待确认项。"

    lines = []
    for p in pending:
        lines.append(
            f"- [{p['id'][:8]}] 旧: {p.get('old_source_path', '?')}\n"
            f"  新: {p.get('new_source_path', '?')}\n"
            f"  理由: {p['reason']} (置信度: {p.get('confidence', 0):.2f})"
        )

    return "\n".join(lines)


@mcp.tool()
def memory_consistency_resolve(log_id: str, action: str) -> str:
    """处理一致性校验待确认项。

    Args:
        log_id: 日志ID（可从 consistency_status 输出中截取前8位）
        action: "confirm"（确认旧事实失效）或 "dismiss"（忽略，不失效）
    """
    if len(log_id) < 32:
        pending = db.get_pending_consistency()
        match = [p for p in pending if p["id"].startswith(log_id)]
        if len(match) != 1:
            return f"无法唯一匹配 log_id: {log_id}（匹配到 {len(match)} 条）"
        log_id = match[0]["id"]

    if action == "confirm":
        log = db.conn.execute(
            "SELECT old_node_id FROM consistency_log WHERE id=?", (log_id,)
        ).fetchone()
        if log:
            db.invalidate_node(log["old_node_id"], "manual_confirmed")
        db.resolve_consistency(log_id, "manual_confirmed")
        return "已确认旧事实失效。"
    elif action == "dismiss":
        db.resolve_consistency(log_id, "manual_dismissed")
        return "已忽略此矛盾。"
    else:
        return f"未知操作: {action}（支持 confirm/dismiss）"


def main():
    parser = argparse.ArgumentParser(description="yacmemo MCP server")
    parser.add_argument("--config", default=None, help="Path to config.toml")
    parser.add_argument("--user", required=True,
                        help="User ID to serve (e.g. yachen, wife)")
    args = parser.parse_args()

    _init_for_user(args.config, args.user)
    logger.info("yacmemo MCP server starting for user '%s' (stdio transport)", args.user)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
