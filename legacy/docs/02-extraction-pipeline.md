# Layer 2 提取管道设计文档

> 2026-09-13
> 状态：已实现
> 代码位置：`yacmemo/extractor.py`、`yacmemo/llm.py`、`yacmemo/fs_utils.py`、`yacmemo/db.py`、`yacmemo/vector.py`、`yacmemo/embedding.py`

## 一、概述

yacmemo 三层架构中，Layer 1 是 Agent 直接用自然语言写的 .md 文件（自由格式、混合多事项），Layer 2 是独立 LLM 从原文中提取结构化记忆的管道。提取管道由 `Extractor` 类（`extractor.py:60`）实现，核心入口是 `process_file(user_id, md_path)`（`extractor.py:71`）。

Layer 2 使用的 LLM 是 Ling-3.0-tiny-MLX-4bit（7.9B/1.3B MoE），部署在 m2ultra 的 mlx-serve 上（端口 11234）。这是一个小模型，所以提取策略在"能力充分利用"和"输出可控"之间做了权衡。

管道做四件事：
1. 把一个混合多事项的 .md 原文按独立事项拆分为多个细粒度文件
2. 从每个拆分文件中提取 entity（实体）和 event（事件）
3. 写入 SQLite（结构化数据）+ LanceDB（向量索引）
4. 记录 provenance（溯源）指针，从 entity 可追溯到拆分文件，从拆分文件可追溯到原文

## 二、处理流程

`process_file` 的 9 步完整流程：

### Step 1：读取文件并计算 hash（`extractor.py:76-84`）

```python
with open(md_path, encoding="utf-8") as f:
    content = f.read()
chash = content_hash(content)
```

`content_hash`（`fs_utils.py:9`）对文件内容做 SHA256。文件不存在时直接返回 `failed`。

### Step 2：检查是否已处理（`extractor.py:86-91`）

查 `processed_files` 表（`db.py:95-106`），获取上次处理时的 `content_hash`：

- hash 一致且上次 status 为 `success` → 返回 `skipped`，跳过整个管道
- hash 不一致或无记录 → 继续处理

这是增量管道的入口判断：如果 Agent 没改文件，不重复消耗 LLM 调用。

### Step 3：确定拆分目录（`extractor.py:93-95`）

```python
split_dir = get_split_dir(md_path, memory_root)
```

`get_split_dir`（`fs_utils.py:20`）把 `.md` 后缀去掉换成同名目录：

```
memory/resources/projects/01-工作记录.md
  → memory/resources/projects/01-工作记录/
```

拆分目录就在原文旁边，方便 Agent 用 Read 查看拆分结果。

### Step 4：读取上次拆分结果（`extractor.py:97-100`）

如果本次是增量处理（`is_incremental = True`），且拆分目录已存在，调 `_read_prev_splits`（`extractor.py:209`）读取上次拆分文件的元数据（name + title），供 LLM 做增量对比。

### Step 5：调用 LLM 提取（`extractor.py:102-117`）

调 `_extract`（`extractor.py:195`）→ `llm.chat_json()`（`llm.py:90`），让 LLM 对原文做拆分+提取。LLM 失败时记录 `failed` 状态到 `processed_files` 表并返回。LLM 返回空 files 时记录 `success`（split_count=0）并返回。

### Step 6：写拆分文件（`extractor.py:119-120`）

调 `_write_split_files`（`extractor.py:227`），把 LLM 返回的每个事项写为独立 .md 文件到拆分目录。写入前做 hash 校验——如果用户手动改过拆分文件，保留为 `.conflict` 备份（详见下文第六节和 [split-file-protection.md](./split-file-protection.md)）。

### Step 7：清除旧数据（`extractor.py:122-128`）

对每个拆分文件，按 `source_path` 删除旧的 entity/event/edge 和向量：

```python
self.db.delete_nodes_by_source(user_id, split_rel)
self.db.delete_events_by_source(user_id, split_rel)
self.db.delete_edges_by_source(user_id, split_rel)
self.vector.delete_by_source(split_rel)
```

`_list_split_files`（`extractor.py:309`）只匹配当前 LLM 返回的文件名列表，避免误删其他拆分文件。

### Step 8：索引新数据（`extractor.py:130-181`）

遍历每个拆分文件的 entities 和 events：

- entity → `db.upsert_node`（`db.py:202`）写 SQLite，`emb.embed_one` + `vector.upsert_node_vector`（`vector.py:69`）写 LanceDB
- event → `db.upsert_event`（`db.py:286`）写 SQLite，`emb.embed_one` + `vector.upsert_event_vector`（`vector.py:73`）写 LanceDB
- embedding 失败只记 warning，不阻塞——SQLite 数据仍在，只是向量搜索缺这条数据

### Step 9：更新处理状态（`extractor.py:182-193`）

```python
status = "incremental" if is_incremental else "success"
self.db.record_processed_file(user_id, rel_path, chash, split_dir_rel, status, split_file_count=len(files))
```

记录新的 content_hash 和拆分文件数量，下次 Step 2 用这个 hash 做 skip 判断。

## 三、拆分策略

### 3.1 为什么要拆分

Agent 写 .md 是自由格式，一个文件可能混合多个不相关事项。如果直接从整篇原文提取 entity，provenance 只能指向原文文件级——Agent 搜到一个实体时，看到 source_path 是一个 500 行的工作记录，不知道这个实体具体在讲哪件事。

拆分后，provenance 指向拆分文件级（事项级粒度）。Agent 搜到实体 → source_path 指向 `01-工作记录/部署yacmemo.md` → Read 这个文件就能看到完整的事项上下文。粒度从"文件级"细化到"事项级"。

### 3.2 拆分目录结构

拆分目录放在原文旁边，同名目录：

```
memory/resources/projects/
  01-工作记录.md                ← Layer 1 原文（Agent 写）
  01-工作记录/                 ← Layer 2 拆分目录（LLM 写）
    ├── deploy-yacmemo.md
    ├── fix-vector-search.md
    └── multi-user-support.md
```

`get_split_dir`（`fs_utils.py:20`）实现这个映射。`list_md_files`（`fs_utils.py:47`）在扫描时用 `is_in_split_dir`（`fs_utils.py:31`）过滤掉拆分目录内的文件，避免管道递归处理拆分文件。

### 3.3 拆分文件格式

`_write_split_files`（`extractor.py:227`）生成的文件格式：

```markdown
---
source: 01-工作记录.md
created: 2026-09-13
updated: 2026-09-13
---

# 部署 yacmemo 到 debsvc

用 conda 创建 teleagent 环境，安装依赖，配置 config.toml...

## 关联实体
- debsvc: 服务器
- conda: 工具
- config.toml: 配置文件

## 事件
- 2026-09-13: 在 debsvc 上创建 conda 环境并安装 yacmemo
```

- frontmatter：`source`（原文文件名）、`created`/`updated`（UTC 日期）
- 正文：LLM 生成的简洁摘要，保留关键细节
- 关联实体段落：列出从该事项中提取的实体
- 事件段落：列出从该事项中提取的事件

### 3.4 Agent 不碰拆分目录

MCP server 的 `memory_write` 和 `memory_edit` 工具通过 `safe_write`/`safe_edit`（`fs_utils.py:71`/`fs_utils.py:96`）拦截写入：

```python
# fs_utils.py:86
if not allow_split and is_in_split_dir(path, memory_root):
    raise ValueError("Cannot write to split directory (managed by memory-enhancer)")
```

`is_in_split_dir`（`fs_utils.py:31`）的判断逻辑：如果文件所在目录的父目录下存在同名 `.md` 文件，则该目录是拆分目录。例如 `01-工作记录/deploy-yacmemo.md` 的父目录是 `01-工作记录/`，其父目录 `projects/` 下存在 `01-工作记录.md`，所以判定为拆分目录。

## 四、增量更新

### 4.1 hash 比对决定处理模式

Step 2 查 `processed_files` 表的 `content_hash`：

| 条件 | 模式 | 行为 |
|---|---|---|
| 无记录 | 首次提取 | `is_incremental = False`，全量提取 |
| hash 一致 + status=success | skip | 直接返回，不调 LLM |
| hash 不一致 | 增量提取 | `is_incremental = True`，带上次拆分结果调 LLM |

### 4.2 增量提取的 LLM 交互

`_extract`（`extractor.py:195`）在增量模式下，把 `_INCREMENT_HINT` 拼到 user message 后面：

```python
user_msg = content
if prev_splits:
    user_msg += "\n\n" + _INCREMENT_HINT + "\n".join(
        f"- {s['name']}: {s['title']}" for s in prev_splits
    )
```

`_INCREMENT_HINT`（`extractor.py:49`）告诉 LLM 做增量更新的规则：

- 新增的内容 → 创建新文件
- 修改的内容 → 更新对应文件
- 未变的内容 → 保持不变
- 删除的内容 → 不再包含

同时附上上次拆分文件的 name + title 列表，让 LLM 知道已有哪些事项。

### 4.3 为什么不"删旧重提"

每次原文变更都全量重新提取也可以工作，但增量模式有两个优势：

1. **省 LLM 调用**：hash 一致时直接 skip，hash 不一致时 LLM 拿到上次拆分列表可以只输出变化部分，不需要重新生成未变事项的完整内容。
2. **时态连续性**：`upsert_node`（`db.py:202`）按 `user_id + name + source_path` 做 upsert——同名同源的实体会更新而非新建，保留 `created_at` 不变，只更新 `updated_at`。全量删除重提会导致所有实体重新创建，丢失原始创建时间。

## 五、LLM Prompt 设计

### 5.1 System Prompt（`_EXTRACT_SYSTEM`，`extractor.py:31`）

```
你是一个记忆提取助手。请将给定的工作记录按独立事项拆分为多个文件，
并为每个文件提取关联的实体和事件。
以JSON格式输出，格式如下：
{
  "files": [
    {
      "name": "文件名（不含.md后缀，用英文kebab-case）",
      "title": "标题（中文，简明描述这个事项）",
      "content": "文件正文内容（简洁摘要，保留关键细节）",
      "entities": [{"name": "实体名称", "type": "实体类型", "summary": "一句话描述"}],
      "events": [{"date": "日期(YYYY-MM-DD或留空)", "type": "事件类型", "description": "事件描述"}]
    }
  ]
}
```

Prompt 直接在 system message 里给出 JSON schema，不使用 function calling。原因是 mlx-serve 对 function calling 的支持不稳定，`response_format: {"type": "json_object"}` 是更可靠的纯 JSON 输出控制方式。

### 5.2 增量提示（`_INCREMENT_HINT`，`extractor.py:49`）

```
以下是上次的拆分结果，请做增量更新：
- 新增的内容 → 创建新文件
- 修改的内容 → 更新对应文件
- 未变的内容 → 保持不变
- 删除的内容 → 不再包含

上次拆分结果：
```

这段拼在 user message 末尾，后面跟上次拆分文件的 name: title 列表。

### 5.3 LLM 调用参数

`chat_json`（`llm.py:90`）调 `_call`（`llm.py:40`）时设置的参数：

| 参数 | 值 | 代码位置 |
|---|---|---|
| `response_format` | `{"type": "json_object"}` | `llm.py:61`，强制纯 JSON 输出 |
| `enable_thinking` | `false` | `llm.py:56`，关闭思考模式（MoE 模型思考模式耗时长且输出不稳定） |
| `temperature` | `0.3` | `llm.py:50`，偏低温度保证输出稳定性 |
| `max_tokens` | `2048` | `config.py:17`，`extract_max_tokens` 默认值 |
| `timeout` | `60` 秒 | `config.py:19`，`extract_timeout` 默认值 |
| 重试 | 3 次 | `llm.py:65`，指数退避 2s/4s |

`chat_json` 拿到 raw 字符串后 `json.loads` 解析，解析失败抛 `LLMJSONError`（`llm.py:18`），携带 raw 内容方便调试。

## 六、provenance 两层指针

### 6.1 指针结构

```
entity.source_path  →  拆分文件路径（如 resources/projects/01-工作记录/deploy-yacmemo.md）
                         ↓ 拆分文件 frontmatter.source
                      原文路径（如 resources/projects/01-工作记录.md）
```

- 第一层：`nodes` 表的 `source_path` 字段（`db.py:43`）指向拆分文件相对路径，同时有 `original_path` 字段指向原文相对路径
- 第二层：拆分文件 frontmatter 的 `source` 字段记录原文文件名

Agent 搜到实体后，可以直接 Read `source_path` 看到事项级上下文（几十行），如果需要完整背景再通过 `original_path` 或 frontmatter 的 `source` 追溯到原文。

### 6.2 为什么两层而不是一层

| 方案 | 粒度 | 追溯能力 |
|---|---|---|
| 一层：直接指向原文 | 文件级 | Agent 看到指向 500 行工作记录，不知道实体在讲哪件事 |
| 两层：指向拆分文件 + 拆分文件指向原文 | 事项级 | Agent 先看拆分文件（精确上下文），需要时再看原文（完整背景） |

两层指针的核心价值是"先精确后完整"的溯源路径。事项级粒度让 Agent 的 Read 操作高效（不用读 500 行找一句话），原文级追溯保证不丢失全局上下文。

`upsert_node`（`db.py:202`）同时写入 `source_path`（拆分文件路径）和 `original_path`（原文路径），两个字段在 Step 8 一起写入。

## 七、向量化

### 7.1 embedding 文本构造

- entity 的 `node_text = f"{name}: {summary}"`（`extractor.py:147`），例如 `"debsvc: debsvc 服务器，IP 192.168.5.7"`
- event 的 `evt_text = f"{date} {type}: {description}"`（`extractor.py:168`），例如 `"2026-09-13 deploy: 在 debsvc 上创建 conda 环境并安装 yacmemo"`

### 7.2 向量存储

embedding 由 `EmbeddingClient`（`embedding.py:8`）调 omlx 服务的 `/v1/embeddings` 端点生成，维度 1024。向量写入 LanceDB：

- entity 向量 → `node_vectors` 表（`vector.py:13`）
- event 向量 → `event_vectors` 表（`vector.py:20`）

`_upsert`（`vector.py:54`）先按 id 删除旧记录再插入新记录（LanceDB 无原生 upsert）。

### 7.3 embedding 失败不阻塞

```python
# extractor.py:155-159
try:
    emb_vec = self.emb.embed_one(node_text)
    self.vector.upsert_node_vector(node_id, node_text, emb_vec, split_rel)
except Exception as e:
    logger.warning("Embedding failed for entity '%s': %s", name, e)
```

embedding 失败只记 warning，SQLite 数据照常写入。这意味着向量搜索可能缺部分数据，但结构化查询（按 name/type/source_path 查 SQLite）不受影响。这种降级策略是因为 embedding 服务（omlx）和 LLM 服务（mlx-serve）是两个独立进程，一个挂了不应该影响另一个。

## 八、错误处理

| 错误类型 | 代码位置 | 处理方式 |
|---|---|---|
| 原文不存在 | `extractor.py:80-82` | 返回 `failed`，errors=["File not found"] |
| LLM 调用失败（超时/HTTP错误） | `llm.py:65-88` | 3 次指数退避重试，全部失败抛 `LLMError` |
| LLM 返回非法 JSON | `llm.py:98-101` | 抛 `LLMJSONError`，携带 raw 内容 |
| 提取阶段任何异常 | `extractor.py:104-110` | 记录 `failed` 状态 + error_msg 到 `processed_files`，返回 `failed` |
| LLM 返回空 files | `extractor.py:113-117` | 记录 `success`（split_count=0），不算错误 |
| embedding 失败 | `extractor.py:158-159` / `177-179` | 记 warning，不阻塞，SQLite 数据仍写入 |
| 向量删除失败 | `vector.py:112-114` | 记 warning，不阻塞后续索引 |
| 拆分文件写入时 IO 错误 | `extractor.py:300-301` | 异常上抛，由调用方处理 |

关键设计原则：**LLM 失败是硬失败**（整个文件标记 failed，下次 cron 会重试），**embedding 失败是软失败**（数据降级但不停管道）。

## 九、冲突保护

写拆分文件前的 hash 校验机制，详见 [拆分文件保护设计文档](./split-file-protection.md)。这里简述提取管道中的实现。

`_write_split_files`（`extractor.py:227`）在覆盖每个拆分文件前：

```
1. 文件在磁盘上存在？
   → 计算磁盘文件 hash（file_hash）
   → 查 split_files 表获取上次写入时记录的 hash（db.get_split_file）
   → 两个 hash 不一致 → 文件被用户手动改过
   → 重命名为 {path}.conflict.{timestamp} 保留用户改动
   → 日志 warning

2. 写入新内容

3. 计算新内容 hash，记录到 split_files 表（db.record_split_file）
```

代码位置：`extractor.py:284-305`。`.conflict` 文件不会被 `list_split_files_in_dir`（`fs_utils.py:127`）列出，不会被管道再次处理。用户需要手动决定保留还是丢弃 `.conflict` 文件中的改动——Agent 不会自动处理，这需要人类判断。

## 十、完整示例

### 10.1 原文

假设 Agent 写了一个混合了 3 个事项的工作记录 `resources/projects/01-工作记录.md`：

```markdown
# 2026-09-13 工作记录

## 部署 yacmemo
今天把 yacmemo 部署到 debsvc（192.168.5.7）。
用 conda 创建了 teleagent 环境，安装了依赖。
config.toml 配了 LLM 指向 m2ultra:11234，embedding 指向 m2ultra:11235。
跑了测试，148 passed。

## 修复向量搜索 bug
发现 LanceDB 的 delete 语句用了双引号导致报错。
改成单引号后修复。代码在 vector.py 的 _upsert 方法。

## 多用户支持
给老婆也加了用户，config.toml 用 [[users]] 数组定义。
每个用户独立 data/{user_id}/ 目录，LanceDB 表也隔离。
wife 用户的 MCP 注册时命令带 --user wife。
commit 4a5c100。
```

### 10.2 首次提取

Step 2 查 `processed_files` → 无记录 → `is_incremental = False`。

Step 5 调 LLM，user message 就是原文全文，不带 `_INCREMENT_HINT`。

LLM 返回：

```json
{
  "files": [
    {
      "name": "deploy-yacmemo",
      "title": "部署 yacmemo 到 debsvc",
      "content": "把 yacmemo 部署到 debsvc（192.168.5.7）。用 conda 创建 teleagent 环境，安装依赖。config.toml 配 LLM 指向 m2ultra:11234，embedding 指向 m2ultra:11235。跑测试 148 passed。",
      "entities": [
        {"name": "debsvc", "type": "服务器", "summary": "debsvc 服务器，IP 192.168.5.7"},
        {"name": "conda", "type": "工具", "summary": "Python 环境管理工具"},
        {"name": "config.toml", "type": "配置文件", "summary": "yacmemo 主配置文件"}
      ],
      "events": [
        {"date": "2026-09-13", "type": "deploy", "description": "在 debsvc 上创建 conda 环境并部署 yacmemo"}
      ]
    },
    {
      "name": "fix-vector-search",
      "title": "修复向量搜索 bug",
      "content": "LanceDB 的 delete 语句用了双引号导致报错，改成单引号后修复。代码在 vector.py 的 _upsert 方法。",
      "entities": [
        {"name": "LanceDB", "type": "数据库", "summary": "向量数据库"},
        {"name": "vector.py", "type": "代码文件", "summary": "向量存储模块"}
      ],
      "events": [
        {"date": "2026-09-13", "type": "fix", "description": "修复 LanceDB delete 语句引号问题"}
      ]
    },
    {
      "name": "multi-user-support",
      "title": "多用户支持",
      "content": "config.toml 用 [[users]] 数组定义多用户，每用户独立 data/{user_id}/ 目录和 LanceDB 表。wife 用户 MCP 注册时命令带 --user wife。commit 4a5c100。",
      "entities": [
        {"name": "config.toml", "type": "配置文件", "summary": "yacmemo 主配置文件"},
        {"name": "4a5c100", "type": "commit", "summary": "多用户支持功能的提交"}
      ],
      "events": [
        {"date": "2026-09-13", "type": "feature", "description": "实现多用户支持，commit 4a5c100"}
      ]
    }
  ]
}
```

Step 6 写 3 个拆分文件到 `01-工作记录/` 目录：

```
resources/projects/01-工作记录/
  ├── deploy-yacmemo.md
  ├── fix-vector-search.md
  └── multi-user-support.md
```

每个文件内容如 3.3 节所示的格式。

Step 7 清旧数据 → 无（首次提取）。

Step 8 索引：
- 7 个 entity 写入 `nodes` 表，7 条向量写入 `node_vectors`
  - `config.toml` 出现在两个拆分文件中，因为 `source_path` 不同（`deploy-yacmemo.md` vs `multi-user-support.md`），所以是两条独立的 node 记录
- 3 个 event 写入 `events` 表，3 条向量写入 `event_vectors`

Step 9 记录 `processed_files`：status=success，content_hash=原文 hash，split_file_count=3。

### 10.3 增量更新

假设 Agent 在原文末尾加了新事项"配置 cron 定时任务"，其他三个事项不变。

Step 2 查 `processed_files` → hash 不一致 → `is_incremental = True`。

Step 4 读取上次拆分结果：
```python
[{"name": "deploy-yacmemo", "title": "部署 yacmemo 到 debsvc"},
 {"name": "fix-vector-search", "title": "修复向量搜索 bug"},
 {"name": "multi-user-support", "title": "多用户支持"}]
```

Step 5 调 LLM，user message = 原文 + `_INCREMENT_HINT` + 上次拆分列表。LLM 知道已有 3 个事项，返回的 JSON 包含 4 个 files（原有 3 个内容不变 + 新增 1 个 `setup-cron`）。

Step 6 写拆分文件：
- `deploy-yacmemo.md`：磁盘文件存在，hash 校验一致（未被手动改）→ 安全覆盖
- `fix-vector-search.md`：同上
- `multi-user-support.md`：同上
- `setup-cron.md`：新文件，直接创建

Step 7 清旧数据：按 `_list_split_files` 返回的 4 个文件名，删除每个拆分文件对应的旧 entity/event/edge 和向量。

Step 8 索引：4 个拆分文件的 entities 和 events 全部重新写入。`upsert_node` 对同名同源的实体会更新而非新建，`created_at` 保留不变。

Step 9 记录 status=incremental。

### 10.4 用户手动修改后再次提取

假设用户手动编辑了 `deploy-yacmemo.md`，加了"后来发现端口配错了，改成了 11235"。

下次原文变更触发提取时，Step 6 写 `deploy-yacmemo.md`：

1. 文件在磁盘上存在
2. `file_hash(filepath)` 得到当前磁盘 hash
3. `db.get_split_file(user_id, rel_filepath)` 得到上次记录的 hash
4. 两个 hash 不一致（用户改过）
5. 重命名为 `deploy-yacmemo.md.conflict.20260913120000` 保留用户改动
6. 写入新的提取内容
7. 记录新 hash

日志输出：`Split file manually modified, preserved as .../deploy-yacmemo.md.conflict.20260913120000`

用户事后可以通过 `memory_split_status` MCP 工具看到 conflict 状态，决定如何处理。