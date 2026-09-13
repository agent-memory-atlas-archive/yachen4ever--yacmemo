---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '72428fa9-bf76-43cf-bc25-2a410b6bb564'
  PropagateID: '72428fa9-bf76-43cf-bc25-2a410b6bb564'
  ReservedCode1: 'd5d26982-2247-4198-815b-394f1c0344d8'
  ReservedCode2: 'd5d26982-2247-4198-815b-394f1c0344d8'
---

# Provenance 指针与搜索机制设计文档

> 2026-09-13
> 状态：已实现

## 一、provenance 的设计理由

### 1.1 问题：Agent 幻觉无法验证

Agent 从记忆中搜索到一个事实后，面临一个信任问题：**这个结论真的来自这个文件吗？**

没有 provenance 的事实是"孤儿"——Agent 无法回溯到原文核对，只能盲目相信提取结果。如果 LLM 提取时理解有偏差，或摘要丢失了关键限定条件，错误会沿搜索→生成链路传播，且无法定位源头。

### 1.2 方案：每个事实都带指针

yacmemo 的核心设计是：**每条被提取的 entity/event/edge 都记录它来自哪个文件**。Agent 搜索到结果后，看到 `source_path` 字段就知道这个事实来自哪个拆分文件，需要验证时直接 `memory_read` 读源文件核对。

以一个具体场景贯穿全文：Agent 搜索"role_color 字段什么时候加的"。

```
Agent: memory_search("role_color 字段什么时候加的")
→ 返回:
  1. [node] role_color: sys_user 表新增角色色标字段
     source: 01-数据门户/feat-a.md (distance: 0.05)
  
Agent 看到摘要，但想确认细节——这个字段到底是什么类型？有没有默认值？
→ memory_read("01-数据门户/feat-a.md")
→ 读到拆分文件原文，确认：ALTER TABLE sys_user ADD role_color VARCHAR(8) DEFAULT 'blue'

Agent 还想看完整上下文——这个改动是在哪个工作记录里做的？
→ memory_read("01-数据门户.md")
→ 读到原文，确认这是 2026-08-15 数据门户迭代的工作记录
```

没有 provenance，Agent 搜索到"role_color: sys_user 表新增角色色标字段"后，没有任何线索去验证或补充细节。有了 provenance，Agent 可以按需追溯到任意粒度。

---

## 二、两层指针设计

### 2.1 为什么是两层

yacmemo 的文件结构是三层的：

```
memory/
  01-数据门户.md                  ← 原文（Agent 写，自由格式，完整工作记录）
  01-数据门户/                    ← 拆分目录（LLM 提取，按事项拆分）
    ├── feat-a.md                 ← 拆分文件（事项级，有 frontmatter）
    ├── fix-b.md
    └── ui-c.md
```

原文是一个完整的工作记录，可能包含多个事项（新功能、修 bug、UI 调整）。拆分文件是 LLM 按事项拆分后的产物，每个文件聚焦一个独立事项。

两层指针对应两种追溯粒度：

| 指针 | 存储位置 | 指向 | 粒度 | Agent 何时用 |
|---|---|---|---|---|
| `source_path` | SQLite nodes/edges/events 表 | 拆分文件（如 `01-数据门户/feat-a.md`） | 事项级 | 想看某个事项的细节 |
| frontmatter `source` | 拆分文件 YAML frontmatter | 原文（如 `01-数据门户.md`） | 完整上下文 | 想看整个工作记录的来龙去脉 |

### 2.2 第一层：entity → 拆分文件

SQLite 中每条 entity/event/edge 记录都有 `source_path` 字段，指向拆分文件的相对路径。

以 `db.py` 中 nodes 表为例：

```sql
CREATE TABLE IF NOT EXISTS nodes (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL,
    summary       TEXT,
    source_path   TEXT NOT NULL,    -- ← 第一层指针：指向拆分文件
    source_hash   TEXT NOT NULL,
    original_path TEXT,              -- ← 也有 original_path，但 frontmatter 是更规范的方式
    created_at    TEXT NOT NULL,
    ...
);
```

edges 表和 events 表同样有 `source_path` 和 `original_path` 字段。

提取管道 `extractor.py` 在写入 entity 时设置这两个字段：

```python
# extractor.py:148-153
node_id = self.db.upsert_node(
    user_id,
    name=name, type_=ent_type, summary=ent_summary,
    source_path=split_rel,        # 拆分文件相对路径，如 "01-数据门户/feat-a.md"
    source_hash=shash,
    original_path=rel_path,        # 原文相对路径，如 "01-数据门户.md"
)
```

向量表也带 `source_path`（`vector.py:13-18`），搜索结果直接返回来源：

```python
_NODE_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 1024)),
    pa.field("text", pa.string()),
    pa.field("source_path", pa.string()),   # ← 向量也带来源
])
```

### 2.3 第二层：拆分文件 → 原文

拆分文件的 YAML frontmatter 中有 `source` 字段，指向原文文件名：

```yaml
---
source: 01-数据门户.md
created: 2026-08-15
updated: 2026-08-15
---

# 新增 role_color 字段

ALTER TABLE sys_user ADD role_color VARCHAR(8) DEFAULT 'blue'
...
```

这层指针在 `extractor.py:250-253` 写入：

```python
lines = [
    "---",
    f"source: {os.path.basename(original_path)}",   # ← 第二层指针：原文文件名
    f"created: {now}",
    f"updated: {now}",
    "---",
    ...
]
```

Agent 读拆分文件后，从 frontmatter 的 `source` 字段知道原文是 `01-数据门户.md`，需要完整上下文时再读原文。

### 2.4 为什么不用 original_path 字段代替 frontmatter

SQLite 表里已经有 `original_path` 字段，为什么还要在拆分文件 frontmatter 里再写一遍？

因为 **Agent 读的是文件，不是数据库**。Agent 通过 `memory_read` 读拆分文件后，拿到的是文件内容（含 frontmatter），不需要再查数据库就知道原文是谁。`original_path` 字段是给提取管道和一致性校验用的，frontmatter `source` 是给 Agent 读的。

---

## 三、搜索机制

yacmemo 提供四种搜索工具，覆盖不同需求：

### 3.1 语义搜索 memory_search

**用途**：按概念搜索，找到语义相关的实体/事件/关系。

**代码**：`mcp_server.py:104-130`

```python
@mcp.tool()
def memory_search(query: str, limit: int = 10) -> str:
    query_emb = emb.embed_one(query)               # query → 1024 维向量
    results = vector.search_all(query_emb, limit)   # 搜三张向量表
    # 格式化输出，每条带 source_path
    for r in results:
        lines.append(f"{i}. [{kind}] {text}")
        lines.append(f"   source: {source} (distance: {dist:.4f})")
```

**搜索范围**：`vector.search_all()`（`vector.py:87-106`）搜 `node_vectors`、`event_vectors`、`edge_vectors` 三张 LanceDB 表，合并结果按距离排序：

```python
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
            r["kind"] = kind
        all_results.extend(results)
    all_results.sort(key=lambda x: x.get("_distance", float("inf")))
    return all_results[:limit]
```

**返回格式**：

```
1. [node] role_color: sys_user 表新增角色色标字段
   source: 01-数据门户/feat-a.md (distance: 0.0512)
2. [event] 2026-08-15 schema_change: sys_user 表新增 role_color 字段
   source: 01-数据门户/feat-a.md (distance: 0.0834)
3. [edge] role_color → belongs_to → sys_user
   source: 01-数据门户/feat-a.md (distance: 0.1023)
```

每条结果都带 `kind`（node/event/edge）、`text`（摘要）、`source_path`（来源文件）、`_distance`（距离，越小越相关）。

### 3.2 正则搜索 memory_grep

**用途**：精确文本匹配，直接搜 .md 原文。

**代码**：`mcp_server.py:133-155`

```python
@mcp.tool()
def memory_grep(pattern: str, path: str = "") -> str:
    search_dir = _resolve_path(path) if path else _memory_root()
    result = subprocess.run(
        ["rg", "-n", "--no-heading", pattern, search_dir],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout
```

直接调 ripgrep 搜 memory_root 下所有 .md 文件，覆盖原文和拆分文件。返回格式是 ripgrep 标准输出：

```
01-数据门户/feat-a.md:5:ALTER TABLE sys_user ADD role_color VARCHAR(8) DEFAULT 'blue'
01-数据门户.md:23:- 新增 role_color 字段，用于角色色标管理
```

语义搜索找"概念相关"，grep 找"字面出现"。比如 Agent 想确认 `role_color` 这个词在哪些文件里出现过，grep 比 semantic search 更精确。

### 3.3 实体历史 memory_history

**用途**：查同一个实体的所有版本（含已失效），看事实随时间的演变。

**代码**：`mcp_server.py:242-262`

```python
@mcp.tool()
def memory_history(entity_name: str) -> str:
    history = db.get_node_history(user.id, entity_name)
    for h in history:
        status = "有效" if h["valid"] else f"已失效({h.get('invalid_reason', '?')})"
        lines.append(
            f"- [{status}] {h['name']} ({h['type']}): {h.get('summary', '')}\n"
            f"  created: {h['created_at']}, source: {h['source_path']}"
        )
```

`db.get_node_history()`（`db.py:247-252`）查同名实体的所有记录，按创建时间排序，不过滤 valid 状态：

```python
def get_node_history(self, user_id, name):
    cur = self.conn.execute(
        "SELECT * FROM nodes WHERE user_id=? AND name=? ORDER BY created_at",
        (user_id, name),
    )
    return [dict(r) for r in cur.fetchall()]
```

**返回格式**：

```
- [有效] role_color (field): sys_user 表新增角色色标字段，VARCHAR(8) DEFAULT 'blue'
  created: 2026-08-15T10:30:00, source: 01-数据门户/feat-a.md
- [已失效(superseded_by:xxx)] role_color (field): sys_user 表新增角色色标字段
  created: 2026-08-10T08:00:00, source: 01-数据门户/feat-a.md
```

Agent 可以看到 `role_color` 实体有两个版本：旧版摘要较粗，新版补充了字段类型和默认值。旧版已被一致性校验自动标记失效，原因是"被新版本取代"。

### 3.4 文件读取 memory_read

**用途**：读指定路径的 .md 文件，用于追溯原文。

**代码**：`mcp_server.py:159-175`

```python
@mcp.tool()
def memory_read(path: str) -> str:
    full_path = _resolve_path(path)
    with open(full_path, encoding="utf-8") as f:
        return f.read()
```

Agent 从搜索结果拿到 `source_path` 后，用 `memory_read` 读拆分文件看细节；从拆分文件 frontmatter 拿到 `source` 后，用 `memory_read` 读原文看完整上下文。

---

## 四、搜索数据流

以"Agent 搜索 role_color 字段什么时候加的"为例，完整数据流：

```
Agent 调 memory_search("role_color 字段什么时候加的")
│
├─→ MCP server (mcp_server.py:112)
│   │
│   ├─→ emb.embed_one("role_color 字段什么时候加的")
│   │   → 1024 维浮点向量 [0.012, -0.034, 0.078, ...]
│   │
│   ├─→ vector.search_all(vec, limit=10)
│   │   │
│   │   ├─→ LanceDB: node_vectors.search(vec).limit(10)
│   │   │   → [{id:"a1b2", text:"role_color: sys_user 表新增角色色标字段",
│   │   │       source_path:"01-数据门户/feat-a.md", _distance:0.0512}]
│   │   │
│   │   ├─→ LanceDB: event_vectors.search(vec).limit(10)
│   │   │   → [{id:"c3d4", text:"2026-08-15 schema_change: sys_user 表新增 role_color 字段",
│   │   │       source_path:"01-数据门户/feat-a.md", _distance:0.0834}]
│   │   │
│   │   ├─→ LanceDB: edge_vectors.search(vec).limit(10)
│   │   │   → [{id:"e5f6", text:"role_color → belongs_to → sys_user",
│   │   │       source_path:"01-数据门户/feat-a.md", _distance:0.1023}]
│   │   │
│   │   └─→ 合并三表结果，按 _distance 升序排序，取前 10
│   │
│   └─→ 格式化输出:
│       1. [node] role_color: sys_user 表新增角色色标字段
│          source: 01-数据门户/feat-a.md (distance: 0.0512)
│       2. [event] 2026-08-15 schema_change: sys_user 表新增 role_color 字段
│          source: 01-数据门户/feat-a.md (distance: 0.0834)
│       3. [edge] role_color → belongs_to → sys_user
│          source: 01-数据门户/feat-a.md (distance: 0.1023)
│
├─→ Agent 看摘要，想确认字段类型和默认值
│   │
│   ├─→ memory_read("01-数据门户/feat-a.md")
│   │   → 读到拆分文件:
│   │     ---
│   │     source: 01-数据门户.md
│   │     ---
│   │     # 新增 role_color 字段
│   │     ALTER TABLE sys_user ADD role_color VARCHAR(8) DEFAULT 'blue'
│   │     ## 关联实体
│   │     - role_color: field
│   │     - sys_user: table
│   │
│   └─→ Agent 确认：VARCHAR(8), DEFAULT 'blue'
│
├─→ Agent 想看完整工作记录的上下文
│   │
│   ├─→ 从拆分文件 frontmatter 看到 source: 01-数据门户.md
│   │
│   ├─→ memory_read("01-数据门户.md")
│   │   → 读到原文:
│   │     # 2026-08-15 数据门户迭代
│   │     ## 新增功能
│   │     - 新增 role_color 字段，用于角色色标管理
│   │     - 新增用户列表按角色筛选功能
│   │     ## Bug 修复
│   │     - 修复用户列表排序错乱问题
│   │     ## UI 调整
│   │     - 角色色标颜色调整为可配置
│   │
│   └─→ Agent 确认：这是 2026-08-15 数据门户迭代的一部分，role_color 用于角色色标管理
```

---

## 五、搜索结果的 provenance 展示

`memory_search` 返回的每条结果都带 `source_path`，格式为 `来源文件 (distance: 距离值)`。这个设计让 Agent 在不额外调用工具的情况下就知道事实来源。

`mcp_server.py:119-127` 的格式化逻辑：

```python
for i, r in enumerate(results, 1):
    kind = r.get("kind", "?")        # node / event / edge
    text = r.get("text", "")         # 摘要文本
    source = r.get("source_path", "") # 来源拆分文件
    dist = r.get("_distance", 0)     # 向量距离
    lines.append(f"{i}. [{kind}] {text}")
    lines.append(f"   source: {source} (distance: {dist:.4f})")
```

`memory_history` 同样带 `source_path`（`mcp_server.py:255-260`）：

```python
lines.append(
    f"- [{status}] {h['name']} ({h['type']}): {h.get('summary', '')}\n"
    f"  created: {h['created_at']}, source: {h['source_path']}"
)
```

一致性校验待确认项也展示来源对比（`mcp_server.py:275-279`），让用户看到新旧事实分别来自哪个文件：

```python
lines.append(
    f"- [{p['id'][:8]}] 旧: {p.get('old_source_path', '?')}\n"
    f"  新: {p.get('new_source_path', '?')}\n"
    f"  理由: {p['reason']} (置信度: {p.get('confidence', 0):.2f})"
)
```

provenance 不是可选的装饰——它是每条事实的身份证。搜索结果没有 `source_path` 的事实不会被返回，因为 LanceDB 的 schema 里 `source_path` 是必填字段（`vector.py:17`）。

---

## 六、重新提取时的 provenance 维护

当原文 .md 被修改后重新提取，需要清理旧数据再写新数据，否则会出现指向已失效内容的悬空指针。

### 6.1 清理旧数据

`extractor.py:122-128`，重新提取前先按 `source_path` 清理旧数据：

```python
# Step 7: Clear old data for this source (re-extraction)
for split_file in self._list_split_files(split_dir, [f["name"] for f in files]):
    split_rel = to_rel_path(split_file, memory_root)
    self.db.delete_nodes_by_source(user_id, split_rel)    # 删 SQLite nodes
    self.db.delete_events_by_source(user_id, split_rel)   # 删 SQLite events
    self.db.delete_edges_by_source(user_id, split_rel)    # 删 SQLite edges
    self.vector.delete_by_source(split_rel)               # 删 LanceDB 向量
```

四个删除操作覆盖所有存储层：

| 操作 | 代码位置 | 清理什么 |
|---|---|---|
| `db.delete_nodes_by_source` | `db.py:258-261` | SQLite nodes 表中该 source_path 的实体记录 |
| `db.delete_events_by_source` | `db.py:304-307` | SQLite events 表中该 source_path 的事件记录 |
| `db.delete_edges_by_source` | `db.py:279-282` | SQLite edges 表中该 source_path 的关系记录 |
| `vector.delete_by_source` | `vector.py:108-114` | LanceDB 三张向量表中该 source_path 的向量 |

`vector.delete_by_source` 遍历三张向量表逐表删除：

```python
def delete_by_source(self, source_path):
    for table_name in ["node_vectors", "event_vectors", "edge_vectors"]:
        tbl = self.db.open_table(table_name)
        tbl.delete(f"source_path = '{source_path}'")
```

### 6.2 写入新数据

清理完成后，`extractor.py:135-180` 写入新的 entity/event，带新的 `source_path` 和 `original_path`：

```python
for f in files:
    split_file_path = os.path.join(split_dir, f["name"] + ".md")
    split_rel = to_rel_path(split_file_path, memory_root)   # 新的 source_path
    shash = file_hash(split_file_path)

    for ent in f.get("entities", []):
        node_id = self.db.upsert_node(
            user_id,
            name=name, type_=ent_type, summary=ent_summary,
            source_path=split_rel,          # 新 source_path
            source_hash=shash,
            original_path=rel_path,          # 原文路径不变
        )
        emb_vec = self.emb.embed_one(node_text)
        self.vector.upsert_node_vector(node_id, node_text, emb_vec, split_rel)
```

### 6.3 为什么先删后写，不是 upsert

因为拆分文件的粒度可能变——上次提取拆出 3 个文件，修改后可能拆出 4 个或 2 个。如果只做 upsert，旧的 `feat-d.md` 对应的 entity 不会被清理，会残留指向已不存在文件的悬空指针。先按 `source_path` 全删再全写，保证每个 entity 的 provenance 指针始终指向当前有效的拆分文件。

---

## 七、多粒度搜索策略

四种搜索工具覆盖不同需求，Agent 按场景选择：

| 工具 | 机制 | 适用场景 | 例子 |
|---|---|---|---|
| `memory_search` | 语义向量搜索 | 概念相关、模糊查询 | "role_color 字段什么时候加的" → 找到 role_color 实体和相关事件 |
| `memory_grep` | 正则文本匹配 | 精确文本、关键词定位 | `rg "role_color"` → 找到所有包含该词的文件和行 |
| `memory_history` | SQLite 按 name 查全版本 | 同实体的时态演变 | `memory_history("role_color")` → 看到字段定义从粗到细的演变 |
| `memory_read` | 直接读文件 | 追溯原文细节 | `memory_read("01-数据门户/feat-a.md")` → 读到字段类型和默认值 |

### 7.1 典型组合用法

Agent 研究一个技术决策时，通常组合使用：

```
1. memory_search("数据门户角色色标方案")
   → 语义搜索找到 role_color 实体和相关事件，看到 source_path

2. memory_read("01-数据门户/feat-a.md")
   → 读拆分文件，看到字段定义和关联实体

3. memory_history("role_color")
   → 发现该实体有两个版本，旧版已失效，了解字段定义的演变

4. memory_grep("role_color")
   → 确认 role_color 在所有文件中的出现位置，不遗漏其他引用

5. memory_read("01-数据门户.md")
   → 读原文，了解完整工作记录的上下文
```

### 7.2 搜索不替代阅读

语义搜索返回的是摘要（`text` 字段），不是原文。摘要由 LLM 生成，可能有信息损失。provenance 的意义正在于此：**搜索让 Agent 快速定位，阅读让 Agent 获得准确信息**。

Agent 不会仅凭搜索结果的摘要就下结论——它会沿着 provenance 指针读到拆分文件甚至原文，确认细节后才使用。这是 yacmemo 防幻觉的核心机制：搜索提供线索，provenance 提供验证路径，阅读提供准确事实。

---

## 八、代码索引

| 功能 | 文件 | 关键函数/方法 |
|---|---|---|
| 语义搜索 MCP 工具 | `mcp_server.py:104` | `memory_search()` |
| 向量搜索实现 | `vector.py:87` | `VectorStore.search_all()` |
| 节点向量搜索 | `vector.py:81` | `VectorStore.search_nodes()` |
| 按 source 删除向量 | `vector.py:108` | `VectorStore.delete_by_source()` |
| 正则搜索 MCP 工具 | `mcp_server.py:133` | `memory_grep()` |
| 文件读取 MCP 工具 | `mcp_server.py:159` | `memory_read()` |
| 实体历史 MCP 工具 | `mcp_server.py:242` | `memory_history()` |
| 实体历史 DB 查询 | `db.py:247` | `MemoryDB.get_node_history()` |
| 按 source 删 nodes | `db.py:258` | `MemoryDB.delete_nodes_by_source()` |
| 按 source 删 events | `db.py:304` | `MemoryDB.delete_events_by_source()` |
| 按 source 删 edges | `db.py:279` | `MemoryDB.delete_edges_by_source()` |
| 提取时设置 provenance | `extractor.py:148` | `Extractor.process_file()` |
| 拆分文件 frontmatter 写入 | `extractor.py:250` | `Extractor._write_split_files()` |
| 重新提取前清理旧数据 | `extractor.py:122` | `Extractor.process_file()` Step 7 |
| 一致性校验 provenance | `consistency.py:85` | `ConsistencyChecker.check_new_node()` |
| nodes 表 schema | `db.py:37` | `source_path` + `original_path` 字段 |
| 向量表 schema | `vector.py:13` | `source_path` 字段 |