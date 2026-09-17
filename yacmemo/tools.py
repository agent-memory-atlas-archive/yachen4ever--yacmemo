"""Shared MCP tool registrations for both transports (stdio and HTTP).

`register_tools` binds the memory tools to one user's store/searcher via
closures. Every call is logged to the server-level UsageDB (client UA/IP via
the FastMCP Context; StoreError refusals count as normal business outcomes,
unexpected exceptions as errors). The tool set, docstrings, and error
phrasing are identical across transports.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from mcp.server.fastmcp import Context, FastMCP

from .search import Searcher
from .store import Store, StoreError
from .usage import UsageDB, summarize_args

logger = logging.getLogger(__name__)


def _client_from_ctx(ctx: Context | None) -> tuple[str, str]:
    """Best-effort client identification from the HTTP request (UA, IP)."""
    try:
        req = ctx.request_context.request  # None on stdio transport
        if req is None:
            return "stdio", ""
        ua = req.headers.get("user-agent", "")
        ip = req.client.host if req.client else ""
        return ua or "unknown", ip
    except Exception:
        return "", ""


def register_tools(mcp: FastMCP, store: Store, searcher: Searcher,
                   usage: UsageDB | None = None, user_id: str = "local") -> None:
    """Register the 8 memory tools on an MCP server instance."""

    @contextmanager
    def _logged(tool_name: str, ctx: Context | None, summary: str, out: dict):
        t0 = time.monotonic()
        client, ip = _client_from_ctx(ctx)
        try:
            yield
        except Exception as e:
            out["ok"], out["error"] = False, str(e)
            raise
        finally:
            if usage:
                usage.log_call(user_id, tool_name, summary,
                               int((time.monotonic() - t0) * 1000),
                               ok=out["ok"], error=out["error"],
                               client=client, ip=ip)

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
    def memory_search(query: str, limit: int = 10, kind: str = "hybrid",
                      ctx: Context = None) -> str:
        """混合检索记忆（FTS + 语义向量）。结果带 ⚠ 标注表示存在疑似重复/矛盾，先合并再回答。

        Args:
            query: 查询文本（中文/英文均可；关键词式查询命中率更高）
            limit: 返回数量上限
            kind: "hybrid"（默认）/"fts"/"vector"
        """
        out = {"ok": True, "error": ""}
        with _logged("memory_search", ctx, summarize_args("memory_search", locals()), out):
            try:
                results = searcher.search(query, limit=limit, kind=kind)
                return _fmt_search(results)
            except StoreError as e:
                return f"{e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"搜索失败: {e}"

    @mcp.tool()
    def memory_read(path_or_title: str, ctx: Context = None) -> str:
        """读取笔记全文，附相关笔记（wiki-links + 语义近邻）。

        正文夹在 [正文开始]/[正文结束] 标记之间（逐字原文）；
        标记之后的"相关笔记"是工具附加信息，不是文件内容。

        Args:
            path_or_title: 相对路径或笔记标题
        """
        out = {"ok": True, "error": ""}
        with _logged("memory_read", ctx, summarize_args("memory_read", locals()), out):
            try:
                r = store.read(path_or_title)
            except StoreError as e:
                return f"读取失败: {e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"读取失败: {e}"

            # 正文与装饰必须边界清晰：附加段的语法不得模仿笔记自身结构
            #（曾经的 "## 相关笔记" 附加段被 agent 当成文件小节拿去当 edit 锚点，
            # 连续撞"未找到"后引发一整轮基础设施排查——2026-09-16 TeleAgent 实例）
            lines = [f"[正文开始 | {r['path']} | memory_edit 的 old_string 须从本块逐字复制]",
                     r["content"],
                     "[正文结束 | 以下相关笔记为工具附加信息，非文件内容]"]
            if r["related"]:
                lines.append("相关笔记：")
                for rel in r["related"]:
                    if rel.get("missing"):
                        lines.append(f"- [[{rel['title']}]]（目标不存在，可考虑创建或清理该链接）")
                    else:
                        note = f" — {rel['note']}" if rel.get("note") else ""
                        lines.append(f"- [[{rel['title']}]] ({rel['via']}){note}")
            return "\n".join(lines)

    @mcp.tool()
    def memory_write(title: str, content: str, force: bool = False,
                     force_confirm: bool = False, ctx: Context = None) -> str:
        """新建笔记（一篇一主题，标题即主题名）。近似标题会被拒绝；更新已有笔记请用 memory_edit。

        主题注册制硬约束：笔记必须属于已注册主题——写入 topics/<主题>/ 目录，
        或注册主题卡/相关路径覆盖的范围；journal/、archive/、curator/ 免注册区
        不受限。新主题先用 topic_register 注册（仅用户明确要求时），force 不豁免。

        Args:
            title: 笔记标题，可含目录前缀（如 "projects/yacmemo部署配置"）
            content: markdown 正文（首行建议 "# 标题"；事实行用 "- [类别] 内容"）
            force: 明确越过近似标题守卫（会被记录为违约指标）
            force_confirm: force 使用频繁（24h 内达阈值）时的人工确认二级开关
        """
        out = {"ok": True, "error": ""}
        with _logged("memory_write", ctx, summarize_args("memory_write", locals()), out):
            try:
                r = store.write(title, content, force=force,
                                force_confirm=force_confirm)
            except StoreError as e:
                return f"{e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"写入失败: {e}"
            note = "（注意：本次为 force 越过近似标题守卫，已记录）" if r["forced"] else ""
            return f"已写入并索引: {r['path']}{note}"

    @mcp.tool()
    def memory_edit(path: str, old_string: str, new_string: str,
                    ctx: Context = None) -> str:
        """就地修改笔记（唯一文本锚点替换）。这是更新事实的正确方式，不要新建重复笔记。

        Args:
            path: 笔记路径或标题
            old_string: 要替换的原文（必须在笔记中唯一）
            new_string: 替换后的文本
        """
        out = {"ok": True, "error": ""}
        with _logged("memory_edit", ctx, summarize_args("memory_edit", locals()), out):
            try:
                r = store.edit(path, old_string, new_string)
                return f"已修改并重新索引: {r['path']}"
            except StoreError as e:
                return f"{e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"编辑失败: {e}"

    @mcp.tool()
    def memory_edit_section(path: str, heading: str, new_content: str,
                            ctx: Context = None) -> str:
        """按 "## 标题" 替换整个小节（保留标题行，替换到下一个同级标题或文末）。

        Args:
            path: 笔记路径或标题
            heading: 小节标题（## 及更深层；一级标题是笔记本身，不可用）
            new_content: 新小节内容
        """
        out = {"ok": True, "error": ""}
        with _logged("memory_edit_section", ctx,
                     summarize_args("memory_edit_section", locals()), out):
            try:
                r = store.edit_section(path, heading, new_content)
                return f"已替换小节 '{r['heading']}' 并重新索引: {r['path']}"
            except StoreError as e:
                return f"{e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"编辑失败: {e}"

    @mcp.tool()
    def memory_move(path: str, new_path: str, ctx: Context = None) -> str:
        """移动笔记到新路径（标题不变，[[链接]] 按标题解析不受影响）。

        目标路径同样受主题注册制约束：不能移到未注册主题覆盖的范围之外。

        Args:
            path: 现有路径或标题
            new_path: 新路径（相对 memory_root）
        """
        out = {"ok": True, "error": ""}
        with _logged("memory_move", ctx, summarize_args("memory_move", locals()), out):
            try:
                r = store.move(path, new_path)
                return f"已移动: {r['old_path']} → {r['new_path']}"
            except StoreError as e:
                return f"{e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"移动失败: {e}"

    @mcp.tool()
    def memory_delete(path: str, ctx: Context = None) -> str:
        """删除笔记。仅在用户明确要求时调用（如"删掉 X"/"X 不用记了"）；git 历史可恢复。

        Args:
            path: 笔记路径或标题
        """
        out = {"ok": True, "error": ""}
        with _logged("memory_delete", ctx, summarize_args("memory_delete", locals()), out):
            try:
                r = store.delete_note(path)
                return f"已删除: {r['path']}（git 历史可恢复）"
            except StoreError as e:
                return f"{e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"删除失败: {e}"

    @mcp.tool()
    def memory_audit(ctx: Context = None) -> str:
        """全量一致性审计：外部变更自愈、标题重复、语义撞车、悬空链接、
        悬空主题卡、游离文件、守卫统计与 git 快照状态。"""
        out = {"ok": True, "error": ""}
        with _logged("memory_audit", ctx, "", out):
            try:
                r = store.audit()
            except StoreError as e:
                return f"{e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"审计失败: {e}"

            lines = []
            if r["added"]:
                lines.append(f"== 新发现文件（{len(r['added'])}，已建立索引）==")
                for p in r["added"]:
                    lines.append(f"- {p}")
            if r["resynced"]:
                lines.append(f"== 外部修改（{len(r['resynced'])}，已自动重建索引）==")
                for p in r["resynced"]:
                    lines.append(f"- {p}")
            if r["missing"]:
                lines.append(f"== 外部删除（{len(r['missing'])}，已清理索引）==")
                for p in r["missing"]:
                    lines.append(f"- {p}")
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
            cards = r.get("dangling_cards", [])
            lines.append(f"== 悬空主题卡（{len(cards)}）==")
            for cid in cards[:10]:
                lines.append(f"- {cid}")
            stray = r.get("stray", [])
            lines.append(f"== 游离文件（{len(stray)}）==")
            for p in stray[:10]:
                lines.append(f"- {p}")
            g = r["guard_stats"]
            lines.append(f"== 守卫统计 == 拒绝 {g['refused']} 次，force 越过 {g['forced']} 次，"
                         f"未覆盖拦截 {g['uncovered']} 次")
            lines.append(f"== git == {r.get('git', '')}")
            if r.get("audit_file"):
                lines.append(f"== 审计快照 == {r['audit_file']}")
            return "\n".join(lines)

    @mcp.tool()
    def memory_list(path: str = "", sort: str = "name", ctx: Context = None) -> str:
        """列出笔记目录树。

        Args:
            path: 子目录（空 = 根目录）
            sort: "name" 或 "mtime"（最近变更优先）
        """
        out = {"ok": True, "error": ""}
        with _logged("memory_list", ctx, summarize_args("memory_list", locals()), out):
            try:
                entries = store.list_notes(path, sort=sort)
            except StoreError as e:
                return f"{e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"列出失败: {e}"
            return "\n".join(entries) if entries else "（空）"

    # ------------------------------------------------------------ topics

    @mcp.tool()
    def topic_list(ctx: Context = None) -> str:
        """列出当前注册的长期记忆主题（活跃 + 已归档分组，附 abstract 位置）。"""
        out = {"ok": True, "error": ""}
        with _logged("topic_list", ctx, "", out):
            try:
                topics = store.load_topics()
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"读取失败: {e}"
            if not topics:
                return "尚无注册主题。用 topic_register 注册第一个（需用户明确要求）。"
            active = [t for t in topics if not t.get("archived")]
            archived = [t for t in topics if t.get("archived")]
            lines = [f"共 {len(active)} 个活跃主题："]
            for t in active:
                lines.append(f"- {t['title']} — {t['status']}")
                lines.append(f"    卡: {t['card']}")
            if archived:
                lines.append(f"\n已归档（{len(archived)} 个，检索仍可用、context 不再注入）：")
                for t in archived:
                    lines.append(f"- {t['title']} — {t['status']}")
            lines.append("（免注册区：journal/、archive/、curator/）")
            return "\n".join(lines)

    @mcp.tool()
    def topic_register(title: str, description: str = "", related: str = "",
                       ctx: Context = None) -> str:
        """注册一个新的长期记忆主题。仅在用户明确要求时调用（如"把 X 加入长期记忆"）。

        Args:
            title: 主题名（如 "notecalc"、"女儿教育"）
            description: 一句话现状描述（写入主题卡）
            related: 相关笔记路径，逗号分隔（可选）
        """
        out = {"ok": True, "error": ""}
        with _logged("topic_register", ctx,
                     summarize_args("topic_register", locals()), out):
            try:
                r = store.topic_register(title, description=description,
                                         related=related)
            except StoreError as e:
                return f"{e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"注册失败: {e}"
            return (f"已注册主题「{r['title']}」，abstract: {r['card']}。"
                    f"该主题后续的笔记写入主题卡所在目录；现状变化就地更新 abstract。")

    @mcp.tool()
    def topic_unregister(title: str, ctx: Context = None) -> str:
        """注销一个长期记忆主题（仅在用户明确要求时调用，如"X 不用长期记录了"）。
        仅移出注册表，笔记文件一律不动；归档语义请用 archive_topic。

        Args:
            title: 主题名（与 topic_list 中一致）
        """
        out = {"ok": True, "error": ""}
        with _logged("topic_unregister", ctx, f"title={title}", out):
            try:
                r = store.topic_unregister(title)
            except StoreError as e:
                return f"{e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"注销失败: {e}"
            return (f"已注销主题「{r['title']}」：注册表已移除，笔记文件未动。"
                    f"原 abstract: {r['card'] or '（未记录）'}。"
                    f"相关笔记现为游离文件（审计会点名），请与用户确认后用 "
                    f"memory_move 归位 archive/，或明确确认后用 memory_delete 删除。")

    @mcp.tool()
    def archive_topic(title: str, ctx: Context = None) -> str:
        """归档主题（仅在用户明确要求时调用，如"X 归档吧"）：abstract 移入 archive/，
        注册表标记为已归档——检索仍可用，memory_context 不再注入，不计游离。

        Args:
            title: 主题名（与 topic_list 活跃列表中一致）
        """
        out = {"ok": True, "error": ""}
        with _logged("archive_topic", ctx, f"title={title}", out):
            try:
                r = store.archive_topic(title)
                return (f"已归档主题「{r['title']}」：abstract 移至 {r['card'] or '（原无卡）'}，"
                        f"注册表已标记为已归档；检索仍可用，context 不再注入。")
            except StoreError as e:
                return f"{e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"归档失败: {e}"

    # ------------------------------------------------------------ profile

    @mcp.tool()
    def get_user_preference(section: str = "", ctx: Context = None) -> str:
        """读取用户画像与偏好（PROFILE.md，记忆层功能而非主题记忆）。返回全文或指定小节。

        Args:
            section: 小节名（如"材料与文档偏好"）；空 = 返回全文
        """
        out = {"ok": True, "error": ""}
        with _logged("get_user_preference", ctx, f"section={section}", out):
            try:
                return store.get_preference(section)
            except StoreError as e:
                return f"{e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"读取失败: {e}"

    @mcp.tool()
    def update_user_preference(section: str, content: str,
                               ctx: Context = None) -> str:
        """创建或替换用户画像/偏好的一个小节（agent 加以维护；写提炼结论，不贴对话原文）。

        Args:
            section: 小节名（如"沟通风格"、"材料与文档偏好"）
            content: 小节内容（事实行用 "- [类别] 内容" 语法）
        """
        out = {"ok": True, "error": ""}
        with _logged("update_user_preference", ctx, f"section={section}", out):
            try:
                r = store.update_preference(section, content)
                where = "新建小节" if r.get("created") else "替换小节"
                return f"已更新 PROFILE.md（{where}）: {r['section']}"
            except StoreError as e:
                return f"{e}"
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"更新失败: {e}"

    @mcp.tool()
    def memory_context(ctx: Context = None) -> str:
        """返回核心记忆上下文：主题注册表 + 各主题卡摘要头。每次会话开始时先调用一次。"""
        out = {"ok": True, "error": ""}
        with _logged("memory_context", ctx, "", out):
            try:
                return store.memory_context()
            except Exception as e:
                out["ok"], out["error"] = False, str(e)
                return f"读取失败: {e}"
