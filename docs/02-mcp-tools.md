---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '40a3e2ba-39f4-4467-9c92-69f23eeb85d6'
  PropagateID: '40a3e2ba-39f4-4467-9c92-69f23eeb85d6'
  ReservedCode1: '3848ce31-0c5b-403d-9a5c-3d43cec1275c'
  ReservedCode2: '3848ce31-0c5b-403d-9a5c-3d43cec1275c'
---

# MCP 工具规格（13 个）

> 适用传输：stdio（`yacmemo-mcp`）与 HTTP（`yacmemo-server`），工具面完全一致。
> 所有工具返回人类可读文本；错误以中文消息直接返回（不抛协议错误），agent 可读可自纠。

## 总则

- **路径语义**：所有 `path` 参数接受相对 memory_root 的路径或笔记标题（标题精确匹配，见 `memory_read` 的解析顺序）。
- **同步索引**：所有写工具在返回成功前完成文件写入 → hash → embedding → FTS/向量/冲突表更新，单次典型开销 < 300ms。返回成功即索引可用。
- **git 快照**：所有写/删除/主题操作在成功后自动产生一个 git commit（`{tool}: {path}` 格式），记忆仓库永远 git-clean；首次写入自动 `git init`（含 `.index/` 忽略与 repo-local 身份）；git 不可用时降级为只写不快照，绝不阻塞记忆功能。外部直接改文件（Obsidian/vim）的改动由 `memory_audit` 自愈时统一快照（`external:` 前缀）。commit 身份按 `[[users].git_user_name/email]` > `[memory].git_user_name/email]` > 默认 `<id>` / `<id>@yacmemo.com` 解析，仓库已有身份绝不覆盖；最近一次快照失败会显示在 `memory_audit` 输出的 `== git ==` 行（消灭静默降级）。
- **失败语义**：文件永远是第一位。embedding 失败时内容照常写入、FTS 照常更新，仅向量/D2 缺失（下次写入或 `memory_audit` 自愈补齐）。
- **守卫拒绝是正常返回**（不是错误）：agent 应读拒绝消息并改用建议的工具。

## 1. memory_search

```
memory_search(query: str, limit: int = 10, kind: str = "hybrid") -> str
```

双通道检索 + Reciprocal Rank Fusion（k=60，只用名次不用分值）：

| 通道 | 机制 | 擅长 |
|---|---|---|
| fts | SQLite FTS5 trigram，BM25 排序 | 关键词式查询（子串字面匹配，token ≥ 3 字符） |
| vector | Qwen3-Embedding 最近笔记（cosine/L2） | 自然语句、同义改写 |

- `kind="fts"` / `"vector"` 强制单通道（评测用）；默认 hybrid。
- **查询措辞建议**：给 FTS 通道喂关键词（"端口 9721"、"restic 备份"），自然语句交给向量通道。混合查询（"yacmemo 端口"）两通道同时工作。

返回格式：

```
1. yacmemo部署配置 (score 0.0316, fts+vector)
   path: projects/yacmemo部署配置.md
   ⚠ 与 [[yacmemo部署记录0910]] 疑似重复（score 0.91）— 建议读两篇后用 memory_edit 合并。对方内容: 服务端口为 8080
```

**遇到 ⚠ 标注的处置约定**：先 `memory_read` 两篇 → `memory_edit` 合并 → 再回答用户。合并后冲突对自动消失（内容 hash 变化触发重算）。

## 2. memory_read

```
memory_read(path_or_title: str) -> str
```

解析顺序：① 相对路径精确存在 → ② 笔记标题精确匹配 → ③ 补 `.md` 后缀再试路径。

返回 = 标题头 + 正文 + `## 相关笔记`：

- `[[wiki-link]]` 目标存在的：列出标题 + 对方首条 observation（`via: link`）；
- 目标不存在的：标注"目标不存在"（agent 可顺手创建或清理）；
- 语义近邻 top-2（`via: vector`，需要 embedding 端点）。

## 3. memory_write

```
memory_write(title: str, content: str, force: bool = False,
             force_confirm: bool = False) -> str
```

新建笔记。`title` 可含目录前缀（`"projects/foo"` → `projects/foo.md`），目录只是归档，**笔记的标题是去掉目录后的主题名**。文件名对非法字符（`\ / : * ? " < > |`）做替换清洗。

**近重名守卫**：归一化（小写、去标点空白、剥离日期串/`-2`/`(新)`/`更新`/`v3` 等后缀）后与所有既有标题做模糊比对，相似度 ≥ `title_similarity_threshold`（默认 0.85）即拒绝：

```
已存在近似标题笔记，拒绝新建：
  - [[yacmemo部署配置]] (projects/yacmemo部署配置.md, 相似度 0.93)
更新内容请用 memory_edit / memory_edit_section；确属新主题请 memory_write(force=true)。
```

**force 两级确认**（24 小时滚动窗口内 forced 事件 ≥ `force_confirm_threshold`，默认 3）：

- 裸 `force=true` 被拒，返回"需要人工确认"并列出候选笔记；
- 确认确属新主题后，`force=true, force_confirm=true` 放行；
- 全程记入 `guard_events`——refused / forced 次数即违约率指标。

**journal 豁免**：`journal/` 目录下的写入不做重名拦截（时间线流水天然按日期命名）。

## 4. memory_edit

```
memory_edit(path: str, old_string: str, new_string: str) -> str
```

唯一文本锚点替换（与 Claude Code 的 Edit 语义一致）：

- `old_string` 未找到 → 拒绝，提示先 `memory_read`；
- 命中多处 → 拒绝并列出近似行号，要求扩展锚点上下文；
- 恰好一处 → 替换、写盘、全量重索引该笔记（FTS/向量/冲突重算）。

**这是更新事实的正确方式**——事实变更永远就地编辑，不新建笔记。

## 5. memory_edit_section

```
memory_edit_section(path: str, heading: str, new_content: str) -> str
```

按 `##` 及更深层标题替换整个小节：保留标题行，替换体到下一个同级/更高级标题或文末。

- 标题不存在 → 拒绝并列出**现有全部小节名**；
- 同名标题多处命中 → 拒绝并列出行号；
- 一级标题（`# 笔记标题`）不可用——那是笔记本身，请用 `memory_edit`。

适合重写一整段（如"## 部署步骤"全换），比多次 `memory_edit` 高效。

## 6. memory_move

```
memory_move(path: str, new_path: str) -> str
```

移动文件到新相对路径（自动补 `.md`）。notes/FTS/向量全部随路径更新（embedding 走 vec_cache，零 API 调用）。`[[链接]]` 按标题解析，移动不改标题，因此**不需要改写链接**。目标已存在则拒绝。

## 7. memory_audit

```
memory_audit() -> str
```

全量一致性审计，兼**自愈**：

1. **外部修改自愈**：磁盘 hash ≠ `notes.content_hash` 的笔记自动重建索引（外部编辑在 Obsidian/vim 里做的也能对齐）；
2. **外部删除清理**：文件已消失的笔记清理全部索引并列入报告；
3. D1 标题重复全量两两扫描；
4. open 状态的 D2 语义撞车清单（含双方文本与分数）；
5. D3 悬空 `[[链接]]`；
6. 守卫统计（refused / forced 次数）。

修复建议都内联在输出里。发现即展示，**系统不做任何自动删除或失效**。自愈涉及的外部改动统一以 `external: self-healed N note(s)` 快照入库，保持 git-clean 不变式（输出末尾附 git 快照状态行）。

## 8. memory_list

```
memory_list(path: str = "", sort: str = "name") -> str
```

列出 memory_root（或子目录）下全部 `.md`，`sort="mtime"` 时最近变更优先。`.index/` 永不列出。

## Agent 选择工具的决策树

```
要记一个新主题？        → memory_search 查重 → memory_write（被拒就转 edit）
要更新已有事实？        → memory_edit（锚点唯一）/ memory_edit_section（整段重写）
要找"某件事记在哪"？    → memory_search（关键词式 query）
要梳理一个主题全貌？    → memory_read（看相关笔记链路）
定期体检？              → memory_audit
```


## 9. topic_list

```
topic_list() -> str
```

列出当前注册的全部长期记忆主题（标题、一句话现状、主题卡路径）。免注册区（journal/、archive/、curator/）单列说明。

## 10. topic_register

```
topic_register(title: str, description: str = "", related: str = "") -> str
```

注册新的长期记忆主题：追加到 `TOPICS.md` 注册表，并创建主题卡（`topics/<主题>/主题卡.md`，或用既有笔记充当卡）。

- **调用门槛**：仅在用户明确要求时调用（"把 X 加入长期记忆"）——这条写进约定块，注册行为本身即用户授权的凭证；
- 重复主题名拒绝（提示直接编辑既有主题卡）；
- 注册后主题卡与注册表立即入索引。

## 11. topic_unregister

```
topic_unregister(title: str) -> str
```

注销长期记忆主题：把该主题块从 `TOPICS.md` 移除（其余内容逐字保留）。

- **调用门槛与注册相同**：仅在用户明确要求时调用（"X 不用长期记录了"）；
- **只动注册表，笔记文件一律不动**——注销后相关笔记成为游离文件（D4 点名），工具返回消息会引导 agent 与用户确认后归位 `archive/` 或删除；
- 未知主题名拒绝并列出现有主题。

## 12. memory_context

```
memory_context() -> str
```

**每次会话开始先调用**。返回核心记忆上下文 = `TOPICS.md` 注册表全文 + 各主题卡摘要头（前 12 行）。解决冷启动失忆：agent 不必"想到去搜什么"，主题体系直接在场。

## 13. memory_delete

```
memory_delete(path: str) -> str
```

删除笔记（移除文件 + 全部索引行 + 相关 collisions/向量），删除动作自动产生 `delete:` git 快照，历史可恢复。

- **调用门槛**：仅在用户明确要求时调用（"删掉 X"/"X 不用记了"）——与 topic_register 同级的人工确认语义；
- 未知路径/标题拒绝；
- 系统（store）自身永远不会主动删除——这是 v1"自动失效错杀"教训的边界：删除永远是被指令的。

## Agent 决策树（更新）

```
会话开始              → memory_context（回顾主题体系）
用户要新增长期记忆主题 → topic_register（仅用户明示时）→ memory_write
用户不再长期记录某主题 → topic_unregister（仅用户明示）→ 引导归位/清理
用户要删某条记忆      → memory_delete（仅用户明示；git 可恢复）
要记一个新主题？      → memory_search 查重 → memory_write（被拒转 edit）
要更新已有事实？      → memory_edit / memory_edit_section
要找"某件事记在哪"？  → memory_search（关键词式 query）
要梳理一个主题全貌？  → memory_read / 主题卡
定期体检？            → memory_audit（+ WebUI 审计页）
不知道有哪些主题？    → topic_list
```