> [English](en/04-consistency.md) | 简体中文

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
- `forced` 次数：**违约率的直接度量**（P4 核心指标）；
- `uncovered` 次数：主题注册制硬拦截的触发计数（2026-09-18 增补，见 01-architecture §6.1）。

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
- 自愈联动：涉及笔记被编辑/外部修改/删除时，collisions 行删除并重算（`remove_collisions_involving` + audit 的 hash 级 resync）；**清除有留痕**（2026-09-19 增补）：重索引后不再命中的旧对即"自动清除"，计数上报三处——`memory_edit` / `memory_edit_section` 成功返回追加"（自动清除过期冲突对 N 对）"、审计概览与快照的概览行、`memory_audit` 输出的 `== 自动清除过期冲突对 ==` 行。合并型编辑由此有了可见信号：**看到清除计数 = 这次编辑消解了语义撞车**；
- **机器产物区不进 obs 空间**：journal/audit/ 审计快照与 curator/ 提案报告是系统派生输出，不是记忆——其处置行 `- [时间] 已处理 ...` 会被 `parse_observations` 当作伪 observation（类别=时间戳）且跨快照高度相似，故 `_index_note` 对机器产物区（`store._machine_zones`）跳过 obs 索引与 D2，D1 候选同样排除（快照标题同构、提案报告归一化剥日期后互相撞）。审计快照仍可被 FTS/note 级向量检索到。

**D3 悬空链接**：`[[目标]]` 不匹配任何既有标题 → audit 列出（多为手误或待创建）。机器产物区豁免——审计快照会引用上一轮悬空链接的原文，源笔记删除后不得自指点名（与 D1/D2 同口径）。引用目标不含文字的标记（如文献引注 `[[1,28,28]]`）不算链接，已过滤。

**D4 游离文件**：不属于任何注册主题的 markdown 被 audit 点名（免注册区 journal/、archive/、curator/ 豁免）。这是主题注册制的执法机制——主题之外不留藏身之处，注册制本身见 [01-architecture.md](01-architecture.md) §十三。**写路径已硬拦截（2026-09-17 增补）**：工具面（memory_write / save 新建 / move 目标）不可能再制造游离，D4 转为兜底——管 Obsidian 手建、注销后遗等工具面之外的游离；写入拦截与 D4 共用同一覆盖判定（`_path_covered`），口径永远一致。

**D5 悬空主题卡**：注册表 `卡:` 字段指向不存在的 abstract（restructure/手工编辑 TOPICS.md 的遗留）。D3 只扫笔记正文里的链接，注册表自身无校验——audit 补位点名，处置靠修注册表或重建卡。

已知盲区（接受）：措辞距离远但逻辑矛盾的 D2 漏检（如"sys_user 无 role_color"vs"新增 role_color 字段"）。堵住它的代价是全量 LLM 扫描——v1 的教训，不做。

**覆盖边界（刻意）**：D2 只作用于 observation 行（`- [类别] 文本`），不比对笔记全文——正文散文里的重复事实不产生 ⚠（写约定要求事实行写进 observation 正是为此）。这类重复的兜底是 note 级向量检索把两篇同时召回、交读取时的主模型裁决，加上 audit 的 D1 标题扫描。

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

guard_events(id, ts, kind,     -- refused / forced / uncovered（clear_all 与 reindex 不清除）
             attempted_title, matched_path)

audit_actions(id, kind,        -- 人的处置：D1-D5 / P（提案裁决），重跑不重放
              action)            -- dismissed（D 类）；adopted / dismissed（P 类）

audit_exec_events(seq, issue_id, kind,
                  event,       -- agent 汇报：executing / progress / executed / blocked
                  note, identity, ts)  -- 追加式时间线；verified 由 audit() 自动追加
```

## 四、审计问题的执行工作流（2026-09-25 增补）

判断、执行、验证三种角色分离，谁都不越界：

```
审计发现 ──▶ 人判断 ──▶ agent 执行 ──▶ 审计验证
          忽略（误报）   memory_audit_update    已执行且不再报告
          派发（复制执行指令）  汇报过程          = 复审通过（自动）
```

- **人**（WebUI）：忽略误报；把真问题用「复制执行指令」派给任意 agent（指令内嵌汇报约定）；
- **agent**（MCP `memory_audit_update`）：`executing` 接手 → `progress` 过程 → `executed` 完成 / `blocked` 受阻；identity 自动入时间线；复审不归 agent 管；
- **系统**（`memory_audit`）：唯一验证者——上轮有执行记录、本轮不再报告的问题自动追加 `verified` 封口事件（派生验证，无需人工点头）；再次出现则标「复发」。

状态是读取端派生，不落库：最新执行事件 + 最新审计报告即可推出 待处理 / 执行中 / 已执行待复审 / 复审通过 / 已忽略。

**质量提案同管线 + 自动结案**：提案条目（P 类）走同一状态机，但人没有「采纳」动作——**派发即采纳**（复制执行指令），人只做忽略；条目状态由执行事件与忽略记录推出。提案全部条目收口（executed / dismissed）后，文件头部自动打 `> 状态：已结案` 标记，`memory_search` 默认不再返回它（显式 `memory_read` 仍可读）——已派发过的工作不会因旧报告仍可检索而被重复执行。

**结案调和（2026-09-25 增补，双向自愈）**：结案标记是双向的——agent 也可以在确认全部条目完成后用 `memory_edit` 手工打标（markdown 事实源优先）。审计时：①文件已标结案、条目只有旧口径「已采纳」行而无执行事件的 → 补记 `executed`（identity=reconcile，幂等）；②同日复审给文件追加了新条目、旧标记已与事实不符的 → 自动撤标、状态行复位，提案回到待处理并重新可检索（新条目处理完毕后会再次自动打标）。无任何记录的条目不补记（复审后无法区分新老，保持可见待人裁决）。「复审通过」封口只作用于 D 类，提案条目完成即已执行。条目全局序号跨提案节与复审节连续编号（复审节打印序号从头计），`P:<file>:<index>` 由此保持唯一。

## 五、阈值与调参

| 参数 | 默认 | 含义 | 调整依据 |
|---|---|---|---|
| `title_similarity_threshold` | 0.85 | 归一化标题相似度拦截线 | refused 日志中误拦比例高 → 降；漏拦 → 升 |
| `collision_cosine_threshold` | 0.86 | D2 撞车 cosine 线 | audit 误报多 → 升；漏报 → 降 |
| `force_confirm_threshold` | 3 | 24h 内 force 免确认次数 | 模型守约能力强可放宽 |

## 六、与 v1 一致性层的对照

| | v1（已退役） | v2 |
|---|---|---|
| 判断者 | 1.3B 小模型自报置信度 | 确定性规则 + 主模型（读取时） |
| 失效方式 | 自动标记 `valid=0`（可静默错杀） | 不删除不隐藏，⚠ 标注并存 |
| 覆盖时机 | 文档说逐节点实时，实际每写全量扫描 | 写时增量 + 审计全量 |
| 成本 | 每写 O(全部记忆) 次 LLM 调用 | 零 LLM |
| 违约可见性 | 错杀不可见 | 全部可见（⚠ 标注 / audit 报告） |