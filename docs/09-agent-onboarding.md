# Agent 接入指引（AI 专用）

> 如果你是 AI agent（Claude Code / Codex / Cursor / 自研 runtime……）并读到了本文，请按顺序完成下面的接入自检，并把结果明确告诉用户。本文是 README 的展开版；接入方式的完整说明见 [08-agent-config.md](08-agent-config.md)。

## ① 自检：你是否已配置 yacmemo MCP？

- **看工具**：你的可用工具里有 `memory_search` / `memory_read` / `memory_write` / `memory_context` 等 yacmemo 工具 → **已接入**，直接跳到 ③；
- **看配置**：Claude Code 跑 `claude mcp list`；Codex 跑 `codex mcp list`；Cursor 看 `.cursor/mcp.json`；其他客户端查各自的 MCP 设置。

## ② 未接入：引导用户完成配置

向用户说明"yacmemo 是多端共享的长期记忆层"，并向用户确认其身份对应的**用户ID**（如 `yachen`），然后给出对应命令（替换 `<用户ID>`）：

```bash
# Claude Code
claude mcp add --transport http yacmemo http://debsvc.local:9721/<用户ID>/mcp
# Codex CLI
codex mcp add yacmemo --url http://debsvc.local:9721/<用户ID>/mcp
# 其他 MCP 客户端：添加 streamable HTTP 端点 http://<服务器>:9721/<用户ID>/mcp
```

配置完成后提示用户**重启会话或重连 MCP**，之后回到 ① 自检即可走通。同机 stdio 方式与端点清单见 [08-agent-config.md](08-agent-config.md) 与 [05-deployment.md](05-deployment.md) §2.1。

## ③ 已接入：把记忆逻辑织入工作流，并向用户展示状态

1. **会话开始**：先调 `memory_context` 冷启动回顾，然后向用户展示一行接入摘要，例如：

   > ✅ 已接入 yacmemo 记忆层（用户：yachen）——画像偏好 3 条；活跃主题 12 个：《yacmemo部署配置》《备份策略》……；最近审计无待处理问题。

2. **日常遵循记忆纪律**（完整约定见 [01-architecture.md §八](01-architecture.md)，工具规格见 [02-mcp-tools.md](02-mcp-tools.md)）：

   - 回答事实性问题前先 `memory_search`；结果带 ⚠ 时先读两篇、用 `memory_edit` 合并，然后再回答；
   - 写入先查重：已有同主题笔记用 `memory_edit` / `memory_edit_section` **就地更新**，不新建重复笔记；
   - **长期记忆只写注册主题目录内**——新主题须请用户明确授权后 `topic_register`（越界写入会被硬拦截，`force` 不豁免）；流水账放 `journal/`；
   - 事实行用 observation 语法：`- [配置] 服务端口为 9721`；
   - 注册 / 注销 / 归档主题、删除笔记：**仅在用户明确要求时执行**。

3. **不确定就问**：找不到该写进哪个主题、或对记忆内容有疑问，向用户说明而不是猜测。

## ④ 桌面 agent 附加项

TeleAgent 类自带本地记忆文件（USER.md / MEMORY.md）的 agent：本地文件只存指针、不存事实副本，模板与维护约定见 [08-agent-config.md](08-agent-config.md) §二。
