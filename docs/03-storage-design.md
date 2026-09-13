---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'b1ee98d5-530b-4114-9ad5-75a378b979e6'
  PropagateID: 'b1ee98d5-530b-4114-9ad5-75a378b979e6'
  ReservedCode1: '8ad44a99-2e96-4f6a-8660-b5b3cdf1898a'
  ReservedCode2: '8ad44a99-2e96-4f6a-8660-b5b3cdf1898a'
---

# yacmemo 存储层设计

## 1. 概述

yacmemo 同时使用三种存储引擎，各自解决不同的问题，互相不可替代：

| 存储 | 回答的问题 | 角色 | 代码位置 |
|------|-----------|------|---------|
| .md 文件 | "发生了什么" | 人类可读可编辑的工作记录，source of truth | `fs_utils.py` |
| SQLite | "我们知道什么" | 结构化事实存储与状态管理 | `db.py` → `MemoryDB` |
| LanceDB | "什么和什么相关" | 向量索引，语义搜索与相似度匹配 | `vector.py` → `VectorStore` |

数据流方向：`.md → SQLite + LanceDB`（单向派生）。删掉后两个，可以从 .md 全量重建；反过来不行。

## 2. 三种存储各自的角色

### 2.1 .md 文件——source of truth

用户（或 Agent）直接写 .md 文件记录工作内容。这是整个系统的唯一事实来源。

- **可 git**：版本控制天然支持，`git diff` 能看到每次改了什么
- **可 grep**：`grep -r "role_color" .` 直接搜文本
- **可 Obsidian**：用 Obsidian 打开 memory_root 目录，图谱、双链、标签全用
- **人类可读可编辑**：不依赖任何工具就能阅读和修改

文件组织示例：

```
memory_root/
├── memories/
│   ├── identity/whoami.md
│   ├── soul/personality.md
│   └── preferences/work-style.md
├── resources/
│   ├── environment/m2ultra.md
│   ├── infrastructure/esxi.md
│   └── projects/
│       ├── 01-数据门户.md          ← 原始记录
│       └── 01-数据门户/            ← 拆分目录（extractor 生成）
│           ├── feat-a.md
│           └── feat-b.md
```

大文件会被 extractor 拆分到同名目录下（`01-数据门户.md` → `01-数据门户/`），拆分文件也参与提取，但有冲突保护机制（详见 `split-file-protection.md`）。

### 2.2 SQLite——结构化事实存储与状态管理

SQLite 存储"我们从 .md 中提取出了什么"的结构化结果，以及系统运行时的状态信息。它解决 .md 做不到的事情：

| 能力 | .md 能做吗 | SQLite 能做吗 |
|------|-----------|--------------|
| 查"role_color"这个实体的有效版本 | grep 能搜到文本，但不知道哪个有效 | `valid=1` 精确过滤 |
| 查某个实体所有历史版本 | 能搜到所有提及，但分不清版本关系 | `get_node_history` 按 `created_at` 排序 |
| 查当前有多少待确认的矛盾 | 不行 | `SELECT count(*) FROM consistency_log WHERE status='pending'` |
| 多用户数据隔离 | 只能靠目录分 | `user_id` 列过滤 |
| 聚合统计（多少实体、多少事件） | 不行 | `SELECT count(*) GROUP BY` |
| 增量提取跟踪（哪些文件处理过） | 不行 | `processed_files` 表 |

**关键设计决策：系统级单库 + user_id 列隔离。** 所有用户共享一个 SQLite 文件（`data/system.db`），业务表都有 `user_id` 列。这比每用户一个数据库文件更容易管理（备份一条命令、统计一条 SQL），且数据规模小（几百文件、几千节点），不存在性能瓶颈。

### 2.3 LanceDB——向量索引

LanceDB 存储"实体/事件/关系的文本向量"，支持语义搜索。它解决 SQLite 做不到的事情：

- **语义搜索**：`search_nodes(query_embedding)` 找到"和查询语义相似"的实体，而不是字面匹配
- **一致性校验依赖它**：新提取的实体先算 embedding，在 `node_vectors` 中搜索相似实体，再让 LLM 判断是否矛盾（`consistency.py:51`）
- **跨类型搜索**：`search_all` 同时搜 node/event/edge 三张向量表，按距离排序合并

LanceDB 目录按用户隔离：`data/lancedb/{user_id}/`，每个用户有独立的向量库。

## 3. 为什么不能只用一种

### 3.1 只用 .md

`grep` 能搜文本，但：
- 不知道哪个实体当前有效、哪个已失效（`valid` 字段不存在于 .md 中）
- 不能做聚合统计（"我有多少个 type=person 的实体？"——得自己数）
- 不能跟踪增量提取状态（哪些 .md 已经提取过、hash 变没变）
- 不能记录一致性校验结果（矛盾检测是 LLM 判断 + 向量搜索的结果，无法写回 .md）

### 3.2 只用 SQLite

- 不可 `git diff`：SQLite 是二进制文件，`git diff` 看不到有意义的变化
- 不可 Obsidian 浏览：Obsidian 只认 .md
- provenance 自引用：每个 entity 都有 `source_path` 指向来源 .md，如果 .md 不存在，provenance 链条断裂——没有外部可验证的原始记录
- 人类不可直接编辑：用户不会手写 SQL 来记录"今天做了什么"

### 3.3 只用 LanceDB

- 没有向量的表（`consistency_log`、`processed_files`、`users`、`split_files`）放进去不合适——这些是状态表，不需要向量搜索
- 多表 join 和事务不是 LanceDB 的设计目标——LanceDB 是列式向量数据库，不是关系数据库
- 不支持 `valid` 状态过滤的精确查询（向量搜索是近似匹配，不是精确过滤）
- 不可 git、不可人类阅读

## 4. SQLite 数据模型

系统级单库（`data/system.db`），共 7 张表。初始化时开启 WAL 模式和外键约束（`db.py:148-149`）：

```python
self.conn.execute("PRAGMA journal_mode=WAL")
self.conn.execute("PRAGMA foreign_keys=ON")
```

### 4.1 users——用户管理

系统级表，管理所有用户。`config.toml` 的 `[[users]]` 仅用于首次导入，运行时从数据库读取。

```sql
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL DEFAULT '',
    memory_root     TEXT NOT NULL,           -- 该用户的 .md 文件根目录
    llm_api_key     TEXT DEFAULT '',          -- 空则继承全局配置
    embedding_api_key TEXT DEFAULT '',        -- 空则继承全局配置
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
```

设计理由：
- `memory_root` 按用户隔离：不同用户的 .md 文件存不同目录
- `llm_api_key` / `embedding_api_key` 可按用户覆盖（空值继承全局 `[llm]` / `[embedding]` 配置），实现多用户不同 API key

### 4.2 nodes——实体（节点）

每条记录代表一个从 .md 中提取出的实体（人、设备、项目、概念等）。

```sql
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    name        TEXT NOT NULL,               -- 实体名称
    type        TEXT NOT NULL,               -- 实体类型（person/device/project/concept...）
    summary     TEXT,                        -- 摘要
    source_path TEXT NOT NULL,               -- 来源 .md 路径（provenance）
    source_hash TEXT NOT NULL,               -- 来源文件内容 hash（增量提取依据）
    original_path TEXT,                      -- 拆分文件的原始路径
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    valid       INTEGER DEFAULT 1,           -- 1=有效, 0=已失效
    invalid_at  TEXT,                        -- 失效时间
    invalid_reason TEXT,                     -- 失效原因（如 "superseded_by:xxx"）
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_nodes_user_name ON nodes(user_id, name);
CREATE INDEX IF NOT EXISTS idx_nodes_user_valid ON nodes(user_id, valid);
CREATE INDEX IF NOT EXISTS idx_nodes_source ON nodes(source_path);
```

设计理由：
- `valid` + `invalid_at` + `invalid_reason`：实体生命周期管理。一致性校验发现矛盾时，旧实体被标记为 `valid=0`，保留历史记录而非物理删除
- `source_path` + `source_hash`：provenance 追溯 + 增量提取。文件 hash 变了才重新提取
- `idx_nodes_user_name`：按用户 + 名称查询（历史版本查询依赖此索引）
- `idx_nodes_user_valid`：按用户查有效实体（一致性全量扫描依赖此索引）

查询示例——查 `role_color` 的所有历史版本：

```sql
SELECT id, summary, source_path, created_at, valid, invalid_reason
FROM nodes
WHERE user_id = 'yachen' AND name = 'role_color'
ORDER BY created_at;
```

对应代码：`db.py:247` `get_node_history(user_id, name)`。

### 4.3 edges——关系

实体之间的关系（A 是 B 的成员、A 依赖 B 等）。

```sql
CREATE TABLE IF NOT EXISTS edges (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    source_node   TEXT NOT NULL,             -- 源节点名称
    target_node   TEXT NOT NULL,             -- 目标节点名称
    relation      TEXT NOT NULL,             -- 关系类型（member_of/depends_on/uses...）
    summary       TEXT,
    source_path   TEXT NOT NULL,
    source_hash   TEXT NOT NULL,
    original_path TEXT,
    valid_from    TEXT NOT NULL,             -- 关系生效时间
    invalid_at    TEXT,                      -- 关系失效时间（时态关系）
    invalid_reason TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_edges_user_source ON edges(user_id, source_node);
CREATE INDEX IF NOT EXISTS idx_edges_user_target ON edges(user_id, target_node);
```

设计理由：
- `valid_from` + `invalid_at`：支持时态关系——某关系在某个时间段有效，之后可能失效。这不是简单的"当前关系"，而是"关系的时间线"
- `source_node` / `target_node` 存的是节点**名称**而非 id，因为提取时可能还没有 node id（先提边再提节点的情况）
- 两个索引分别支持"从 A 出发的关系"和"指向 A 的关系"两种查询

### 4.4 events——事件

从 .md 中提取的事件记录（某天做了什么、发生了什么）。

```sql
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    date          TEXT,                      -- 事件日期（可能未知）
    type          TEXT,                      -- 事件类型
    summary       TEXT NOT NULL,             -- 事件摘要
    details       TEXT,                      -- 详细信息
    source_path   TEXT NOT NULL,
    source_hash   TEXT NOT NULL,
    original_path TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_events_user_date ON events(user_id, date);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_path);
```

设计理由：
- `date` 可空：有些事件无法确定具体日期（从 .md 中提取不到时间信息）
- `idx_events_user_date`：按用户 + 日期查询，支持时间线浏览

查询示例——查某用户 2026 年 9 月的所有事件：

```sql
SELECT date, type, summary, source_path
FROM events
WHERE user_id = 'yachen' AND date LIKE '2026-09%'
ORDER BY date;
```

### 4.5 processed_files——文件处理跟踪

记录哪些 .md 文件已经被提取过，以及提取时的内容 hash。增量提取的核心依据。

```sql
CREATE TABLE IF NOT EXISTS processed_files (
    path          TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,             -- 提取时的文件内容 SHA256
    split_dir     TEXT,                      -- 拆分目录路径（如有）
    processed_at  TEXT NOT NULL,
    status        TEXT NOT NULL,             -- success / error / pending
    error_msg     TEXT,
    split_file_count INTEGER DEFAULT 0,      -- 拆分出的子文件数量
    PRIMARY KEY (user_id, path),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
```

设计理由：
- 复合主键 `(user_id, path)`：同一文件路径在不同用户下独立跟踪
- `content_hash`：增量提取依据——文件 hash 没变就跳过，变了就重新提取
- `status` + `error_msg`：提取失败时不丢失记录，下次可重试
- `split_dir` + `split_file_count`：跟踪大文件拆分状态

查询示例——查所有提取失败的文件：

```sql
SELECT path, error_msg, processed_at
FROM processed_files
WHERE user_id = 'yachen' AND status = 'error'
ORDER BY processed_at;
```

### 4.6 consistency_log——一致性校验日志

记录一致性校验发现的矛盾及其处理状态。

```sql
CREATE TABLE IF NOT EXISTS consistency_log (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    old_node_id     TEXT,                    -- 旧实体 id
    new_node_id     TEXT,                    -- 新实体 id
    old_source_path TEXT,                    -- 旧实体来源
    new_source_path TEXT,                    -- 新实体来源
    reason          TEXT NOT NULL,           -- LLM 判断的矛盾理由
    confidence      REAL,                    -- LLM 置信度（0.0-1.0）
    checked_at      TEXT NOT NULL,
    auto_invalidated INTEGER DEFAULT 0,     -- 是否自动失效旧实体
    status          TEXT DEFAULT 'pending',  -- pending / auto_invalidated / resolved
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_consistency_user_status ON consistency_log(user_id, status);
```

设计理由：
- `old_node_id` + `new_node_id`：记录矛盾双方，便于追溯
- `confidence`：LLM 判断的置信度。高于阈值（默认 0.8）且 `auto_invalidate=True` 时自动失效旧实体
- `status` 三态：`pending`（待人工确认）→ `resolved`（已处理）；或 `auto_invalidated`（系统自动处理）
- `idx_consistency_user_status`：按用户 + 状态查询

查询示例——查待确认矛盾数量：

```sql
SELECT count(*) AS pending_count
FROM consistency_log
WHERE user_id = 'yachen' AND status = 'pending';
```

对应代码：`db.py:345` `get_pending_consistency(user_id)`。

### 4.7 split_files——拆分文件完整性跟踪

记录 extractor 生成的拆分文件的 hash，用于检测用户是否手动编辑了拆分文件。

```sql
CREATE TABLE IF NOT EXISTS split_files (
    user_id     TEXT NOT NULL,
    path        TEXT NOT NULL,               -- 拆分文件路径
    content_hash TEXT NOT NULL,             -- 写入时的文件内容 hash
    written_by  TEXT NOT NULL,               -- 写入者（extractor / enhancer）
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    status      TEXT DEFAULT 'active',       -- active / conflict
    PRIMARY KEY (user_id, path),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_split_files_user_status ON split_files(user_id, status);
```

设计理由：
- `content_hash`：提取时记录拆分文件的 hash。下次提取前比对——如果 hash 变了说明用户手动编辑过拆分文件，触发冲突保护（保留 `.conflict.{timestamp}` 备份）
- `status`：`active`（正常）或 `conflict`（检测到用户编辑，需人工处理）
- `written_by`：区分是 extractor 还是 enhancer 写入的

详见 `split-file-protection.md`。

## 5. LanceDB 向量索引

### 5.1 三张向量表

每张表用 PyArrow schema 定义，向量维度 1024（与 embedding 模型对齐）：

```python
# vector.py:13-32

_NODE_SCHEMA = pa.schema([
    pa.field("id", pa.string()),           # 对应 SQLite nodes.id
    pa.field("vector", pa.list_(pa.float32(), 1024)),
    pa.field("text", pa.string()),          # "name: summary" 拼接文本
    pa.field("source_path", pa.string()),  # 来源 .md 路径
])

_EVENT_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 1024)),
    pa.field("text", pa.string()),
    pa.field("source_path", pa.string()),
])

_EDGE_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 1024)),
    pa.field("text", pa.string()),          # "source relation target: summary"
    pa.field("source_path", pa.string()),
])
```

| 向量表 | 来源 | text 字段内容 | 用途 |
|-------|------|-------------|------|
| `node_vectors` | nodes 表 | `{name}: {summary}` | 实体语义搜索 + 一致性校验 |
| `event_vectors` | events 表 | `{summary}` | 事件语义搜索 |
| `edge_vectors` | edges 表 | `{source} {relation} {target}: {summary}` | 关系语义搜索 |

三张表 schema 完全相同（id + vector + text + source_path），分开建表而非合并是因为搜索时需要区分类型。

### 5.2 目录隔离

LanceDB 按用户隔离，每个用户有独立的向量库目录：

```python
# config.py:133-141
def user_lancedb_abs(self, user: UserConfig) -> str:
    lancedb_base = self.storage.get("lancedb_path", "data/lancedb/")
    base = os.environ.get("YACMEMO_HOME", os.getcwd())
    lancedb_root = str(Path(base) / lancedb_base)
    return os.path.join(lancedb_root, user.id)  # data/lancedb/{user_id}/
```

对比 SQLite 的单库 + user_id 列隔离，LanceDB 选择目录隔离的原因：LanceDB 不支持 SQL 级别的行级过滤，按用户分目录是最简单可靠的隔离方式。

### 5.3 核心操作

#### upsert（先 delete 再 add）

LanceDB 没有原生 upsert，用"先删后加"实现：

```python
# vector.py:54-67
def _upsert(self, table_name, item_id, text, embedding, source_path, schema):
    tbl = self.db.open_table(table_name)
    with contextlib.suppress(Exception):
        tbl.delete(f"id = '{item_id}'")  # 不存在时静默忽略
    tbl.add([{
        "id": item_id,
        "vector": embedding,
        "text": text,
        "source_path": source_path,
    }])
```

#### search（L2 距离）

```python
# vector.py:81-85
def search_nodes(self, query_embedding, limit=10):
    tbl = self.db.open_table("node_vectors")
    results = tbl.search(query_embedding).limit(limit).to_list()
    return results  # [{id, text, source_path, _distance}]
```

LanceDB 默认使用 L2 距离，`_distance` 越小越相似。一致性校验中，距离阈值转换为相似度判断（`consistency.py:63`）：

```python
if distance > (1 - threshold):  # threshold 默认 0.85
    continue  # 不够相似，跳过
```

#### delete by source

重新提取某文件前，先删除该文件产生的所有向量：

```python
# vector.py:108-114
def delete_by_source(self, source_path):
    for table_name in ["node_vectors", "event_vectors", "edge_vectors"]:
        tbl = self.db.open_table(table_name)
        tbl.delete(f"source_path = '{source_path}'")
```

#### search_all（跨类型搜索）

同时搜三张表，合并后按距离排序：

```python
# vector.py:87-106
def search_all(self, query_embedding, limit=10):
    all_results = []
    for table_name, kind in [
        ("node_vectors", "node"),
        ("event_vectors", "event"),
        ("edge_vectors", "edge"),
    ]:
        tbl = self.db.open_table(table_name)
        results = tbl.search(query_embedding).limit(limit).to_list()
        for r in results:
            r["kind"] = kind  # 标注来源类型
        all_results.extend(results)
    all_results.sort(key=lambda x: x.get("_distance", float("inf")))
    return all_results[:limit]
```

## 6. 派生关系

```
┌─────────┐     提取(extract)      ┌──────────┐
│  .md    │ ───────────────────── │  SQLite  │
│ 文件    │ ───────────────────── │  (nodes, │
│ (truth) │     嵌入(embed)       │  edges,  │
│         │ ───────────────────── │  events) │
└─────────┘                       └──────────┘
      │                              │
      │           一致性校验           │
      │           (consistency)       │
      │              ↓                 │
      │         ┌──────────┐          │
      └───────→ │ LanceDB  │ ←────────┘
                │ (vectors)│
                └──────────┘
```

- **.md 是 source of truth**：所有数据最终来源于 .md 文件
- **SQLite 是 .md 的结构化投影**：提取出的实体/关系/事件存入 SQLite，加上状态管理（valid/invalid、processed_files、consistency_log）
- **LanceDB 是 SQLite 的向量投影**：nodes/events/edges 的文本嵌入向量存入 LanceDB
- **重建方向**：`.md → SQLite + LanceDB` 可全量重建（删除后两者，重新提取所有 .md）；反过来不行（SQLite/LanceDB 无法还原 .md 的人类可读文本）

## 7. 为什么不用图数据库

概念上，yacmemo 的数据模型天然是图：

- 实体（nodes）是节点
- 关系（edges）是边
- provenance（source_path）是边到文件的追溯
- 时态校验是时态边（valid_from / invalid_at）

但工程上选择 SQLite 而非 Neo4j 等图数据库，理由如下：

| 维度 | 图数据库（Neo4j） | SQLite | 判断 |
|------|------------------|--------|------|
| 数据规模 | 为百万级节点设计 | 几百文件、几千节点 | SQLite 够用 |
| 查询模式 | 多跳遍历（A→B→C→D） | 一跳查询为主（查某实体的关系、查某实体的历史） | 不需要多跳 |
| 运维成本 | 需独立服务、JVM、内存管理 | 零配置，单文件嵌入 | SQLite 优势明显 |
| 事务支持 | 有但不如 SQLite 成熟 | ACID 完整 | SQLite 够用 |
| 多用户 | 需要额外设计 | user_id 列隔离简单 | SQLite 简单 |
| 数据模型 | 不变（节点+边） | 不变（nodes+edges） | 未来可迁移 |

**关键判断**：数据模型本身是图，但查询模式不是图。当前所有查询都是"查某个实体的属性/关系/历史"，不需要"从 A 出发走三跳找到 D"这种图遍历。如果未来需要多跳推理，数据模型不变，可以把 SQLite 中的数据导入图数据库。

## 8. 备份与恢复策略

| 存储层 | 备份策略 | 恢复策略 |
|-------|---------|---------|
| .md 文件 | git 版本控制 + 定时复制 | `git checkout` 或文件复制 |
| SQLite | WAL 模式 + 定时 `sqlite3 system.db ".backup"` | 恢复备份文件 |
| LanceDB | 不单独备份（可从 .md + SQLite 重建） | 删除后重新提取全量 .md |

**核心原则：只备份 .md 和 SQLite，LanceDB 是可重建的派生数据。**

恢复流程：
1. 恢复 .md 文件（从 git 或备份）
2. 恢复 SQLite（从备份文件）
3. 如果 LanceDB 丢失或损坏，删除 `data/lancedb/{user_id}/` 目录，对全量 .md 重新提取即可重建

SQLite 的 WAL 模式（`db.py:148`）提供了崩溃恢复能力：即使进程意外终止，WAL 文件中的已提交事务不会丢失。

## 9. 多用户隔离

### 9.1 隔离策略

| 存储层 | 隔离方式 | 代码位置 |
|-------|---------|---------|
| .md 文件 | 按用户不同 `memory_root` 目录 | `users.memory_root` |
| SQLite | 系统级单库 + `user_id` 列隔离 | 所有业务表都有 `user_id` |
| LanceDB | 按用户不同目录 | `data/lancedb/{user_id}/` |

### 9.2 SQLite 隔离实现

所有业务表（nodes、edges、events、processed_files、consistency_log、split_files）都有 `user_id` 列，所有 CRUD 方法的第一参数都是 `user_id`：

```python
# db.py — 所有方法签名都以 user_id 开头
def upsert_node(self, user_id: str, name: str, type_: str, ...) -> str:
def get_node(self, user_id: str, node_id: str) -> dict | None:
def get_node_history(self, user_id: str, name: str) -> list[dict]:
def get_all_valid_nodes(self, user_id: str) -> list[dict]:
def delete_nodes_by_source(self, user_id: str, source_path: str):
# ... 所有方法都是 user_id 优先
```

### 9.3 级联清理

删除用户时，级联清理所有业务表数据（`db.py:167-175`）：

```python
def remove_user(self, user_id: str):
    """Delete a user and all their data."""
    with self.conn:
        self.conn.execute("DELETE FROM nodes WHERE user_id=?", (user_id,))
        self.conn.execute("DELETE FROM edges WHERE user_id=?", (user_id,))
        self.conn.execute("DELETE FROM events WHERE user_id=?", (user_id,))
        self.conn.execute("DELETE FROM processed_files WHERE user_id=?", (user_id,))
        self.conn.execute("DELETE FROM consistency_log WHERE user_id=?", (user_id,))
        self.conn.execute("DELETE FROM users WHERE id=?", (user_id,))
```

注意：`split_files` 表未在 `remove_user` 中清理，这是一个已知的待修复项。

### 9.4 配置继承

每个用户可以覆盖全局 LLM/embedding API key（`config.py:92-116`）：

```python
def resolve_llm(self, user: UserConfig) -> LLMConfig:
    api_key = user.llm_api_key or self.llm.api_key  # 用户级优先，空则继承全局
    return LLMConfig(base_url=..., api_key=api_key, ...)

def resolve_embedding(self, user: UserConfig) -> EmbeddingConfig:
    api_key = user.embedding_api_key or self.embedding.api_key
    return EmbeddingConfig(base_url=..., api_key=api_key, ...)
```

这意味着不同用户可以使用不同的 API key（不同的模型供应商或不同的计费账户），但共享同一套全局配置（模型名称、超时时间等）。

### 9.5 MCP Server 用户指定

MCP server 启动时必须指定 `--user` 参数，确定当前服务的用户身份，所有操作在该用户的隔离空间内进行。