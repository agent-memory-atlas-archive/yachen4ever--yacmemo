---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'bfe277d6-3971-4cc5-b1af-e4aab2da27cb'
  PropagateID: 'bfe277d6-3971-4cc5-b1af-e4aab2da27cb'
  ReservedCode1: 'fa5d0439-7162-41f2-8be9-80f495e5b4dc'
  ReservedCode2: 'fa5d0439-7162-41f2-8be9-80f495e5b4dc'
---

# 一致性校验设计文档

> 2026-09-13
> 状态：已实现

## 一、要解决什么问题

Agent 跨 session 是失忆的。

一个典型场景：T1 记了"服务器端口是 8080"，T2 改成了 9721 但 Agent 不记得去更新旧记录，T3 搜到"8080"给出错误答案。

这不是 Agent 不够聪明，是架构上缺一层"记忆维护"——没有人回头检查旧记忆和新事实是否矛盾。

一致性校验就是这层"回头检查"——在新增事实时，扫描已有事实，发现矛盾就标记旧的失效，保留完整历史。

---

## 二、核心思路

不是两两比较所有事实（O(n²) 的 LLM 调用不可行），而是**向量粗筛 + LLM 精判**两步走：

1. 新事实提取出来后，用 embedding 向量在 LanceDB 中搜索语义最相似的旧事实（top-5）
2. 对每个相似候选，调 LLM 判断"旧事实和新事实是否矛盾"
3. 矛盾 + 高置信度 → 自动标记旧事实失效
4. 矛盾 + 低置信度 → 记日志等人工确认

---

## 三、触发时机

两个入口，互为补充：

### 入口 A：实时校验（提取后立即触发）

在 `extractor.py` 的 `process_file` 中，Layer 2 提取完 entity/event 写入 SQLite + LanceDB 后，立刻对每个新提取的 node 调用 `checker.check_new_node(user_id, node_id)`。

```
Agent 写 .md → 提取管道拆分+提取 entity/event → 写 SQLite + LanceDB
→ 对每个新 node 调 check_new_node → 向量搜索相似旧 node → LLM 判断矛盾 → 处理
```

好处：矛盾发现得快，不用等到 cron 扫描。坏处：如果 LLM 调用失败，这次校验就跳过了——靠 cron 兜底。

### 入口 B：定时全量扫描（cron 触发）

`enhancer.py` 注册了 `consistency_cron`（默认每天凌晨 3 点），调用 `checker.check_all(user_id)`，遍历该用户的所有 valid nodes 逐个做 `check_new_node`。

这是兜底机制——实时校验漏掉的（LLM 失败、向量索引未就绪等），cron 会补上。

两个入口调用的是同一个 `check_new_node` 方法，逻辑完全一致。

---

## 四、校验单个节点的完整流程

以 `check_new_node(user_id, node_id)` 为例：

### 第一步：获取新节点内容，生成 embedding

```python
node = self.db.get_node(user_id, node_id)
node_text = f"{node['name']}: {node['summary']}"
query_emb = self.emb.embed_one(node_text)
```

比如新节点是 `name="服务器配置", summary="端口改为 9721"`，则 `node_text = "服务器配置: 端口改为 9721"`，调 embedding API（Qwen3-Embedding-0.6B-4bit-DWQ，1024 维）得到查询向量。

### 第二步：向量搜索找相似的旧节点（粗筛）

```python
similar = self.vector.search_nodes(query_emb, limit=5)
```

在 LanceDB 的 `node_vectors` 表中搜索 top-5 最相似的节点。这一步是粗筛——用语义相似度把可能矛盾的候选找出来，避免对所有节点两两调 LLM。

然后过滤掉两类：

- **自身**：`match_id == node_id` 跳过（不能和自己矛盾）
- **距离太远的**：`distance > (1 - threshold)` 跳过（默认 threshold=0.85，即 LanceDB distance > 0.15 就不算相关）

```python
if distance > (1 - threshold):
    continue
```

这个 threshold 的含义：LanceDB 默认用 L2 距离（余弦距离），distance 越小越相似。0 表示完全相同，2 表示完全相反。`1 - 0.85 = 0.15` 意味着只看距离很近的候选。

### 第三步：对每个相似候选，调 LLM 判断是否矛盾（精判）

```python
result = self._judge(old_node, node)
```

LLM 收到的 prompt：

```
SYSTEM: 你是一致性校验助手。判断给定的旧事实和新事实是否矛盾。以JSON格式输出：
{
  "contradictory": true或false,
  "current": "A"或"B"（哪个是当前有效的）,
  "reason": "判断理由（简洁）",
  "confidence": 0.0到1.0的置信度
}

USER:
旧事实（记录时间: 2026-09-10）: 服务器配置: 端口为 8080
新事实（记录时间: 2026-09-13）: 服务器配置: 端口改为 9721

请判断这两个事实是否矛盾。如果矛盾，哪个是当前有效的？
```

LLM 返回 JSON：

```json
{
  "contradictory": true,
  "current": "B",
  "reason": "端口从 8080 改为 9721，新事实是当前有效配置",
  "confidence": 0.95
}
```

关键参数：
- `response_format: {"type": "json_object"}` 强制纯 JSON 输出
- `enable_thinking: false` 关掉思考模式（简单矛盾判断不需要 thinking，速度快 4 倍）
- `max_tokens: 512`（关 thinking 后够用）
- `timeout: 30` 秒

LLM 调用失败时的容错：返回 `{"contradictory": False, "confidence": 0.0, ...}`，不阻塞流程。

### 第四步：根据置信度决定处理方式

```python
auto = (confidence >= self.config.consistency.confidence_threshold  # 默认 0.8
        and self.config.consistency.auto_invalidate)
```

| 置信度 | 处理方式 | consistency_log status |
|---|---|---|
| >= 0.8 且 auto_invalidate=True | 自动标记旧节点失效 | `auto_invalidated` |
| < 0.8 | 不失效，只记日志等人工确认 | `pending` |

**自动失效的操作**：

```python
self.db.invalidate_node(user_id, match_id,
    f"superseded_by:{node_id}: {reason}")
```

在 SQLite 中执行：

```sql
UPDATE nodes SET valid=0, invalid_at=<now>, invalid_reason='superseded_by:<new_id>: <reason>'
WHERE id=<old_id> AND user_id=<user_id>
```

**不管哪种情况，都写 consistency_log**：

```python
self.db.add_consistency_log(
    user_id,
    old_node_id=match_id,           # 被判定矛盾的旧节点
    new_node_id=node_id,             # 触发校验的新节点
    old_source_path=old_node.get("source_path", ""),
    new_source_path=node.get("source_path", ""),
    reason=reason,                   # LLM 给出的矛盾理由
    confidence=confidence,           # LLM 置信度
    auto_invalidated=auto,           # 是否自动失效了
)
```

---

## 五、关键设计选择与理由

### 5.1 为什么不直接删旧事实

旧事实标记 `valid=0` 不删除。原因：

- **保留完整历史**：可以查"服务器端口这个配置实体的演变轨迹"——`get_node_history(user_id, name)` 返回同名实体的所有版本，包括已失效的
- **可追溯**：每条失效记录都有 `invalid_at`（何时失效）和 `invalid_reason`（为什么失效，含 `superseded_by:{new_id}` 指向取代它的新事实）
- **可恢复**：如果 LLM 误判了，人工确认后可以把 `valid` 改回 1（虽然目前没有自动恢复机制，但数据没丢）

这是借鉴 Graphiti 的 temporal invalidation 思路：事实有时态，新事实不否定旧事实的存在，只否定它的有效性。

### 5.2 为什么用向量搜索粗筛而不是两两比较

如果有 1000 个节点，两两比较是 1000 × 1000 / 2 = 50 万次 LLM 调用，不现实。

向量搜索 top-5 把候选缩小到 5 个，每个新节点最多调 5 次 LLM。1000 个节点的全量扫描 = 5000 次 LLM 调用，每次 ~5 秒 = 约 7 小时（关 thinking 后 ~1.5 小时）。虽然仍然不少，但 cron 是凌晨跑，不影响使用。

这是工程上的必要妥协——向量粗筛可能漏掉某些语义不相似但逻辑矛盾的候选（比如"端口是 8080"和"防火墙规则开放了 8080"措辞不同但相关）。但漏掉的会在下次 cron 扫描中再查一次，最终一致。

### 5.3 为什么是半自动而不是全自动

LLM 可能误判。典型误判场景：

- **不同实体的同名属性**："服务器端口是 8080"和"数据库端口是 9721"——都含"端口"但指不同的东西，向量相似但不是矛盾
- **补充而非矛盾**："服务器端口是 8080"和"服务器还配了 SSL 证书"——语义相关但不是矛盾
- **时间维度**："2026 年 1 月端口是 8080"和"2026 年 9 月端口是 9721"——是时间演进不是矛盾

高置信度（> 0.8）时 LLM 比较确定，自动处理没问题。低置信度时 LLM 在"不确定是否矛盾"和"不确定哪个是当前有效的"之间摇摆，让人工看一眼更安全。

人工审核的负担控制：低置信度的矛盾只在 consistency_log 里记一条 pending 记录，不阻塞任何流程。用户/Agent 可以通过 WebUI 一致性看板或 MCP 工具随时查看和处理。

### 5.4 为什么 threshold 是 0.85 / 0.8

**similarity_threshold = 0.85**（向量距离阈值）：
- LanceDB 默认用余弦距离，0 = 完全相同，2 = 完全相反
- `distance > (1 - 0.85) = 0.15` 就跳过——只看非常相似的候选
- 这个值是经验值，太低会漏掉矛盾，太高会多调 LLM 浪费 token
- 可通过 config.toml 的 `[consistency] similarity_threshold` 调整

**confidence_threshold = 0.8**（LLM 置信度阈值）：
- LLM 自己输出的 confidence 在 0-1 之间
- >= 0.8 自动失效，< 0.8 记 pending
- 这个值偏向保守——宁可多让人工确认，也不要误失效

### 5.5 为什么 consistency_log 不删

consistency_log 是追加日志，不删除。原因：

- 审计追溯：可以查"系统曾经发现过哪些矛盾、怎么处理的"
- 自动失效的记录也有 `auto_invalidated=1` 标记，和人工确认的 `manual_confirmed` / `manual_dismissed` 区分开
- WebUI 一致性看板展示"自动失效历史"用这个表

---

## 六、数据模型

### 6.1 nodes 表中的时态字段

```sql
valid          INTEGER DEFAULT 1,   -- 1=有效, 0=已失效
invalid_at     TEXT,                -- 失效时间（ISO 8601）
invalid_reason TEXT                 -- 失效原因，格式: "superseded_by:{new_node_id}: {reason}"
```

### 6.2 consistency_log 表

```sql
CREATE TABLE consistency_log (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    old_node_id     TEXT,            -- 被判定矛盾的旧节点
    new_node_id     TEXT,            -- 触发校验的新节点
    old_source_path TEXT,            -- 旧事实来源文件（provenance）
    new_source_path TEXT,            -- 新事实来源文件（provenance）
    reason          TEXT NOT NULL,   -- LLM 给出的矛盾理由
    confidence      REAL,            -- LLM 置信度（0-1）
    checked_at      TEXT NOT NULL,   -- 校验时间
    auto_invalidated INTEGER DEFAULT 0,  -- 1=自动失效, 0=待人工确认
    status          TEXT DEFAULT 'pending'  -- pending/auto_invalidated/manual_confirmed/manual_dismissed
);
```

status 流转：

```
pending → manual_confirmed（人工确认旧事实失效）
pending → manual_dismissed（人工忽略此矛盾）
         auto_invalidated（高置信度自动失效，不经过 pending）
```

---

## 七、LLM 调用细节

### 7.1 模型与参数

- 模型：Ling-3.0-tiny-MLX-4bit（7.9B/1.3B MoE，本地 mlx-serve 推理）
- `response_format: {"type": "json_object"}` 强制纯 JSON
- `enable_thinking: false` 关闭思考模式（简单判断不需要，速度快 4 倍）
- `max_tokens: 512`（关 thinking 后够用）
- `timeout: 30` 秒

### 7.2 容错

| 场景 | 处理 |
|---|---|
| LLM 返回非 JSON | `LLMJSONError` 捕获，返回 `{"contradictory": False}`，不阻塞 |
| LLM 调用超时 | `Exception` 捕获，返回 `{"contradictory": False}`，记 warning |
| Embedding 调用失败 | `check_new_node` 直接 return，跳过此节点的校验 |
| 向量搜索无结果 | 不调 LLM，正常结束 |

所有失败都不阻塞提取管道——一致性校验是增强层，失败不影响 Layer 1 的 .md 文件和 Layer 2 的 entity 提取。

---

## 八、Agent 和用户如何查看与处理

### 8.1 MCP 工具

| 工具 | 功能 |
|---|---|
| `memory_consistency_status` | 查看待确认项列表（status=pending 的 consistency_log） |
| `memory_consistency_resolve` | 处理待确认项：`confirm` 确认旧事实失效，`dismiss` 忽略 |

`memory_consistency_resolve` 支持短 ID（输入前 8 位即可），内部会自动匹配完整的 UUID。

### 8.2 WebUI

一致性看板页面（`/admin/consistency/{user_id}`）展示三张表：

1. **待确认矛盾**：pending 状态的 consistency_log，每条有"确认失效"和"忽略"按钮
2. **自动失效历史**：auto_invalidated=1 的记录
3. **已失效实体**：valid=0 的 nodes，展示失效时间和原因

### 8.3 实体历史

`memory_history(entity_name)` 返回同名实体的所有版本，包括已失效的，按创建时间排序。这让用户/Agent 可以看到"这个实体是怎么演变的"。

---

## 九、配置项

```toml
[consistency]
similarity_threshold = 0.85    # 向量距离阈值，distance > (1-threshold) 的候选跳过
confidence_threshold = 0.8     # LLM 置信度 >= 此值时自动失效
auto_invalidate = true          # 是否启用自动失效（false = 全部记 pending）
```

---

## 十、完整数据流

### 10.1 实时校验（提取后触发）

```
Agent 写 .md
→ MCP write 工具写文件 → POST /trigger {action: extract}
→ Enhancer 收到 webhook
  → Extractor.process_file
    → LLM 拆分+提取 → 写拆分文件 → 写 SQLite nodes + LanceDB vectors
    → 对每个新 node：
      → check_new_node(user_id, node_id)
        → embed_one("{name}: {summary}") → 1024 维向量
        → vector.search_nodes(vec, limit=5) → top-5 相似旧节点
        → 过滤自身 + 距离太远的
        → 对每个候选：
          → _judge(old_node, new_node) → LLM 判断矛盾
          → contradictory=True：
            → confidence >= 0.8 → invalidate_node(old) + add_consistency_log(auto)
            → confidence < 0.8 → add_consistency_log(pending)
          → contradictory=False：跳过
→ MCP 返回 write 成功（不等校验完成）
```

### 10.2 定时全量扫描（cron）

```
每天 03:00
→ scan_all(config, db)
  → 对每个 user：
    → list_md_files → 对每个 .md 调 process_file（hash 变了才重新提取）
    → check_all(user_id)
      → get_all_valid_nodes → 对每个 node 调 check_new_node
    → check_split_integrity（拆分文件完整性扫描）
```

---

## 十一、已知局限与未来改进

### 11.1 向量粗筛可能漏判

语义不相似但逻辑矛盾的候选会被向量搜索漏掉。例如"sys_user 表有 role_color 字段"和"sys_user 表结构不含 role_color"——措辞不同但语义有一定距离，可能 distance > 0.15 被跳过。

缓解：cron 全量扫描会重复检查，但用的是同一个向量搜索逻辑，仍可能漏。

未来改进：对同名实体直接做精确匹配比较（不依赖向量），或对同类实体做批量比较。

### 11.2 逐个调用 LLM 效率低

每个候选调一次 LLM，5 个候选 = 5 次调用。可以改为批量判断——一次 prompt 传入 5 对事实让 LLM 一次性返回 5 个判断结果。

### 11.3 没有自动恢复机制

如果 LLM 误判导致错误失效，目前只能人工发现后手动处理。可以加一个"恢复"操作：将 `valid` 改回 1，记录恢复时间和原因。

### 11.4 只检查 nodes 不检查 events

当前一致性校验只对 nodes（实体）做，不对 events（事件）做。事件之间一般不矛盾（"1 月部署了 A"和"3 月部署了 B"不矛盾），但如果出现"1 月部署了 A"和"1 月没有部署 A"就需要事件级校验。

未来可以扩展 `check_new_event` 方法，逻辑类似。