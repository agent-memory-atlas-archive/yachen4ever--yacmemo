# yacmemo 精简记忆层设计（v2 终形态）

> 2026-09-13 设计定稿；2026-09-14 更新（HTTP 多机部署模型，P0–P2 已实现）
> v1 三层架构的设计文档在 `legacy/docs/`，保留作为决策记录。本目录其余文档为 v2 现行技术文档：02 工具规格、03 存储与检索、04 一致性、05 部署、06 评测、07 用户使用手册。

---

## 一、决策过程：为什么走到这里

### 1.1 起点：OpenViking 的两个痛点

- **提取阻塞**：OV 用 VLM 对整个 session 历史做提取，每条 90 秒同步阻塞，写入被卡。
- **资源争抢**：VLM 与主模型（Qwen3.8 Flash Next）在同一台 M2 Ultra 上抢显存和带宽。

### 1.2 第一版 yacmemo：判断对了，实现做重了

第一版三层架构（Agent 写 .md → 独立小模型拆分提取 → 独立小模型一致性校验）有两项正确的核心判断：

1. **Agent 写结论，不存原文**——提炼发生在对话内，独立 LLM 不该重做提取。
2. **一致性需要专门对待**——跨 session 失忆的 Agent 不会回头修正旧事实。

但实现上有三个错误：

- 把"维护者"角色交给了 1.3B 激活参数的小模型，且让它**静默自动失效**记忆——错杀的危害远大于漏报；
- Layer 2 的拆分文件机制催生了整套文件保护子系统（hash 校验/冲突备份/完整性扫描），复杂度花在了保护 LLM 生成的文件上；
- 文档说"提取后逐节点实时校验"，实际实现是"每次写入触发全量扫描"，O(全部记忆)/次。

### 1.3 Basic Memory 评估与放弃

BM（basicmachines-co/basic-memory，AGPL-3.0，~3.9k star）与本项目理念同源：markdown 为 source of truth、SQLite 为派生索引、索引路径零 LLM。它的关键启发是：**笔记有稳定地址，事实更新 = edit_note 就地改写**，从数据模型上消灭"同一事实多版本并存"。

放弃自建、采用 BM + 外挂的组合，最后也放弃了。原因：

- 中文向量（FastEmbed 默认模型）与中文 FTS 质量存疑，外挂承担了检索主力后，BM 残余价值（写路径工具 + 图谱）缩水——**外挂方案自己把自己掏空了**；
- 锁版本 = 冻结已知 bug、文档与社区漂移、0.x 升级破坏性变更的达摩克利斯之剑；
- BM 的 `write_note` 无法被拦截，**一致性只能做到"检测 + 标注"，永远到不了"API 级强制"**；
- fork 扩展的合并税高于自拥有 ~1k 行代码。

### 1.4 结论：自建轻量记忆层

自建不是回到第一版，而是只保留验证过有价值的部分，并且获得两个 BM+外挂做不到的能力：

1. **统一的中文一等公民检索**：FTS5 trigram + Qwen3-Embedding 单一混合搜索，不再有两套质量不一致的检索工具；
2. **API 级一致性强制**：`memory_write` 拒绝近重名、`memory_edit` 强制锚点唯一——约定是概率的，API 拒绝是确定的。

---

## 二、核心原则

### 原则 1：markdown 文件是 source of truth

SQLite（FTS/元数据/冲突记录）与 LanceDB（向量）全部是派生索引，删掉可从文件全量重建。人可直接阅读编辑、可 git、可 Obsidian。

### 原则 2：记忆子系统里没有生成式 LLM

全链路唯一的模型调用是 embedding（Qwen3-Embedding-0.6B，单次 ~50ms，<1GB 常驻）。提取、拆分、裁决、摘要等一切生成式工作要么不发生（结构靠约定），要么由主模型在对话内完成（裁决发生在读取时刻）。这直接消灭 OV 痛点 2。

### 原则 3：结构靠约定产生，一致性靠 API 强制

不提取结构，约定产生结构（一篇一主题、observation/`[[链接]]` 语法鼓励不强制）。一致性防线按强度递增分三层：

```
第 1 层  API 强制（确定）    write 拒绝近重名；edit 强制锚点唯一
第 2 层  确定性检测（确定）  D1 标题重复 / D2 observation 撞车 / D3 悬空链接
第 3 层  主模型裁决（读取时）检索结果内联 ⚠ 标注，agent 顺手 edit_note 合并
```

### 原则 4：失效语义优于检测语义

系统**从不删除、从不隐藏**任何记忆。冲突的两条事实都可见、都返回，只加标注。错杀在架构上不可能发生，git 承载真实历史。

### 原则 5：无常驻服务、无 cron、无队列

唯一进程形态是 MCP server（stdio），按 agent 会话启停。索引在写入时同步维护（毫秒级），没有后台扫描、没有 webhook、没有 APScheduler。这直接消灭第一版"每次写入触发全量扫描"的回归。

---

## 三、架构总览

```
你的电脑们（任意 agent：Claude Code / Codex / Cursor / 自研 runtime…）
  │  各端只添加一个远程 MCP URL，客户端零安装、零进程
  │      http://debsvc.local:9721/yachen/mcp
  │      http://debsvc.local:9721/user2/mcp
  ▼
yacmemo-server（debsvc，单进程，streamable HTTP，无状态会话）
  ├── /yachen/mcp → Store(root=/srv/yacmemo/yachen/memory)
  ├── /user2/mcp   → Store(root=/srv/yacmemo/user2/memory)
  │     ├── store.py      markdown CRUD + 写路径守卫 + 同步索引
  │     ├── search.py     FTS5(trigram) + 向量 RRF 融合
  │     ├── detectors.py  D1/D3 确定性检测器
  │     ├── index_db.py   SQLite: 元数据/FTS/冲突记录/守卫事件/向量缓存
  │     ├── vector.py     LanceDB: note_vectors + obs_vectors
  │     └── embedding.py  omlx /v1/embeddings（唯一的模型调用）
  └── GET /health
  │
  ▼
磁盘 (source of truth，单点存放)
  /srv/yacmemo/yachen/memory/  ← git 仓库
  /srv/yacmemo/user2/memory/    ← git 仓库
  各 memory/.index/            ← 可随时删除重建，不进 git
```

数据模型从第一版的 nodes/edges/events 三表塌缩为 **notes + observations** 两级：笔记是主体，observation（若 agent 使用语法）是笔记内的事实行，用于更细粒度的检索与撞车检测。没有实体表、没有边表、没有事件表。

两个用户的内存完全独立：各自的 Store 在构造时绑定各自 root（边界固定，不存在懒解析导致的串目录）；HTTP 传输用无状态会话，任意 MCP 客户端无需会话亲和。8 个工具在 `yacmemo/tools.py` 注册一次，stdio（`yacmemo-mcp`）与 HTTP（`yacmemo-server`）两个入口共享同一工具面。

---

## 四、存储设计

### 4.1 目录约定

```
memory_root/
├── journal/          时间线流水（按日期命名），不参与重名拦截与合并
├── projects/         事项类笔记
├── infra/            基础设施类笔记
├── people/
└── preferences/
```

目录划分只服务人类浏览，检索不依赖目录。

### 4.2 笔记格式

- 一篇一主题，文件名 = 标题（允许中文），首行 `# 标题`；
- 正文自由格式，**无 frontmatter 硬要求**；
- 事实行鼓励使用 observation 语法：`- [类别] 事实内容 #标签`；
- 关联鼓励使用 `[[wiki-link]]`；
- 不使用语法的笔记功能完整降级：检索走 note 级向量 + FTS，D2 检测粒度变粗但可用。

### 4.3 索引结构（全部可重建）

```sql
-- index_db (SQLite)
notes(path TEXT PRIMARY KEY, title TEXT, content_hash TEXT, updated_at TEXT)
fts   (FTS5: title, body, tokenize='trigram')      -- 内容派生自 notes
collisions(id TEXT PRIMARY KEY, kind TEXT,          -- open / resolved / dismissed
           a_path TEXT, b_path TEXT, a_text TEXT, b_text TEXT,
           score REAL, detected_at TEXT, status TEXT)
guard_events(id TEXT PRIMARY KEY, ts TEXT, kind TEXT,        -- refused / forced
             attempted_title TEXT, matched_path TEXT)
vec_cache(content_hash TEXT PRIMARY KEY, vector BLOB)
```

`guard_events` 是设计时 4 张表之外的补充：force 越过守卫必须可数（P4 违约率指标的直接来源），refused 事件用于调标题相似度阈值。

LanceDB 两张向量表（沿用现有 `vector.py`，三表改两表，维度 1024 不变）：

| 表 | 内容 | 写入时机 |
|---|---|---|
| `note_vectors` | 整篇笔记向量（title + 正文） | `memory_write` / `memory_edit` 同步 |
| `obs_vectors` | 逐条 observation 向量 | 同上（一篇通常 0–10 行，~50ms/行） |

写入顺序：**先写文件，再更新索引**。进程崩溃最坏情况是索引滞后（下次写入或 `reindex` 修复），文件永不损坏。重建命令：删除 `.index/` 后调 `reindex` 工具。

---

## 五、检索设计

### 5.1 混合检索

`memory_search` 默认双路召回、RRF 融合：

```
score(d) = Σ_channels 1 / (rrf_k + rank_channel(d))    # rrf_k = 60
通道 A：FTS5 trigram 全文检索（BM25 排序）
通道 B：note_vectors 余弦近邻（Qwen3-Embedding）
```

RRF 只用名次不用分数，避免两路分数量纲对齐问题。`kind` 参数可强制单通道（`fts` / `vector`），用于 P4 阶段的对比评测。

### 5.2 中文细节（第一验证点）

- trigram 分词器要求 SQLite ≥ 3.34，按 ≥3 字子串命中；**2 字短查询在 FTS 通道会落空**，由向量通道兜住（短查询恰是向量强项）；
- 若 P0 实测 trigram 对真实中文查询召回不足，升级路径：jieba 分词后写入 FTS 影子列（`fts_seg`），双列同时检索；
- 大小写不敏感按 trigram 默认配置，中文无影响，英文标题受益。

### 5.3 撞车标注内联

`memory_search` 结果中，凡命中笔记涉及 `collisions` 表中 status=open 的记录，在结果行内追加：

```
1. yacmemo部署配置 (score 0.91)
   "服务端口为 9721，LLM 指向 m2ultra:11234"
   ⚠ 与 [[yacmemo部署记录0910]] 疑似重复 — 建议读两篇后用 memory_edit 合并
```

**读取的时刻就是修复的时刻**：agent 合并后冲突对自然消失（两侧内容 hash 变化，stale 记录被清除）。

### 5.4 1-hop 关联

`memory_read` 返回正文之外，附加"相关笔记"：正文中的 `[[链接]]` 目标（标题 + 首条 observation）+ 该笔记向量的 top-2 近邻。单跳遍历 ~50 行代码实现，覆盖 90% 的图谱需求；多跳遍历若有朝一日成为真实需求，再评估接入现成产品（届时才是图数据库类工具的正确入场时机）。

---

## 六、MCP 工具面（8 个）

| 工具 | 签名 | 关键行为 |
|---|---|---|
| `memory_search` | `query, limit=10, kind="hybrid"\|"fts"\|"vector"` | 双路 RRF 融合 + 撞车标注内联 |
| `memory_read` | `path_or_title` | 正文 + 1-hop 相关笔记 |
| `memory_write` | `title, content, force=false, force_confirm=false` | **近重名拦截**（见 6.1，含两级 force 确认）；写入即同步索引 |
| `memory_edit` | `path, old_string, new_string` | **锚点唯一性强制**：找不到/命中多处 → 拒绝并列出候选位置 |
| `memory_edit_section` | `path, heading, new_content` | 按 `##` 标题段替换（P2 实现） |
| `memory_move` | `path, new_path` | 移动 + 全库索引随路径更新（[[链接]] 按标题解析，移动不改标题故无需改写链接） |
| `memory_audit` | — | 自愈（外部改动/删除的 hash 级重算与清理）+ D1 全量扫描 + collisions 报告 + D3 悬空链接 |
| `memory_list` | `path="", sort="name"\|"mtime"` | 目录树 / 最近变更 |

### 6.1 写路径守卫（本设计的一致性核心）

```
memory_write(title, content):
    normalized = normalize(title)          # 小写、去标点空白、
                                           # 剥离日期串、"-2"/"(新)"/"更新" 等后缀
    for existing in all_titles:
        if fuzzy_ratio(normalized, normalize(existing)) >= 0.85:
            return 拒绝:
              "已存在近似标题笔记 [[{existing}]]。
               更新内容请用 memory_edit / memory_edit_section。
               确属新主题请 force=true。"

    write file → embed note+obs → 更新 fts/vectors/collisions
    return "已写入并索引"
```

- 拒绝是**确定性行为**，不依赖模型自觉；`force=true` 是模型显式越过守卫的唯一通道；
- **force 两级确认**：24 小时内 forced 事件达到 `force_confirm_threshold`（默认 3）后，光 `force=true` 会被拒绝，必须同时传 `force_confirm=true`（显式人工确认语义）；拒绝信息列出候选已有笔记，全过程可数；
- **force 调用次数就是违约率的可数指标**（P4 核心度量），audit 汇总报告；
- journal/ 目录不参与拦截；
- 阈值 `title_similarity_threshold`（默认 0.85）可配，拒绝事件全量落日志用于调阈值。

### 6.2 索引同步性

所有写工具（write/edit/edit_section/move）在返回成功前同步完成：文件写入 → hash → embedding（仅变更部分）→ FTS/向量/冲突表更新。单次调用总开销 < 300ms（典型笔记）。无懒索引、无后台补偿。

---

## 七、一致性机制细则

### 7.1 D1：标题/主题重复（写时拦截 + 审计兜底）

- 写时：6.1 的守卫拦截；
- 审计兜底：对存量笔记（守卫上线前写入的、或 force 越过的）做全量归一化标题两两比对；
- 同时比对共享 tag（≥2 个共同 tag 且标题相似度 ≥ 0.7 也列为候选）。

### 7.2 D2：observation 语义撞车（写时增量检测）

```
memory_write / memory_edit 完成 embedding 后：
    for obs_vector in 新写入的 obs_vectors:
        top-k = obs_vectors.search(obs_vector, k=5)     # 排除同 path
        for hit in top-k where cosine >= 0.86:
            insert collisions(kind="obs", a, b, score, status="open")
```

- 阈值可配（`collision_cosine_threshold`，默认 0.86）；
- **刻意不判断是否矛盾、不裁决谁有效**——只标记"疑似在说同一件事"；
- **stale 清理与自愈**：`memory_audit` 比对磁盘文件 hash 与 `notes.content_hash`——外部修改的笔记自动重建索引（embedding 走 vec_cache，未变行零调用），其涉及 collisions 随之重算；外部删除的笔记清理全部索引并列入 `missing` 报告。审计即自愈，无后台进程；
- 局限（接受）：措辞距离远但逻辑矛盾的不会命中——残余风险由第 3 层（主模型在检索到可疑对时判断）覆盖，不做基建。

### 7.3 D3：悬空引用

`[[链接]]` 指向不存在笔记的清单，audit 输出。`memory_move` 的链接改写能预防大部分，剩余靠 agent 顺手修复。

### 7.4 违约率指标（P4 度量）

| 指标 | 来源 | 含义 |
|---|---|---|
| force 使用率 | 写工具日志 | 模型越过守卫的频率 = 约定失效频率 |
| D1/D2 命中数与误报数 | collisions 表 | 检测器质量，用于调阈值 |
| 检索命中率 | 20 条真实查询的个人评测集 | top-3 内含目标笔记的比例，分通道对比 |
| 合并动作数 | audit 前后 collisions status 变化 | 修复闭环是否真的在转 |

---

## 八、Agent 使用约定（贴入任意 agent 的系统提示）

```text
# 记忆使用约定（yacmemo）
写入前：
1. 先查后写。写任何记忆前，先用 memory_search 查是否已有同主题笔记。
2. 已有同主题笔记 → memory_edit / memory_edit_section 增量修改，绝不新建重复笔记。
3. 新建时标题 = 主题名（如"yacmemo部署配置"），禁止日期后缀和"-2"/"新"等尾巴
   （时间线流水放 journal/ 目录）。
写入时：
4. 写提炼后的结论，不贴对话原文；一篇笔记一个主题。
5. 事实行用 observation 语法：- [配置] 服务端口为 9721
6. 与其他笔记相关时写关系：- 部署于 [[debsvc服务器]]
检索时：
7. memory_search 结果带 ⚠ 标注时，先读两篇，用 memory_edit 合并，然后才回答用户。
8. 探索一个主题用 memory_read 的相关笔记链路，不要只凭单条搜索结果下结论。
```

约定仍会写进提示（第 1、2、4 条减少无效往返），但系统不再**依赖**模型守约——守卫与检测器兜底，这正是本设计与第一版的本质区别。

---

## 九、部署与多用户

**一个服务，所有机器，所有 agent**。yacmemo-server 跑在数据所在的 debsvc 上，以 streamable HTTP 暴露 MCP；任何电脑上的任何 MCP 客户端只需添加 URL（`http://debsvc.local:9721/{user}/mcp`），客户端零安装、零进程。用户隔离 = URL 路径 = 磁盘目录，构造时固定边界。部署细节（systemd、各客户端配置示例、备份）见 `05-deployment.md`。

- git：每个 memory_root 一个仓库，`.index/` 入 `.gitignore`；事实历史由 git 承载；
- 可选：Syncthing 同步 memory 目录到 Mac 用 Obsidian 浏览；
- 依赖：`mcp`（FastMCP，锁 `<2`，2.x 的 MCPServer 迁移列为评估项）、`lancedb`、`pyarrow`、`numpy`、`rapidfuzz`、`httpx`、可选 `jieba`。服务端单进程；客户端零依赖。

配置示例：

```toml
[memory]
root = "/srv/yacmemo/yachen/memory"
journal_dir = "journal"

[embedding]
base_url = "http://m2ultra:11235/v1"
model = "Qwen3-Embedding-0.6B"
dimensions = 1024

[search]
rrf_k = 60
fts_seg_fallback = false        # jieba 影子列开关

[guard]
title_similarity_threshold = 0.85
collision_cosine_threshold = 0.86
obs_topk = 5
```

---

## 十、现有代码资产去向

| 现有文件 | 去向 |
|---|---|
| `embedding.py` | 原样复用 |
| `vector.py` | 复用改造：node/event/edge 三表 → note/obs 两表 |
| `fs_utils.py` | 复用 `content_hash`/`file_hash`；CRUD 重写为 `store.py`（含守卫） |
| `consistency.py` | 前半段（embed→向量搜索→距离过滤）改写为 `detectors.py` 的 D2；LLM 裁决与自动失效退役 |
| `mcp_server.py` | 骨架模板（FastMCP 结构、启动参数） |
| `db.py` | 退役；`index_db.py` 重写（4 张表） |
| `extractor.py`、`enhancer.py`、`webui/`、`config.py` 大部 | 退役，从运行路径移除（代码可留档） |
| `docs/01–05` | 保留为决策记录 |

净新代码估计 600–800 行（含测试），复用约 400 行。

---

## 十一、分阶段落地

> **实现状态（2026-09-14）**：P0–P2 已实现并提交（51 个测试通过；三通道基线 fts 6/10 → hybrid 9/10；HTTP 多用户回环测试通过）。P3（部署 + 约定块进 agent 系统提示）与 P4（两周实测）待执行。

| 阶段 | 内容 | 工作量 | 验收标准 |
|---|---|---|---|
| P0 | 仓库转型（旧模块移出运行路径）、骨架、**trigram 中文实测** | 0.5 天 | 用 10 条真实中文查询记录 fts/vector/hybrid 三通道召回基线 |
| P1 | `search/read/write/edit` + 同步索引 | 1–2 天 | 写入 < 300ms；评测集上 hybrid ≥ 单通道最优 |
| P2 | 守卫完善（force 两级确认）、`edit_section/move`、D1–D3、`audit` 自愈、碰撞标注 | 1 天 | 人造 5 组重复/矛盾样本全被拦截或标出，误报 ≤ 2 |
| P3 | 部署 yacmemo-server + 约定块进各 agent 系统提示 | 0.5 天 | 多机多客户端隔离运行一天无串数据 |
| P4 | 两周实测 | — | 7.4 全部指标产出；据数据调阈值 |

每阶段可独立回滚：P1 完成后系统已可用（无守卫），守卫是纯增量。

---

## 十二、风险与开放问题

| # | 风险 | 缓解 |
|---|---|---|
| 1 | trigram 对 2 字中文短查询落空 | 向量通道兜底；P0 实测后决定是否启用 jieba 影子列 |
| 2 | 模型不守约定、高频 force | force 是显式显眼动作，次数可数；超阈值则收紧：force 需人工确认（MCP 返回待确认标记） |
| 3 | observation 语法采纳率低 | D2 自动降级到 note 级向量比对（粒度粗但可用）；P4 统计采纳率 |
| 4 | 标题归一化误拦（确属不同的两个主题相似命名） | force 通道 + 拒绝事件日志驱动调阈值 |
| 5 | `[[链接]]` 改写只支持精确标题 | 不支持别名是已知简化，未改写引用在 audit 中报告 |
| 6 | D2 漏掉措辞距离远的逻辑矛盾 | 接受的残余风险，主模型读取时裁决；不做全量 LLM 扫描（第一版教训） |
| 7 | 双索引（FTS/LanceDB）与文件的一致性 | 先文件后索引的写序 + 索引可全量重建，最坏降级不损坏 |

---

## 十三、被否决的备选方案（决策记录）

| 方案 | 否决原因 |
|---|---|
| 继续用 OpenViking | 提取阻塞 90s/条；VLM 与主模型抢资源 |
| 第一版 yacmemo（三层） | 小模型自动失效静默错杀；拆分文件保护子系统复杂度倒挂；每次写入触发全量扫描 |
| Mem0 / Letta / Graphiti / Cognee / MemOS | 提取路径需要生成式 LLM / 图数据库 / 常驻服务栈，违反核心原则 2、5；详见 2026-09 调研记录 |
| Basic Memory 直接采用 | 锁版本风险；中文向量/FTS 弱；write_note 不可拦截，一致性到不了 API 强制层 |
| BM + yacmemo 外挂 | 外挂承担检索主力后 BM 残余价值缩水；双检索工具质量不一致；写路径仍不可守卫 |
| fork BM 扩展 | 数万行 0.x 代码库的合并税 > 自拥有 1k 行 |
