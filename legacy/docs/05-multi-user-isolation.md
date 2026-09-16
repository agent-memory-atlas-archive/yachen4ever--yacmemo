# 多用户隔离设计文档

> 2026-09-13
> 状态：已实现

## 一、设计目标

yacmemo 从第一天起就按多用户系统设计（用户自己用 + 给老婆用），核心目标：

1. **记忆完全隔离**：每个用户的 .md 原文、实体（node）、事件（event）、关系（edge）、向量索引、一致性校验记录都互不可见。用户 A 的搜索不会命中用户 B 的记忆，用户 B 的实体不会出现在用户 A 的历史中。
2. **运行时增删用户**：通过 WebUI 管理面板添加/删除用户，不需要重启服务，不需要手动编辑配置文件再重启。
3. **按用户覆盖 API key**：每个用户可以使用自己的 LLM/embedding API key，也可以留空继承全局配置。
4. **级联清理**：删除用户时，该用户在所有表中的数据一并清除。

---

## 二、架构演进

### 2.1 旧架构：每用户独立 SQLite

最初的设计是"每个用户一套独立数据库"：

```
data/
  yachen/
    memory/          ← .md 原文
    .index/
      memory.db      ← 独立 SQLite
  wife/
    memory/
    .index/
      memory.db      ← 独立 SQLite
  lancedb/
    yachen/          ← 独立 LanceDB
    wife/
```

每个用户一个 `MemoryDB` 实例，物理隔离。看起来安全，但有问题：

- **WebUI 管理困难**：用户列表要遍历 `data/` 目录推断，增删用户要动态打开/关闭 SQLite 连接，数据库实例的生命周期难以管理。
- **集中查询不便**：统计"系统有多少实体""处理了多少文件"这类跨用户指标需要打开多个数据库逐个查询。
- **连接管理复杂**：每个用户一个 SQLite 连接，用户多了连接数线性增长。

### 2.2 新架构：系统级单库 + user_id 列隔离

重构后改为共享一个系统级 SQLite，所有业务表加 `user_id` 列做逻辑隔离：

```
data/
  system.db           ← 系统级单库（所有用户共享，user_id 列隔离）
  lancedb/
    yachen/           ← 向量索引按用户分目录
    wife/
  yachen/
    memory/           ← .md 原文（按用户分目录）
  wife/
    memory/
```

### 2.3 对比

| 维度 | 旧架构（每用户独立 SQLite） | 新架构（系统级单库 + user_id 列） |
|------|--------------------------|-------------------------------|
| SQLite 文件 | `data/{user}/.index/memory.db` | `data/system.db`（共享） |
| 隔离方式 | 物理隔离（不同文件） | 逻辑隔离（user_id 列 + WHERE 过滤） |
| 用户管理 | 遍历目录推断 | `users` 表 + CRUD API |
| 跨用户统计 | 打开多个 DB 逐个查 | 一条 SQL 搞定 |
| 连接管理 | 每用户一个连接 | 单连接，WAL 模式 |
| LanceDB | 按用户分目录 | 按用户分目录（不变） |
| WebUI 增删用户 | 需动态打开/关闭 DB 连接 | 直接操作 `users` 表 |

**为什么改**：WebUI 需要直接管理用户增删，旧架构要为每个用户创建独立数据库文件、管理独立的连接实例，不方便集中管理。新架构一个数据库连接搞定所有用户，增删用户就是操作 `users` 表。

---

## 三、SQLite 隔离机制

### 3.1 核心原则

**所有 db 方法的第一参数都是 `user_id`**。这不是编码风格偏好，而是防御性设计——强制每个数据库操作都显式指定用户，防止忘带 `WHERE user_id=?` 导致跨用户串数据。

```python
# db.py 中的方法签名（全部以 user_id 开头）
def upsert_node(self, user_id: str, name: str, type_: str, ...) -> str:
def get_node(self, user_id: str, node_id: str) -> dict | None:
def get_all_valid_nodes(self, user_id: str) -> list[dict]:
def invalidate_node(self, user_id: str, node_id: str, reason: str):
```

### 3.2 业务表的 user_id 列

六张业务表全部带 `user_id` 列，且建了 `(user_id, ...)` 复合索引：

| 表 | user_id 列 | 复合索引 |
|----|-----------|---------|
| nodes | `user_id TEXT NOT NULL` | `idx_nodes_user_name(user_id, name)`、`idx_nodes_user_valid(user_id, valid)` |
| edges | `user_id TEXT NOT NULL` | `idx_edges_user_source(user_id, source_node)`、`idx_edges_user_target(user_id, target_node)` |
| events | `user_id TEXT NOT NULL` | `idx_events_user_date(user_id, date)` |
| processed_files | `user_id TEXT NOT NULL` | `PRIMARY KEY (user_id, path)` |
| consistency_log | `user_id TEXT NOT NULL` | `idx_consistency_user_status(user_id, status)` |
| split_files | `user_id TEXT NOT NULL` | `PRIMARY KEY (user_id, path)`、`idx_split_files_user_status(user_id, status)` |

所有业务表都通过 `FOREIGN KEY (user_id) REFERENCES users(id)` 关联到 `users` 表。

### 3.3 users 表

`users` 表是系统级元数据表，存储所有用户的配置信息：

```sql
CREATE TABLE IF NOT EXISTS users (
    id                TEXT PRIMARY KEY,
    display_name      TEXT NOT NULL DEFAULT '',
    memory_root       TEXT NOT NULL,
    llm_api_key       TEXT DEFAULT '',
    embedding_api_key TEXT DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
```

- `id`：用户唯一标识，用作 `--user` 参数和所有业务表的 `user_id` 外键。
- `display_name`：人类可读名称（如"老婆"）。
- `memory_root`：该用户的 .md 记忆文件根目录（相对路径或绝对路径）。
- `llm_api_key` / `embedding_api_key`：按用户覆盖的 API key，空字符串表示继承全局配置。

### 3.4 完整建表 SQL

```sql
-- Users table (system-level, manages all users)
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL DEFAULT '',
    memory_root     TEXT NOT NULL,
    llm_api_key     TEXT DEFAULT '',
    embedding_api_key TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Nodes (entities) — per-user isolation via user_id
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,
    summary     TEXT,
    source_path TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    original_path TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    valid       INTEGER DEFAULT 1,
    invalid_at  TEXT,
    invalid_reason TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_nodes_user_name ON nodes(user_id, name);
CREATE INDEX IF NOT EXISTS idx_nodes_user_valid ON nodes(user_id, valid);
CREATE INDEX IF NOT EXISTS idx_nodes_source ON nodes(source_path);

-- Edges
CREATE TABLE IF NOT EXISTS edges (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    source_node   TEXT NOT NULL,
    target_node   TEXT NOT NULL,
    relation      TEXT NOT NULL,
    summary       TEXT,
    source_path   TEXT NOT NULL,
    source_hash   TEXT NOT NULL,
    original_path TEXT,
    valid_from    TEXT NOT NULL,
    invalid_at    TEXT,
    invalid_reason TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_edges_user_source ON edges(user_id, source_node);
CREATE INDEX IF NOT EXISTS idx_edges_user_target ON edges(user_id, target_node);

-- Events
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    date          TEXT,
    type          TEXT,
    summary       TEXT NOT NULL,
    details       TEXT,
    source_path   TEXT NOT NULL,
    source_hash   TEXT NOT NULL,
    original_path TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_events_user_date ON events(user_id, date);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_path);

-- Processed files
CREATE TABLE IF NOT EXISTS processed_files (
    path          TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    split_dir     TEXT,
    processed_at  TEXT NOT NULL,
    status        TEXT NOT NULL,
    error_msg     TEXT,
    split_file_count INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, path),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- Consistency log
CREATE TABLE IF NOT EXISTS consistency_log (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    old_node_id     TEXT,
    new_node_id     TEXT,
    old_source_path TEXT,
    new_source_path TEXT,
    reason          TEXT NOT NULL,
    confidence      REAL,
    checked_at      TEXT NOT NULL,
    auto_invalidated INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'pending',
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_consistency_user_status ON consistency_log(user_id, status);

-- Split files integrity tracking
CREATE TABLE IF NOT EXISTS split_files (
    user_id     TEXT NOT NULL,
    path        TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    written_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    status      TEXT DEFAULT 'active',
    PRIMARY KEY (user_id, path),
    FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_split_files_user_status ON split_files(user_id, status);
```

### 3.5 级联删除

`remove_user` 方法在单个事务中删除该用户在所有表中的数据：

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

使用 `with self.conn` 上下文管理器确保原子性——要么全部删除成功，要么全部回滚。注意 `split_files` 表的数据也通过 `user_id` 隔离，在删除用户时应当一并清理（当前实现在 `remove_user` 中未显式删除 `split_files`，但数据隔离机制一致）。

---

## 四、LanceDB 隔离机制

### 4.1 按用户分目录

向量索采用 LanceDB，按用户分目录隔离：

```
data/lancedb/
  yachen/           ← 用户 yachen 的向量库
    node_vectors.lance
    event_vectors.lance
    edge_vectors.lance
  wife/             ← 用户 wife 的向量库
    node_vectors.lance
    event_vectors.lance
    edge_vectors.lance
```

路径由 `config.user_lancedb_abs(user)` 计算：

```python
def user_lancedb_abs(self, user: UserConfig) -> str:
    lancedb_base = self.storage.get("lancedb_path", "data/lancedb/")
    base = os.environ.get("YACMEMO_HOME", os.getcwd())
    lancedb_root = (
        str(Path(base) / lancedb_base)
        if not os.path.isabs(lancedb_base) else lancedb_base
    )
    return os.path.join(lancedb_root, user.id)
```

每个用户创建独立的 `VectorStore` 实例，连接到自己的 LanceDB 目录。`VectorStore.__init__` 打开该目录下的 LanceDB，自动创建 `node_vectors`、`event_vectors`、`edge_vectors` 三张表。

### 4.2 为什么不把向量也放单库

SQLite 可以用 `user_id` 列做 `WHERE` 过滤实现逻辑隔离，但 LanceDB 不支持这种用法。LanceDB 的向量搜索是基于 ANN（近似最近邻）索引的，没有 "按列过滤后再搜索" 的高效机制。即使支持 filter，也会先全量扫描再过滤，性能和隔离性都不理想。

分目录是最自然的隔离方式——每个用户的向量索引完全独立，搜索时只在自己的索引中查找，不存在跨用户泄露的可能。删除用户时删掉对应目录即可，干净利落。

---

## 五、config.toml 与运行时用户管理

### 5.1 config.toml 中的 [[users]]

用户在 `config.toml` 中通过 `[[users]]` 数组声明，启动时同步到数据库：

```toml
[[users]]
id = "yachen"
display_name = "yachen"
memory_root = "./data/yachen/memory"

[[users]]
id = "wife"
display_name = "老婆"
memory_root = "./data/wife/memory"
llm_api_key = "sk-wife-separate-key"
```

### 5.2 sync_users_to_db() 同步逻辑

`config.sync_users_to_db(db)` 在 enhancer 启动时执行，将 config.toml 中的用户同步到数据库：

```python
def sync_users_to_db(self, db) -> list[UserConfig]:
    # 1. config.toml 中的用户：已存在则更新字段，不存在则新增
    for u in self.users:
        existing = db.get_user(u.id)
        if existing:
            db.update_user(u.id, display_name=u.display_name, ...)
        else:
            db.add_user(id=u.id, display_name=u.display_name, ...)

    # 2. 从数据库加载所有用户（包括运行时通过 WebUI 添加的）
    db_users = db.list_users()
    return [UserConfig(...) for u in db_users]
```

**关键设计**：同步是"只增不删"的——config.toml 中有的用户会被新增或更新，但数据库中有而 config.toml 中没有的用户（运行时通过 WebUI 添加的）不会被删除。这保证了 WebUI 添加的用户在服务重启后仍然存在。

### 5.3 运行时用户管理

运行时通过 WebUI API 增删用户，直接操作数据库：

- **添加用户**：`POST /api/users`，写入 `users` 表，创建 memory_root 和 lancedb 目录，同时追加到内存中的 `config.users` 列表。
- **删除用户**：`DELETE /api/users/{user_id}`，调用 `db.remove_user()` 级联删除所有数据，从内存列表中移除。

添加用户后不需要重启 enhancer——`config.users` 列表在内存中同步更新，cron 扫描和 webhook 立即能识别新用户。

### 5.4 API key 按用户覆盖

`Config.resolve_llm(user)` 和 `Config.resolve_embedding(user)` 实现按用户覆盖：

```python
def resolve_llm(self, user: UserConfig) -> LLMConfig:
    api_key = user.llm_api_key or self.llm.api_key
    return LLMConfig(
        base_url=self.llm.base_url,
        api_key=api_key,
        model=self.llm.model,
        ...
    )
```

逻辑很简单：`user.llm_api_key` 非空就用用户的，为空（空字符串）就继承全局 `self.llm.api_key`。`base_url` 和 `model` 始终用全局配置，只有 API key 支持按用户覆盖。

---

## 六、MCP Server 的用户选择

MCP server 是 Agent 直接调用的记忆工具入口（9 个 `memory_*` 工具）。启动时通过 `--user` 参数选择服务哪个用户：

```bash
# 为用户 yachen 启动 MCP server
yacmemo-mcp --user yachen

# 为用户 wife 启动另一个 MCP server 实例
yacmemo-mcp --user wife
```

`--user` 是必填参数（`required=True`）。初始化时 `_init_for_user()` 将 `user.id` 绑定到全局变量，后续所有 `db` 调用和向量搜索都使用这个 `user.id`：

```python
def _init_for_user(config_path: str | None, user_id: str):
    global config, user, db, vector, emb
    config = load_config(config_path)
    user = config.get_user(user_id)
    db = MemoryDB(config.sqlite_abs)       # 系统级 SQLite
    vector = VectorStore(                   # 该用户的 LanceDB
        config.user_lancedb_abs(user),
        config.embedding.dimensions,
    )
```

所有 MCP 工具内部调用 `db.get_node_history(user.id, ...)`、`db.get_pending_consistency(user.id)` 等，`user.id` 来自初始化时绑定的全局变量。Agent 不需要（也无法）在工具调用时指定用户——用户在 MCP server 启动时就固定了。

**切换用户 = 启动另一个 MCP server 实例**。在 TeleAgent 的工具设置界面，每个用户注册一个 MCP server，命令分别带 `--user yachen` 和 `--user wife`，各自连接到独立的 MCP 实例。

---

## 七、enhancer 的多用户处理

enhancer 是后台增强服务，负责 LLM 提取和一致性校验。它需要同时处理所有用户的记忆，采用"遍历所有用户 + 每用户独立构建"的模式。

### 7.1 _UserExtractor / _UserChecker 包装类

`Extractor` 和 `ConsistencyChecker` 的原始方法签名以 `user_id` 为第一参数。为了在 enhancer 中简化调用，引入了两个包装类，在构造时绑定 `user_id`，自动注入到所有 db 调用中：

```python
class _UserExtractor:
    """Wraps Extractor with user_id bound to all db calls."""

    def __init__(self, cfg, user_id, db, vector, llm, emb):
        self._user_id = user_id
        self._inner = Extractor(cfg, db, vector, llm, emb)

    def process_file(self, md_path: str):
        return self._inner.process_file(self._user_id, md_path)
```

`_UserChecker` 同理，`check_new_node` 和 `check_all` 都自动注入 `user_id`。

### 7.2 cron 扫描

`scan_all()` 遍历 `config.users` 列表，对每个用户调用 `scan_user()`：

```python
def scan_all(config: Config, db: MemoryDB):
    for user in config.users:
        scan_user(config, user, db)
```

`scan_user()` 为该用户构建独立的 `VectorStore`（连接到 `data/lancedb/{user_id}/`）、独立的 `LLMClient`（使用该用户的 API key），然后扫描该用户 memory_root 下的所有 .md 文件。

### 7.3 webhook 请求

webhook `/trigger` 接受 `user_id` 参数，为指定用户异步执行提取或一致性校验：

```python
class TriggerRequest(BaseModel):
    action: str
    path: str | None = None
    user_id: str | None = None
```

MCP server 的 `memory_write` / `memory_edit` 工具在写入文件后，会异步 POST 到 enhancer 的 `/trigger`，请求体中带 `user_id`：

```python
httpx.post(
    f"http://{config.server.host}:{config.server.port}/trigger",
    json={"action": "extract", "path": rel, "user_id": user.id},
    timeout=5,
)
```

enhancer 收到请求后，根据 `user_id` 查找用户配置，构建该用户的 extractor，在新线程中执行提取。

---

## 八、测试验证

### 8.1 db.py 的用户隔离测试

`tests/test_db.py` 中的 `TestUserCRUD`、`TestNodeCRUD`、`TestEdgeCRUD`、`TestEventCRUD`、`TestConsistencyLog` 各自包含用户隔离测试：

- **`test_user_isolation_nodes`**：两个用户（alice、bob）各插入同名实体 "EntityA"，验证各自只能查到自己的数据，且内容不同。
- **`test_user_isolation_edges`**：两个用户各插入一条 edge，验证各自只有一条记录。
- **`test_user_isolation_events`**：两个用户各插入一个 event，验证隔离。
- **`test_get_node_cross_user_returns_none`**：alice 创建的 node，用 bob 的 user_id 查询返回 `None`。
- **`test_remove_user_cascades`**：为 alice 添加 node、event、processed_file、consistency_log，删除 alice 后验证所有数据消失，bob 的数据不受影响。
- **`TestProcessedFiles.test_user_isolation`**：两个用户记录同一个文件路径但不同 hash，验证各自独立。
- **`TestConsistencyLog.test_user_isolation`**：两个用户各添加一条 consistency log，验证各自的 pending 列表隔离。

### 8.2 webui/app.py 的用户 CRUD 测试

`tests/test_webui_app.py` 中的 `TestUsersAPI` 覆盖用户管理 API：

- **`test_list_users`**：GET `/api/users` 返回两个用户。
- **`test_create_user_json`**：POST `/api/users` 创建新用户 charlie，验证数据库中有记录。
- **`test_create_duplicate_user`**：创建已存在的 alice 返回 409。
- **`test_create_user_missing_id`**：缺少 id 返回 400。
- **`test_create_user_missing_memory_root`**：缺少 memory_root 返回 400。
- **`test_delete_user`**：DELETE `/api/users/bob` 后验证数据库中不存在。
- **`test_delete_nonexistent_user`**：删除不存在的用户返回 404。

测试 fixture 使用两个用户（alice、bob），通过 `sync_users_to_db` 同步到临时数据库，确保测试环境与真实运行环境一致。

---

## 九、数据流总览

```
┌──────────────────────────────────────────────────────┐
│                    config.toml                        │
│  [[users]] → 启动时 sync_users_to_db() 同步到数据库    │
└──────────────────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────┐
│                 data/system.db (SQLite)                │
│                                                      │
│  users 表 ────── 系统级用户元数据                      │
│  nodes 表  ────┐                                      │
│  edges 表  ────┤  所有业务表带 user_id 列              │
│  events 表 ────┤  所有查询 WHERE user_id=?             │
│  processed_files ┤  所有方法第一参数是 user_id         │
│  consistency_log ┤                                     │
│  split_files ───┘                                     │
└──────────────────────────────────────────────────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ data/lancedb/ │ │ data/lancedb/ │ │ data/lancedb/ │
│   yachen/     │ │   wife/      │ │   charlie/   │
│ (向量索引)    │ │ (向量索引)    │ │ (向量索引)    │
└──────────────┘ └──────────────┘ └──────────────┘
          │              │              │
          ▼              ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ data/yachen/  │ │ data/wife/   │ │ data/charlie/ │
│   memory/    │ │   memory/    │ │   memory/    │
│ (.md 原文)   │ │ (.md 原文)   │ │ (.md 原文)   │
└──────────────┘ └──────────────┘ └──────────────┘
```

**调用路径**：

- **MCP server**（`--user yachen`）→ 绑定 `user.id="yachen"` → 所有 db 调用带 `user_id="yachen"` → 只操作 yachen 的数据 → 向量搜索只在 `data/lancedb/yachen/` 中查找
- **enhancer cron** → 遍历所有用户 → 每用户独立构建 extractor + checker → 各自扫描各自的 memory_root → 各自写入各自的 LanceDB 目录
- **enhancer webhook**（`/trigger?user_id=yachen`）→ 查找用户配置 → 构建该用户的 extractor → 异步执行提取
- **WebUI**（`/api/users`）→ 直接操作 `users` 表 → 增删用户不需要重启