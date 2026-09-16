# yacmemo 三层架构总览

> 2026-09-13
> 状态：已实现，~1500 行 Python，148 个测试通过

## 一、问题背景

### 1.1 Agent 跨 session 失忆

AI Agent 的每次对话都是独立的——上一次 session 里做过什么、记了什么，下一次 session 完全不知道。如果不对记忆做持久化，Agent 就是一个"每天都在失忆"的助手。

一个典型场景：

- **T1**（session 1）：Agent 配置了 yacmemo 服务，端口写了 8080。它通过 MCP 工具把这个事实写到 `.md` 文件。
- **T2**（session 2）：Agent 因为性能需要，把端口改成了 9721。它在 `.md` 里追加了新配置。但 Agent 不记得去更新旧记录——它跨 session 是失忆的。
- **T3**（session 3）：Agent 搜索记忆"yacmemo 的端口是多少"，向量搜索返回了旧的 8080 和新的 9721 两条记录，Agent 不知道哪个是当前有效的。

这不是 Agent 不够聪明，是架构上缺一层"记忆维护"——没有人回头检查旧记忆和新事实是否矛盾。

### 1.2 之前用 OpenViking 的三个痛点

我们之前用 OpenViking（OV）做 TeleAgent 的记忆层，暴露了三个结构性问题：

**痛点 1：提取阻塞**

OV 的 `remember` 操作用 VLM 对整个 session 历史做记忆提取，每条 90 秒，同步阻塞。如果 VLM 调用失败，后续所有写入都被卡住。Agent 完成了工作却无法把结论存下来——"想记"比"做了"还慢。

**痛点 2：跨 session 矛盾**

OV 没有一致性校验机制。Agent 在 T1 记录端口=8080，T2 改成 9721 但没想起来更新旧记录，T3 搜到两条矛盾的事实却无法判断。记忆越多，矛盾越多，Agent 越不可靠。

**痛点 3：设计意图偏移**

OV 的设计路径是"存对话原文 → VLM 提取 → 语义搜索提取后的记忆"。但我们的实际需求是"Agent 自己推理好结论 → 写结论 → 索引可搜"。我们把 OV 的文件系统副产品当记忆主力，偏离了它的设计意图，用得别扭。

---

## 二、核心洞察

### 2.1 谁来推理？Agent 还是独立 LLM？

这是整个设计最关键的决策点。调研了 7 个记忆工具后，发现业界存在两条路线的分歧：

| | Agent 推理 | 独立 LLM 推理 |
|---|---|---|
| **优势** | 有完整对话上下文、知道什么值得存、实时不额外花时间 | 能做跨条目一致性校验（Agent 跨 session 失忆做不到） |
| **劣势** | 跨 session 失忆，看不到记忆全貌 | 只看到原文，没有 Agent 的理解上下文，提取的东西可能偏离 |

### 2.2 我们的判断

**对于单条记忆写入，Agent 推理 > 独立 LLM 推理。**

Agent 在对话中已经理解了发生了什么、结论是什么。它写的是"推理后的结论"，不是"对话原文"。独立 LLM 再从原文提取一遍，不仅浪费时间，还可能偏离 Agent 的理解。

**独立 LLM 的正确角色不是"提取者"，而是"维护者"。**

Agent 跨 session 失忆，做不了跨条目的一致性校验——这是独立 LLM 能做而 Agent 做不了的事。所以独立 LLM 的核心价值在于"回头检查旧记忆和新事实是否矛盾"，而不是"从原文中提取新事实"。

### 2.3 折中：Agent 提炼过的 .md 仍需 LLM 拆分

Agent 写的 .md 是自由格式，一个文件可能混合多个事项（比如一篇工作日志同时记了端口变更、依赖升级、bug 修复）。Layer 2 的独立 LLM 做的不是"从对话提取结论"（Agent 已经做了），而是"把 Agent 写的多事项文件按独立事项拆分为细粒度文件"——这是结构化整理，不是推理。

---

## 三、三层架构详解

### 3.1 架构全景

```
Layer 1: Agent 推理 → 写详尽工作记录 .md
         Agent 在对话中已理解了什么发生了、什么结论，直接写结论而非存对话原文
         → 磁盘 .md 文件，可 grep / 编辑 / git / Obsidian

Layer 2: 独立 LLM 从 .md 中拆分提取 entity/event + provenance 指针
         不是从 session 原文提取——Agent 已经提炼过了
         → 拆分文件（细粒度 .md）+ SQLite（结构化事实）+ LanceDB（向量索引）

Layer 3: 独立 LLM 一致性校验
         扫描全部记忆，发现矛盾，标记旧事实失效（不删除，保留历史）
         → SQLite（valid=0 + invalid_at + consistency_log）
```

### 3.2 Layer 1：Agent 推理 → 写工作记录 .md

**谁做**：Agent（主模型，如 chat-pro）

**做什么**：
- Agent 在对话中完成了工作后，把推理结论、操作细节、关键决策写入 `.md` 文件
- 写入通过 MCP 工具 `memory_write` / `memory_edit` 完成（`mcp_server.py:178` / `mcp_server.py:199`）
- 写入后自动触发后台提取（`mcp_server.py:83` `_trigger_extract` 通过 HTTP webhook 异步通知 enhancer）

**不做什么**：
- 不存对话原文（已经推理过，存原文浪费）
- 不直接操作 SQLite/LanceDB（只写 .md，由 Layer 2 去索引）
- 不写拆分目录（`safe_write` / `safe_edit` 在 `fs_utils.py:71` / `fs_utils.py:96` 中用 `is_in_split_dir` 拦截）

**关键约束**：Agent 写的 .md 是自由格式，没有 frontmatter 要求，人类可直接阅读编辑。

### 3.3 Layer 2：独立 LLM 拆分提取

**谁做**：独立 LLM（本地 Ling-3.0-tiny-MLX-4bit，7.9B/1.3B MoE）

**做什么**：
- 读取 Agent 写的 .md 原文，按独立事项拆分为细粒度文件，放在原文旁的同名目录下
- 为每个拆分文件提取 entity（实体）和 event（事件），写入 SQLite
- 对每个 entity/event 生成 embedding 向量，写入 LanceDB
- 记录 provenance 指针：entity 的 `source_path` 指向拆分文件，拆分文件 frontmatter 的 `source` 指向原文

**不做什么**：
- 不从 session 对话原文提取（Agent 已经推理过了，Layer 2 只处理 Agent 写的 .md）
- 不判断事实是否矛盾（那是 Layer 3 的事）
- 不删除/修改 Agent 写的原文 .md

**触发时机**（两个入口，详见 `enhancer.py`）：
- **实时触发**：Agent 通过 MCP 写 .md 后，`_trigger_extract` 发 HTTP POST 到 enhancer 的 `/trigger` 端点（`enhancer.py:188`），异步处理
- **定时触发**：APScheduler cron 定时扫描所有 .md 文件（默认每 6 小时，`config.py:36` `extract_cron = "0 */6 * * *"`），对内容 hash 变了的文件重新提取

**核心代码路径**：

```
Extractor.process_file()              # extractor.py:71 — 入口
  → 读取 .md 原文，计算 content_hash   # fs_utils.py:9 content_hash
  → 查 processed_files 表，hash 没变则跳过  # db.py:321 get_processed_file
  → 调 LLM 拆分+提取 (chat_json)       # llm.py:90 chat_json
  → 写拆分文件（hash 校验 + 冲突保护）  # extractor.py:227 _write_split_files
  → 删旧 entity/event/vector          # db.py:258/279/304 + vector.py:108
  → 写新 entity → SQLite nodes 表      # db.py:202 upsert_node
  → 写新 event → SQLite events 表      # db.py:286 upsert_event
  → 生成 embedding → LanceDB           # embedding.py:42 embed_one + vector.py:69/73
  → 记录 processed_files               # db.py:311 record_processed_file
```

### 3.4 Layer 3：独立 LLM 一致性校验

**谁做**：独立 LLM（同一个本地小模型，关 thinking 模式以提速）

**做什么**：
- 对每个新提取的 entity，用向量搜索找语义相似的旧 entity
- 调 LLM 判断新旧事实是否矛盾
- 高置信度（≥ 0.8）：自动标记旧事实失效（`valid=0`，`invalid_at` 记时间，`invalid_reason` 记原因）
- 低置信度（< 0.8）：记日志等人工确认
- 全程不删除任何数据——旧事实保留在 SQLite 中，可查历史

**不做什么**：
- 不提取新记忆（那是 Layer 2 的事）
- 不直接改 .md 文件（只改 SQLite 中的状态标记）
- 不自动恢复误判（失效后只能人工处理）

**触发时机**（两个入口）：
- **实时校验**：Layer 2 提取完每个新 node 后，立即调 `check_new_node`（`consistency.py:35`）
- **定时全量扫描**：cron 默认每天凌晨 3 点（`config.py:37` `consistency_cron = "0 3 * * *"`），调 `check_all` 遍历所有 valid nodes

### 3.5 三层之间的关系

**source of truth 是 .md 文件**。SQLite 和 LanceDB 都是 .md 的派生索引。如果 SQLite 和 LanceDB 全删了，从 .md 可以全量重建——运行 `yacmemo-enhancer --init` 即可。但反过来不行：.md 删了，记忆就没了。

**三层是流水线，不是对等关系**：

```
Layer 1 (.md 原文)
  ↓  Agent 写入后触发
Layer 2 (拆分文件 + SQLite + LanceDB)
  ↓  提取完每个 entity 后触发
Layer 3 (SQLite 状态更新)
```

**Layer 2+3 是增强层，失败不影响 Layer 1**。如果 LLM 不可用、embedding 服务挂了、LanceDB 损坏了——Agent 仍然可以正常读写 .md 文件。记忆"存在"不依赖 Layer 2+3，只是"可搜索"和"自一致"依赖它们。

---

## 四、设计原则

### 原则 1：.md 文件是 source of truth

SQLite 存的是结构化事实索引（nodes/edges/events 表），LanceDB 存的是向量索引。两者都是从 .md 派生出来的，删了可以重建。

- 备份就是复制 .md 文件（包括拆分目录），不需要备份数据库
- 重建命令：`uv run yacmemo-enhancer --config config.toml --init`（`enhancer.py:279`）
- `processed_files` 表通过 content_hash 比对（`db.py:96`），重建时只处理有内容的文件

**为什么不用数据库做 source of truth**：.md 人类可读可编辑，可 git 版本控制，可用 Obsidian 浏览。数据库不行。

### 原则 2：Layer 2+3 是增强层，失败不影响 Layer 1

整个管道的设计哲学是"尽力而为"——Layer 2 提取失败、Layer 3 校验失败，都不阻塞 Agent 的写入操作。

| 失败场景 | 影响 | 代码处理 |
|---|---|---|
| LLM 提取失败 | 拆分文件和索引不更新，但 .md 已写入 | `extractor.py:106` 记录 failed 状态，返回 ExtractResult(status="failed") |
| Embedding 调用失败 | 该 entity 没有向量，无法语义搜索，但 SQLite 记录在 | `extractor.py:158` 记 warning，继续处理下一个 |
| 一致性校验 LLM 失败 | 跳过该节点的校验，不阻塞 | `consistency.py:127` 返回 `{contradictory: False}` |
| LanceDB 损坏 | 语义搜索不可用，但 grep 和 read 仍可用 | `vector.py` 各方法 catch Exception，记 warning |

MCP 的 `memory_write` / `memory_edit` 在写入 .md 后异步触发提取（`mcp_server.py:83` `_trigger_extract`），不等提取完成就返回成功。提取是后台线程做的事（`enhancer.py:220` `threading.Thread(target=_do, daemon=True)`）。

### 原则 3：provenance 两层指针

每个提取出来的 entity/event 都有可追溯的来源指针，分两层：

```
SQLite entity 记录
  └─ source_path → 拆分文件 (如 "01-数据门户/feat-a.md")
       └─ 拆分文件 frontmatter 的 source 字段 → 原文 (如 "01-数据门户.md")
```

**实现**：
- `db.py:43` `source_path` 字段：指向拆分文件的相对路径
- `db.py:45` `original_path` 字段：指向原文 .md 的相对路径（冗余存储，方便直接跳到原文）
- `extractor.py:252` 写拆分文件时在 frontmatter 中写入 `source: {原文件名}`

**为什么两层而不是一层直接指向原文**：
- 拆分文件是细粒度的——一个原文可能拆出 5 个拆分文件，每个对应一个独立事项。直接指向原文粒度太粗，Agent 搜到一个 entity 还得读整个原文去找相关段落
- 拆分文件有结构——含 frontmatter（source/created/updated）+ 正文 + 关联实体 + 事件，比原文更聚焦
- 搜索时先看 SQLite 里的 summary（摘要），需要细节时 Read 拆分文件，需要完整上下文时 Read 原文

**贯穿示例**：Agent 在 session 1 写了 `resources/projects/yacmemo-deploy.md`，记录了端口配置、依赖安装、systemd 配置三件事。Layer 2 的 LLM 拆分为 `yacmemo-deploy/port-config.md`、`yacmemo-deploy/deps-install.md`、`yacmemo-deploy/systemd-setup.md`。`port-config.md` 的 frontmatter 里 `source: yacmemo-deploy.md`。SQLite 里 entity "yacmemo 服务端口" 的 `source_path = "resources/projects/yacmemo-deploy/port-config.md"`，`original_path = "resources/projects/yacmemo-deploy.md"`。

### 原则 4：一致性校验不删除旧事实，标记 invalid_at

旧事实被发现矛盾后，不删除，只标记失效：

```sql
-- db.py:228 invalidate_node
UPDATE nodes SET valid=0, invalid_at=<now>, invalid_reason='superseded_by:<new_id>: <reason>'
WHERE id=<old_id> AND user_id=<user_id>
```

**为什么不删**：
- **保留完整历史**：`get_node_history(user_id, name)`（`db.py:247`）返回同名实体的所有版本，包括已失效的，可查演变轨迹
- **可追溯**：每条失效记录都有 `invalid_at`（何时失效）和 `invalid_reason`（为什么失效，含 `superseded_by:{new_id}` 指向取代它的新事实）
- **可恢复**：如果 LLM 误判了，数据没丢，人工可处理

这是借鉴 Graphiti 的 temporal invalidation 思路：事实有时态，新事实不否定旧事实的存在，只否定它的有效性。

**贯穿示例**：Agent 在 T1 写了"端口是 8080"，Layer 2 提取出 entity `name="yacmemo 服务端口", summary="端口为 8080"`。T2 Agent 改成 9721，Layer 2 提取出新 entity `name="yacmemo 服务端口", summary="端口改为 9721"`。Layer 3 的向量搜索发现新旧两个 entity 语义相似，LLM 判断矛盾（confidence=0.95），自动将旧 entity 标记 `valid=0, invalid_reason="superseded_by:<new_id>: 端口从 8080 改为 9721，新事实是当前有效配置"`。T3 搜索时只返回 `valid=1` 的记录，Agent 拿到的是 9721。如果用户想看历史，`memory_history("yacmemo 服务端口")` 返回两个版本。

### 原则 5：半自动校验

一致性校验不是全自动也不是全人工，而是分置信度处理（`consistency.py:76`）：

```python
auto = (confidence >= self.config.consistency.confidence_threshold  # 默认 0.8
        and self.config.consistency.auto_invalidate)                 # 默认 True

if auto:
    self.db.invalidate_node(user_id, match_id, f"superseded_by:{node_id}: {reason}")
    # status = "auto_invalidated"
else:
    # status = "pending"，等人工确认
```

| 置信度 | 处理方式 | consistency_log status |
|---|---|---|
| ≥ 0.8 且 auto_invalidate=True | 自动标记旧节点失效 | `auto_invalidated` |
| < 0.8 | 不失效，只记日志等人工确认 | `pending` |

**为什么不全自动**：LLM 可能误判。典型误判场景：
- **不同实体的同名属性**："服务器端口是 8080"和"数据库端口是 9721"——都含"端口"但指不同东西，向量相似但不是矛盾
- **补充而非矛盾**："服务器端口是 8080"和"服务器还配了 SSL 证书"——语义相关但不是矛盾
- **时间维度**："1 月端口是 8080"和"9 月端口是 9721"——是时间演进不是矛盾

高置信度时 LLM 比较确定，自动处理没问题。低置信度时让人工看一眼更安全。

人工审核的负担控制：低置信度的矛盾只在 `consistency_log` 表里记一条 pending 记录（`db.py:329` `add_consistency_log`），不阻塞任何流程。用户/Agent 通过 `memory_consistency_status` MCP 工具（`mcp_server.py:266`）或 WebUI 一致性看板查看和处理。

**贯穿示例**：端口从 8080 改为 9721，LLM 判断 contradiction=true, confidence=0.95 → 自动失效旧记录。如果是"服务器端口是 8080"和"数据库端口是 9721"，LLM 可能判断 contradiction=false（不矛盾），或者 contradiction=true 但 confidence=0.5（不确定）→ 记 pending，等人工确认。用户通过 `memory_consistency_resolve(log_id, "confirm")` 确认，或 `memory_consistency_resolve(log_id, "dismiss")` 忽略。

### 原则 6：轻量基础设施

**选了 SQLite + LanceDB，不引入图数据库。**

| 组件 | 选择 | 理由 |
|---|---|---|
| 关系存储 | SQLite (WAL 模式) | 零配置、单文件、ACID 事务、跨平台 |
| 向量索引 | LanceDB | 嵌入式向量数据库、无需独立服务、与 Python 原生集成 |
| 调度 | APScheduler (cron) | 内置库、无外部依赖 |
| LLM 推理 | 本地 mlx-serve | 数据不出局域网 |
| Embedding | 本地 omlx | 同上 |

**为什么不用图数据库**：
- 数据规模小：几百文件、几千节点/边，SQLite 绰绰有余
- 查询模式简单：一跳查询（搜 entity → 沿 provenance 跳到源文件），不需要多跳遍历
- 运维成本：SQLite 零配置 vs Neo4j/FalkorDB 需独立服务
- 数据模型不变，未来可迁移（只换存储后端）

---

## 五、数据流总览

### 5.1 Agent 写入 → 提取 + 校验的完整流程

用"端口从 8080 改为 9721"这个场景贯穿：

```
Agent (session 2)
  │ memory_edit("yacmemo-deploy.md", "端口为 8080", "端口改为 9721")
  │
  ▼
MCP Server (mcp_server.py:199 memory_edit)
  │ safe_edit() 写入 .md 原文
  │ _trigger_extract() → HTTP POST /trigger {action: "extract", path: "...", user_id: "yachen"}
  │ ← 立即返回 "已编辑，后台提取已触发"
  │                                    (不等提取完成)
  │
  ▼
Enhancer (enhancer.py:188 /trigger 端点)
  │ 收到 webhook，启动后台线程
  │
  ▼
Layer 2: Extractor.process_file() (extractor.py:71)
  │ 1. 读取 .md 原文，计算 content_hash
  │ 2. 查 processed_files → hash 变了 → 重新提取
  │ 3. 调 LLM: "把这段工作记录按独立事项拆分，提取 entity 和 event"
  │    → LLM 返回 JSON: {files: [{name: "port-config", entities: [{name: "yacmemo 服务端口", summary: "端口改为 9721"}], ...}]}
  │ 4. 写拆分文件 yacmemo-deploy/port-config.md (hash 校验 → 安全覆盖)
  │ 5. 删旧 entity/event/vector (source_path 匹配的)
  │ 6. 写新 entity → SQLite nodes 表
  │    upsert_node(name="yacmemo 服务端口", summary="端口改为 9721",
  │               source_path="yacmemo-deploy/port-config.md",
  │               original_path="yacmemo-deploy.md")
  │ 7. 生成 embedding → LanceDB node_vectors 表
  │ 8. 记录 processed_files (status="incremental")
  │
  ▼
Layer 3: ConsistencyChecker.check_new_node() (consistency.py:35)
  │ 1. 取出新 node: name="yacmemo 服务端口", summary="端口改为 9721"
  │ 2. embed_one("yacmemo 服务端口: 端口改为 9721") → 1024 维向量
  │ 3. vector.search_nodes(vec, limit=5) → 找到旧 node "yacmemo 服务端口: 端口为 8080"
  │ 4. _judge(old, new) → LLM 判断:
  │    旧事实: 服务器配置: 端口为 8080 (记录时间: 2026-09-10)
  │    新事实: 服务器配置: 端口改为 9721 (记录时间: 2026-09-13)
  │    → {contradictory: true, current: "B", reason: "端口从 8080 改为 9721",
  │       confidence: 0.95}
  │ 5. confidence=0.95 >= 0.8 → 自动失效
  │    invalidate_node(old_id, "superseded_by:<new_id>: 端口从 8080 改为 9721")
  │ 6. add_consistency_log(auto_invalidated=True, status="auto_invalidated")
  │
  ▼
完成。T3 搜索时：
  memory_search("yacmemo 端口")
  → vector.search_all() 返回 valid=1 的 node (端口 9721)
  → 旧 node (端口 8080) valid=0，不返回
```

### 5.2 Agent 搜索记忆的完整流程

```
Agent (session 3)
  │ memory_search("yacmemo 服务的端口配置", limit=10)
  │
  ▼
MCP Server (mcp_server.py:104 memory_search)
  │
  ▼
EmbeddingClient.embed_one("yacmemo 服务的端口配置")
  │ → 调 omlx API (m2ultra:11235/v1/embeddings)
  │ → 返回 1024 维查询向量
  │
  ▼
VectorStore.search_all(query_emb, limit=10) (vector.py:87)
  │ 在 LanceDB 三张表 (node_vectors / event_vectors / edge_vectors) 中搜索
  │ 合并结果，按 _distance 升序排序（越小越相似）
  │ 返回 top-10: [{kind: "node", text: "yacmemo 服务端口: 端口改为 9721",
  │              source_path: "yacmemo-deploy/port-config.md", _distance: 0.03}, ...]
  │
  ▼
MCP Server 返回格式化文本:
  │ 1. [node] yacmemo 服务端口: 端口改为 9721
  │    source: yacmemo-deploy/port-config.md (distance: 0.0300)
  │ 2. [event] 2026-09-13 配置变更: 端口从 8080 改为 9721
  │    source: yacmemo-deploy/port-config.md (distance: 0.0850)
  │ ...
  │
  ▼
Agent 拿到搜索结果
  │ 看到 source_path = "yacmemo-deploy/port-config.md"
  │ → memory_read("yacmemo-deploy/port-config.md") 读取拆分文件
  │ → 如需完整上下文: memory_read("yacmemo-deploy.md") 读取原文
  │
  ▼
Agent 回答用户："yacmemo 服务的端口是 9721。"
```

### 5.3 定时全量扫描流程

```
每天 03:00 (consistency_cron)
  │
  ▼
scan_all(config, db) (enhancer.py:179)
  │ 对每个 user:
  │   scan_user(config, user, db) (enhancer.py:152)
  │     1. list_md_files(memory_root) → 所有 .md 原文 (排除拆分目录)
  │     2. 对每个 .md: extractor.process_file()
  │        → content_hash 没变 → skip
  │        → content_hash 变了 → 重新提取
  │     3. checker.check_all(user_id)
  │        → get_all_valid_nodes → 对每个 node 调 check_new_node
  │     4. check_split_integrity(config, user, db) (enhancer.py:123)
  │        → 检查拆分文件 hash 是否被手动改
  │        → 文件是否存在 (stale 检测)
  │
  ▼
同时，extract_cron (每 6 小时) 也调 scan_all
  → 主要做增量提取（hash 变了的文件重新提取）
  → 顺带做一致性校验
```

---

## 六、部署架构

### 6.1 物理拓扑

```
┌─────────────────────────────────────────────────────────────┐
│                        m2ultra (192.168.5.2)                │
│                        Apple M2 Ultra, 64GB                 │
│  ┌─────────────────┐    ┌─────────────────┐                 │
│  │  mlx-serve      │    │  omlx           │                 │
│  │  :11234/v1      │    │  :11235/v1      │                 │
│  │  Ling-3.0-tiny  │    │  Qwen3-Embed    │                 │
│  │  4bit MoE       │    │  0.6B 4bit DWQ  │                 │
│  │  (LLM 推理)      │    │  (Embedding)    │                 │
│  └────────┬────────┘    └────────┬────────┘                 │
│           │                      │                          │
└───────────┼──────────────────────┼──────────────────────────┘
            │ HTTP (局域网)          │ HTTP (局域网)
            │                      │
┌───────────┼──────────────────────┼──────────────────────────┐
│           ▼                      ▼                          │
│  debsvc (192.168.5.7:23333)                                  │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  yacmemo-enhancer (systemd service)                    │  │
│  │  FastAPI + uvicorn  :9721                              │  │
│  │  ├── /trigger        (webhook: 提取+校验)               │  │
│  │  ├── /health         (健康检查)                         │  │
│  │  └── /admin/*        (WebUI: Vue 3 + Naive UI SPA)      │  │
│  │  APScheduler: extract_cron / consistency_cron           │  │
│  │                                                        │  │
│  │  数据存储:                                              │  │
│  │  ├── data/system.db      (SQLite, 系统级, 多用户共享)   │  │
│  │  ├── data/lancedb/yachen/ (LanceDB, per-user 子目录)    │  │
│  │  ├── data/lancedb/wife/   (LanceDB, per-user 子目录)    │  │
│  │  ├── data/yachen/memory/  (.md 原文 + 拆分文件)         │  │
│  │  └── data/wife/memory/    (.md 原文 + 拆分文件)         │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  yacmemo-mcp --user yachen (stdio)                           │
│  yacmemo-mcp --user wife   (stdio)                           │
│  → 各自注册为 TeleAgent 的 MCP server                        │
│  → Agent 通过 stdio 与 MCP server 通信                       │
│  → MCP server 读 .md / 查 SQLite / 搜 LanceDB               │
│  → 写 .md 后通过 HTTP webhook 通知 enhancer 触发提取          │
└──────────────────────────────────────────────────────────────┘
```

### 6.2 组件职责

| 组件 | 部署位置 | 职责 | 启动命令 |
|---|---|---|---|
| yacmemo-enhancer | debsvc (systemd) | Webhook 接收 + cron 定时扫描 + WebUI 服务 | `uv run yacmemo-enhancer --config config.toml` |
| yacmemo-mcp | debsvc (TeleAgent MCP 注册) | Agent 交互入口，9 个 MCP 工具 | `uv run yacmemo-mcp --user yachen` |
| mlx-serve | m2ultra (:11234) | LLM 推理（提取 + 校验） | 独立服务 |
| omlx | m2ultra (:11235) | Embedding 生成 | 独立服务 |
| SQLite | debsvc (本地文件) | 结构化事实存储 | 嵌入式，无需启动 |
| LanceDB | debsvc (本地目录) | 向量索引 | 嵌入式，无需启动 |

### 6.3 多用户隔离

系统级 SQLite（`data/system.db`）是共享的，所有业务表都带 `user_id` 列做隔离（`db.py:38` nodes 表、`db.py:59` edges 表、`db.py:79` events 表等）。每个用户有独立的：
- `memory_root`：.md 文件目录（如 `data/yachen/memory/` 和 `data/wife/memory/`）
- LanceDB 子目录：`data/lancedb/{user_id}/`
- LLM/embedding API key 可按用户覆盖（`config.py:92` `resolve_llm`、`config.py:107` `resolve_embedding`）

MCP server 启动必须指定 `--user <id>`（`mcp_server.py:361`），只服务该用户的记忆。Enhancer 的 webhook 和 cron 扫描所有用户（`enhancer.py:179` `scan_all`）。

---

## 七、为什么不直接用现成工具

### 7.1 能力矩阵

| 能力 | OpenViking | Mem0 | Cognee | Graphiti | EverOS | **yacmemo** |
|---|---|---|---|---|---|---|
| .md 文件系统 | 有 | 无 | 无 | 无 | 有 | **有** |
| Agent 直写结论 | 有 | 有(infer=False) | 弱 | 无 | 有 | **有** |
| 独立 LLM 提取 entity | 有(阻塞) | 有 | 有 | 有 | 有(异步) | **有(异步)** |
| provenance 指针 | 无 | 无 | 无 | 有(指向图内) | 无 | **有(指向磁盘 .md)** |
| 一致性校验 | 无 | 有 | 有(improve) | 有(temporal) | 弱(仅profile) | **有** |
| 自托管 MCP | 有 | 无(仅云端) | 有 | 有 | 社区版 | **有** |
| 多用户隔离 | 无 | 有 | 无 | 无 | 无 | **有** |
| 基础设施 | 远程服务 | 云端 | Docker | 图数据库 | 单进程 | **SQLite+LanceDB** |

### 7.2 最接近的几个工具

**EverOS**（13K star）：最接近我们的思路——Markdown is source of truth，cascade 守护进程监听 .md 变更自动增量索引。但缺 provenance 指针（无法从搜索结果追溯到原文）和显式一致性校验（只有 profile 级别的简单更新），MCP 非官方内置。我们补了这两块。

**Graphiti / Zep**（31K star）：时态知识图谱做得最完整——每个事实有 `valid_from / invalid_at` 时间窗口，provenance 是核心特性。我们的 Layer 3 temporal invalidation 借鉴了它的设计。但 episode 在图数据库里不在磁盘 .md，provenance 指向图内部节点不指向外部文件，备份迁移不便。每条 episode 要调多次 LLM，基础设施依赖重（需 Neo4j/FalkorDB）。数据量小（几百 .md）时图数据库的性能优势用不上，运维成本不值得。

**Mem0**（65K star）：事实提取 + 向量库做得好，`infer=False` 可绕过 LLM 直接存储。致命缺口是 MCP server 仅云端托管，OSS 无自托管 MCP——不适合数据不出本地的场景。无文件系统，无 provenance。

**Cognee**（31K star）：`remember(background=True)` 异步提取 + `improve()` 矛盾消解设计不错，内置自托管 MCP。但无文件系统，记忆存在图数据库里（Kuzu 已 deprecated），不可直接阅读编辑。

### 7.3 自建的理由总结

1. **没有工具完整实现三层**（Agent 写 → 独立 LLM 提取 → 独立 LLM 校验）——EverOS 最接近但缺 provenance + 一致性校验，Graphiti 最完整但需图数据库
2. **provenance 指向磁盘文件**是我们独有的设计——其他工具的 provenance 指向数据库内部节点，我们指向人可直接阅读的 .md 文件
3. **数据量小**（几百个 .md 文件），SQLite 绰绰有余，图数据库的规模优势用不上
4. **自建可以完全控制数据模型和提取逻辑**，~1500 行代码 vs 引入一个近千 commits 的框架
5. **数据不出局域网**——LLM 推理在 m2ultra，记忆文件在 debsvc，不依赖任何云服务

---

## 附录：代码文件索引

| 文件 | 行数 | 职责 |
|---|---|---|
| `yacmemo/db.py` | 410 | SQLite 数据层：7 张表（users/nodes/edges/events/processed_files/consistency_log/split_files），系统级数据库 + user_id 隔离 |
| `yacmemo/extractor.py` | 318 | Layer 2 核心：LLM 拆分提取 + 增量更新 + 冲突保护 |
| `yacmemo/consistency.py` | 132 | Layer 3 核心：向量粗筛 + LLM 精判 + 半自动失效 |
| `yacmemo/enhancer.py` | 322 | 服务入口：FastAPI webhook + APScheduler cron + 多用户扫描 |
| `yacmemo/mcp_server.py` | 371 | Agent 接口：9 个 MCP 工具（stdio 传输），按用户隔离 |
| `yacmemo/vector.py` | 115 | LanceDB 向量存储：node/event/edge 三表 + 合并搜索 |
| `yacmemo/config.py` | 213 | 配置加载：TOML → dataclass，多用户配置 + 全局/用户级覆盖 |
| `yacmemo/llm.py` | 106 | LLM 客户端：OpenAI 兼容 API + JSON 输出 + 重试 |
| `yacmemo/embedding.py` | 44 | Embedding 客户端：OpenAI 兼容 /v1/embeddings |
| `yacmemo/fs_utils.py` | 142 | 文件系统工具：hash 计算 + 拆分目录检测 + 安全读写 |
| `yacmemo/webui/app.py` | 520 | WebUI 后端：纯 JSON API + SPA 静态文件服务 |

### 相关设计文档

- [`consistency-checking.md`](./consistency-checking.md) — Layer 3 一致性校验的完整设计细节
- [`split-file-protection.md`](./split-file-protection.md) — 拆分文件保护方案（hash 校验 + 冲突保留）