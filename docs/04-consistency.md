---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '35f45d3d-f751-41a0-a58c-4beefcc8bf4a'
  PropagateID: '35f45d3d-f751-41a0-a58c-4beefcc8bf4a'
  ReservedCode1: '4c18b8b4-dbd8-4434-8afc-ecc5685cb828'
  ReservedCode2: '4c18b8b4-dbd8-4434-8afc-ecc5685cb828'
---

# 一致性机制

> 核心立场：**失效语义优于检测语义**。系统从不删除、从不隐藏任何记忆；违约可见、可逆、可数。
> 删除只发生在用户明确指令（`memory_delete` / WebUI 删除按钮），且每次删除自动产生 git 快照、历史可恢复——系统自身永不主动删除，这与 v1"自动失效"有本质区别。
> 代码位置：`store.py`（守卫 + D2 + 游离检测）、`detectors.py`（D1/D3 + 归一化）、`index_db.py`（collisions / guard_events）。

## 一、为什么一致性要三层防线

记忆腐化的根源是**约定是概率的**：模型再强也有不守"先查后写、就地更新"的时候（session 过长、上下文压缩、小模型）。三种应对的失效语义排序：

1. 自动失效（v1 的做法）：错杀静默发生，正确的记忆消失——**最坏**；
2. 纯约定无检测：违约不可见地积累——**次坏**；
3. **API 强制 + 确定性检测 + 呈现**：违约要么被阻止，要么新旧并存带标注——**本设计**。

## 二、三层防线

### 第 1 层：API 强制（确定，写路径）

| 工具 | 强制行为 |
|---|---|
| `memory_write` | 归一化标题模糊比对 ≥ 0.85 → 拒绝新建，提示改用 `memory_edit`；journal/ 豁免 |
| `memory_edit` | `old_string` 必须存在且唯一，否则拒绝并列出命中行号 |
| `memory_edit_section` | 小节标题必须存在且唯一，否则列出可用小节/行号 |
| `memory_move` | 目标已存在拒绝 |

拒绝消息都是可执行的下一步指令，agent 拿到即能自纠。

**force 两级确认**：`force=true` 是越过近重名守卫的唯一通道，但 24 小时滚动窗口内 forced 事件达到 `force_confirm_threshold`（默认 3）后，裸 force 拒绝，需 `force=true, force_confirm=true` 双参放行（显式人工确认语义）。拒绝信息列出候选已有笔记。全过程落在 `guard_events`：

- `refused` 次数：守卫拦截频率（调 `title_similarity_threshold` 的依据）；
- `forced` 次数：**违约率的直接度量**（P4 核心指标）。

### 第 2 层：确定性检测（确定，零 LLM）

**D1 标题重复**：守卫只拦"写时"，存量笔记（历史写入/force 越过/外部创建）由 audit 全量两两扫描兜底（`detectors.d1_scan`，归一化后 rapidfuzz，O(n²) 字符串运算，个人规模毫秒级）。

**D2 observation 语义撞车**（写时增量，`store._d2_check`）：

```
每条新 observation 向量 → obs_vectors top-5（排除同笔记）
→ 逐候选计算真实 cosine（不假设向量已归一化）
→ cosine ≥ 0.86 → collisions 表记 open（含双方文本与分数）
```

- 增量式：只比新写的行，不跑全量；旧值经 vec_cache 零成本复用；
- **刻意不裁决**：不判断"是否矛盾"、不决定"谁有效"——只标记"疑似在说同一件事"；
- 自愈联动：涉及笔记被编辑/外部修改/删除时，collisions 行删除并重算（`remove_collisions_involving` + audit 的 hash 级 resync）。

**D3 悬空链接**：`[[目标]]` 不匹配任何既有标题 → audit 列出（多为手误或待创建）。引用目标不含文字的标记（如文献引注 `[[1,28,28]]`）不算链接，已过滤。

**D4 游离文件**：不属于任何注册主题的 markdown 被 audit 点名（免注册区 journal/、archive/、curator/ 豁免）。这是主题注册制的执法机制——主题之外不留藏身之处，注册制本身见 [01-architecture.md](01-architecture.md) §十三。

已知盲区（接受）：措辞距离远但逻辑矛盾的 D2 漏检（如"sys_user 无 role_color"vs"新增 role_color 字段"）。堵住它的代价是全量 LLM 扫描——v1 的教训，不做。

### 第 3 层：主模型裁决（读取时刻）

`memory_search` 结果内联 `⚠` 标注（来自 collisions 表）：

```
1. 端口配置说明 (score 0.0281, fts+vector)
   path: 端口配置说明.md
   ⚠ 与 [[yacmemo部署配置]] 疑似重复（score 0.91）— 建议读两篇后用 memory_edit 合并。对方内容: 服务端口为 9721
```

读取的时刻就是修复的时刻：agent 读两篇 → `memory_edit` 合并 → 冲突对自然消失。全屋最强的模型（主模型）做最难的判断，且只在真模糊时才发生；人类可随时 `memory_audit` 兜底。

## 三、数据模型

```sql
collisions(id, kind,           -- obs / title
           a_path, b_path, a_text, b_text, score,
           detected_at, status)  -- open / resolved / dismissed

guard_events(id, ts, kind,     -- refused / forced（clear_all 与 reindex 不清除）
             attempted_title, matched_path)
```

## 四、阈值与调参

| 参数 | 默认 | 含义 | 调整依据 |
|---|---|---|---|
| `title_similarity_threshold` | 0.85 | 归一化标题相似度拦截线 | refused 日志中误拦比例高 → 降；漏拦 → 升 |
| `collision_cosine_threshold` | 0.86 | D2 撞车 cosine 线 | audit 误报多 → 升；漏报 → 降 |
| `force_confirm_threshold` | 3 | 24h 内 force 免确认次数 | 模型守约能力强可放宽 |

## 五、与 v1 一致性层的对照

| | v1（已退役） | v2 |
|---|---|---|
| 判断者 | 1.3B 小模型自报置信度 | 确定性规则 + 主模型（读取时） |
| 失效方式 | 自动标记 `valid=0`（可静默错杀） | 不删除不隐藏，⚠ 标注并存 |
| 覆盖时机 | 文档说逐节点实时，实际每写全量扫描 | 写时增量 + 审计全量 |
| 成本 | 每写 O(全部记忆) 次 LLM 调用 | 零 LLM |
| 违约可见性 | 错杀不可见 | 全部可见（⚠ 标注 / audit 报告） |