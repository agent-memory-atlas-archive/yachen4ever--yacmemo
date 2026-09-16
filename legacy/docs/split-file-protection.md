# 拆分文件保护设计文档

> 2026-09-13
> 状态：已实现

## 一、问题分析

### 1.1 背景回顾

yacmemo 三层架构中，Layer 2 的独立 LLM 会将 Agent 写的粗粒度 .md 原文拆分为细粒度拆分文件，放在原文旁的同名目录下：

```
memory/
  01-数据门户.md              ← Layer 1（Agent 写，自由格式）
  01-数据门户/               ← Layer 2（LLM 拆分，有 frontmatter）
    ├── feat-a.md
    ├── fix-b.md
    └── ui-c.md
```

SQLite 的 entity/event 记录通过 `source_path` 指向拆分文件（provenance 指针）。

### 1.2 已有防护

| 场景 | 防护 | 实现 |
|---|---|---|
| Agent 通过 MCP write/edit 写拆分目录 | **有防护** | `safe_write`/`safe_edit` 调 `is_in_split_dir` 拦截，返回拒绝信息 |
| cron 扫描误处理拆分目录内的文件 | **有防护** | `list_md_files` 调 `is_in_split_dir` 过滤掉拆分目录内的 .md |

### 1.3 未防护的风险

**风险 A：用户手动编辑拆分文件**

用户用 Obsidian、vim 或任何编辑器直接改拆分目录下的 .md 文件。改动不会触发重新提取（因为 cron 只扫描原文不扫描拆分文件），导致：

1. 拆分文件内容与 SQLite entity 记录不一致——SQLite 里的 summary 是旧版，拆分文件已是新版
2. 下次原文 .md 变更触发重新提取时，`_write_split_files` 直接覆盖拆分文件——用户手动改的内容静默丢失，无警告无备份

**风险 B：webhook 缺少拆分目录检查**

enhancer 的 `/trigger` 端点没有 `is_in_split_dir` 检查。虽然 MCP 层拦截了 Agent 写拆分目录，但 `/trigger` 是 HTTP 端点，可以直接被调用指向拆分文件，导致提取管道处理拆分文件而非原文。

**风险 C：拆分文件被删除**

用户或误操作删除了拆分目录下的文件。SQLite 里的 entity 记录仍指向已不存在的 `source_path`。Agent 搜索到实体后尝试 Read 拆分文件会 404。

---

## 二、方案设计

### 2.1 核心思路：hash 校验 + 冲突保留

提取管道每次写拆分文件时，记录文件内容 hash 到 SQLite。下次操作前比对当前磁盘文件 hash 与记录的 hash：

- **hash 一致**：文件未被手动改过，安全覆盖
- **hash 不一致**：文件被手动改过，不直接覆盖，保留用户改动为 `.conflict` 备份文件
- **文件不存在但 SQLite 有记录**：文件被删除，记录 stale 状态

### 2.2 数据模型变更

新增 `split_files` 表：

```sql
CREATE TABLE IF NOT EXISTS split_files (
    user_id     TEXT NOT NULL,
    path        TEXT NOT NULL,          -- 拆分文件相对路径
    content_hash TEXT NOT NULL,         -- 提取管道写入时的内容 hash
    written_by  TEXT NOT NULL,          -- "extractor"（管道写）/ "user_edited"（检测到用户改）
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    status      TEXT DEFAULT 'active',  -- active / stale / conflict
    PRIMARY KEY (user_id, path)
);
```

与 `processed_files` 的区别：`processed_files` 跟踪原文 .md 的处理状态，`split_files` 跟踪拆分文件本身的完整性状态。

### 2.3 提取管道改动（extractor.py）

在 `_write_split_files` 写每个拆分文件前：

```
对每个待写文件 path:
  1. 查 split_files 表获取上次写入时的 hash
  2. 如果文件在磁盘上存在:
     a. 计算磁盘文件的当前 hash
     b. 当前 hash == 记录 hash → 文件未被手动改 → 安全覆盖
     c. 当前 hash != 记录 hash → 文件被手动改 → 保留为 {path}.conflict.{timestamp} → 覆盖写新文件 → 记 status=conflict
  3. 如果文件不存在但 split_files 有记录 → status=stale（文件被删过，现在重新创建）
  4. 写完文件后 → 记录新 hash 到 split_files 表
```

### 2.4 cron 扫描改动（enhancer.py / fs_utils.py）

新增 `check_split_integrity` 函数，在 cron 扫描时运行：

```
对每个已处理的原文 .md:
  1. 找到对应的拆分目录
  2. 遍历拆分目录下所有 .md 文件
  3. 对每个拆分文件:
     a. 查 split_files 表
     b. 文件存在但 hash 不匹配 → 标记 status=user_edited
     c. 文件不存在但表里有记录 → 标记 status=stale（被删）
     d. 文件存在且 hash 匹配 → status=active
  4. 返回异常列表供日志和 WebUI 展示
```

### 2.5 webhook 防护（enhancer.py）

`/trigger` 端点的 `extract` 分支增加 `is_in_split_dir` 检查：

```python
if is_in_split_dir(md_path, memory_root):
    return {"status": "error", "error": "Cannot process split directory file"}
```

### 2.6 MCP 工具新增（mcp_server.py）

新增 `memory_split_status` 工具，让 Agent 查看拆分文件完整性状态：

```
返回: [{path, status, message}]
  status: active / user_edited / stale / conflict
  message: "文件正常" / "检测到用户手动修改" / "文件被删除" / "用户修改已保留为 .conflict 文件"
```

### 2.7 用户手动修改的恢复路径

当检测到 `.conflict` 文件时，用户有两条路径：

1. **保留用户改动**：将 `.conflict` 文件内容合并到新拆分文件中（手动或用 diff 工具），然后删除 `.conflict` 文件
2. **丢弃用户改动**：直接删除 `.conflict` 文件，接受提取管道的新版本

Agent 不会自动处理 `.conflict` 文件——这需要人类判断。

---

## 三、实现清单

| 文件 | 改动 |
|---|---|
| `db.py` | 新增 `split_files` 表 + `record_split_file` / `get_split_file` / `list_split_files` / `update_split_file_status` 方法 |
| `extractor.py` | `_write_split_files` 增加 hash 校验 + 冲突保留逻辑 |
| `enhancer.py` | `/trigger` 增加 `is_in_split_dir` 检查；cron 扫描增加 `check_split_integrity` |
| `fs_utils.py` | 新增 `list_split_files_in_dir` 辅助函数 |
| `mcp_server.py` | 新增 `memory_split_status` 工具 |
| `webui/app.py` | `/api/split-status/{user_id}` 端点 |
| `tests/` | split file 保护相关测试 |

---

## 四、不变量

1. **Agent 永远不碰拆分文件**——MCP write/edit 拦截 + USER.md 约束
2. **提取管道覆盖前先检查**——hash 不匹配时保留 .conflict 文件
3. **用户改动不会静默丢失**——冲突时生成 .conflict 备份，Agent 可查看状态
4. **split_files 表是拆分文件完整性的唯一真相**——hash 比对以此为准