# yacmemo

带 API 级一致性守卫的个人记忆层 —— markdown 为本、全本地、agent 无关。

## 它是什么

yacmemo 让**你所有电脑上的所有 AI agent** 共享同一份长期记忆，且这份记忆是自洽的：

- **markdown 是唯一真相**——笔记就是你服务器上的普通文件：人可读、可 git、可 Obsidian。SQLite + LanceDB 只是派生索引，删掉随时可重建。
- **一个服务，所有设备**——唯一的服务进程跑在数据所在的机器上（streamable HTTP）。Claude Code、Codex、Cursor、自研 runtime……任何 MCP 客户端只需添加一个 URL，客户端零安装、零进程。
- **记忆子系统里没有生成式 LLM**——唯一的模型调用是 0.6B 的 embedding（~50ms）。结构靠约定产生，一致性靠确定性 API 守卫强制，模糊判断交给你的主模型在读取时完成。
- **WebUI 控制台**——浏览器打开 `/ui/`：笔记浏览/编辑（markdown 渲染）、在线搜索、人工审计裁决、调用留痕、健康总览；
- **一致性是被强制的，不是被希望的**——`memory_write` 拒绝近似重复标题，`memory_edit` 强制锚点唯一，矛盾在检索结果里带 ⚠ 标注并存呈现，系统永不静默删除或隐藏任何记忆。

## 架构

```
你的电脑们（任意 MCP agent）
   │  各端只加一个 URL，什么都不用装：
   │  http://debsvc.local:9721/yachen/mcp
   ▼
yacmemo-server（单进程，streamable HTTP，无状态会话）
   ├── /yachen/mcp → Store(root=.../yachen/memory)
   └── /user2/mcp   → Store(root=.../user2/memory)
         store.py      CRUD + 写路径守卫 + 同步索引
         search.py     FTS5 trigram + 向量，RRF 融合
         detectors.py  确定性 D1/D3 检测
         index_db.py   SQLite：元数据/FTS/冲突/守卫事件
         vector.py     LanceDB：笔记 + observation 向量
         embedding.py  唯一的模型调用（0.6B，~50ms）
   ▼
markdown 文件（source of truth，git 版本管理）
```

## 快速开始

### 服务端（数据所在机器）

```bash
git clone <your-repo> yacmemo && cd yacmemo
uv sync
cp config.example.toml config.toml   # 填 embedding 端点与各用户 root
uv run yacmemo-server --config config.toml
curl http://127.0.0.1:9721/health    # → {"status":"ok","users":["user2","yachen"]}
```

浏览器打开 `http://debsvc.local:9721/ui/` 就是自带的管理控制台（笔记 / 搜索 / 审计 / 使用记录 / 健康）。

### 客户端（你的每台电脑、每个 agent）

```
http://debsvc.local:9721/yachen/mcp
http://debsvc.local:9721/user2/mcp
```

```bash
# Claude Code
claude mcp add --transport http yacmemo http://debsvc.local:9721/yachen/mcp
# Codex CLI
codex mcp add yacmemo --url http://debsvc.local:9721/yachen/mcp
```

同机 agent 也可用 stdio：`uv run yacmemo-mcp --root /path/to/memory`。

**第一次用？**请先读 [用户使用手册](docs/07-user-guide.md)——上手、日常用法、常见问题都在里面。

## MCP 工具（8 个）

| 工具 | 用途 |
|---|---|
| `memory_search` | 混合检索（FTS trigram + 向量，RRF 融合）；疑似重复/矛盾内联 ⚠ 标注 |
| `memory_read` | 笔记全文 + 相关笔记（wiki-links + 语义近邻） |
| `memory_write` | 新建笔记；**近似重复标题直接拒绝**（force 需两级确认） |
| `memory_edit` | 就地更新，文本锚点必须唯一 |
| `memory_edit_section` | 按小节整段替换 |
| `memory_move` | 移动文件，索引跟随 |
| `memory_audit` | 自愈式一致性审计（外部改动/删除、D1/D2/D3、守卫统计） |
| `memory_list` | 目录树 / 最近变更 |

完整规格：[docs/02-mcp-tools.md](docs/02-mcp-tools.md)；使用约定（贴进 agent 系统提示）：[docs/01-architecture.md](docs/01-architecture.md) 第八节。

## 文档

| 文档 | 内容 |
|---|---|
| [07-user-guide.md](docs/07-user-guide.md) | **用户使用手册（从这里开始）** |
| [01-architecture.md](docs/01-architecture.md) | 设计、决策记录、原则 |
| [02-mcp-tools.md](docs/02-mcp-tools.md) | 工具规格 |
| [03-storage-and-search.md](docs/03-storage-and-search.md) | 文件格式、索引、混合检索、自愈 |
| [04-consistency.md](docs/04-consistency.md) | 三层防线、force 阶梯、指标 |
| [05-deployment.md](docs/05-deployment.md) | systemd、客户端配置、备份、安全 |
| [06-evaluation.md](docs/06-evaluation.md) | 检索基线与复测方法 |

v1（三层提取架构）冻结在 [`legacy/`](legacy/)，仅作决策记录。

## 技术栈

Python 3.11+ · mcp SDK（FastMCP）· SQLite（FTS5 trigram，WAL）· LanceDB · Qwen3-Embedding-0.6B（任意 OpenAI 兼容端点）· rapidfuzz。服务端单进程；无常驻后台任务、无队列、无 cron、无图数据库、无第二个 LLM。

## License

MIT
