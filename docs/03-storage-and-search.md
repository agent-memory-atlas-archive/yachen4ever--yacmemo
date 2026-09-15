---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'c9ef7a77-45cb-4c2f-b0cd-d0e5e391eca2'
  PropagateID: 'c9ef7a77-45cb-4c2f-b0cd-d0e5e391eca2'
  ReservedCode1: 'd6afc94a-8cc1-4ba7-ba40-8944812fcbf9'
  ReservedCode2: 'd6afc94a-8cc1-4ba7-ba40-8944812fcbf9'
---

# 存储与检索

> 代码位置：`store.py`（CRUD/守卫/索引）、`index_db.py`（SQLite）、`vector.py`（LanceDB）、`search.py`（融合检索）、`embedding.py`（embedding 客户端）。

## 一、四种存储的角色

| 存储 | 回答的问题 | 性质 |
|---|---|---|
| markdown 文件 | 发生了什么 | **source of truth**，人可读可 Obsidian |
| git 仓库 | 什么时候发生的、改了什么 | 历史层，每次变更自动 commit（见 [01-architecture.md](01-architecture.md) §4.4） |
| SQLite | 我们知道什么（元数据/全文/冲突/守卫事件/向量缓存） | 派生索引，可重建 |
| LanceDB | 什么和什么语义相关 | 派生索引，可重建 |

数据流单向：文件 → 索引。写入顺序**先文件后索引**：崩溃最坏情况是索引滞后（`memory_audit` 自愈或 `reindex` 修复），文件永不损坏。

## 二、文件格式

> 主题注册表 `TOPICS.md` 与画像/偏好 `PROFILE.md` 位于 memory_root 根部（主题注册制见 [01-architecture.md](01-architecture.md) §十三；PROFILE 为记忆层功能文件，不注册、不参与游离检测）；`journal/`、`archive/`、`curator/` 为免注册区——不参与主题游离检测。

- 一篇一主题；文件名 = 标题（允许中文，非法字符清洗）；
- 首行 `# 标题` 作为标题来源；无 frontmatter 硬要求；
- 正文自由格式。两个**可选**语法增强检索与一致性：
  - observation 事实行：`- [类别] 事实 #标签`（D2 撞车检测、read 相关性摘要依赖它）；GFM 任务清单 `- [x]` / `- [ ]` 是勾选框，**不作为 observation**（避免清单类笔记制造撞车噪音）；
  - 关联：`[[另一篇笔记标题]]`（read 的 1-hop 相关、D3 悬空检测依赖它）；
- `journal/` 目录：时间线流水，豁免重名拦截。

## 三、索引结构（`<memory_root>/.index/`，不进 git）

### 3.1 SQLite（index.db）

```sql
notes(path PK, title, content_hash, updated_at)   -- 一行一篇笔记
fts(FTS5: title, body, path UNINDEXED, tokenize='trigram')
collisions(id, kind, a_path, b_path, a_text, b_text, score, detected_at, status)
guard_events(id, ts, kind, attempted_title, matched_path)   -- refused / forced
vec_cache(content_hash PK, vector BLOB)                     -- float32 向量缓存
```

- `notes.content_hash`：增量依据 + audit 自愈的比对基准；
- `fts` 是独立表（非 external content），体积换取简单性——全量重建廉价；
- `guard_events` 在 `clear_all` 与 reindex 中**保留**（违约率是累计指标）；
- `vec_cache` 按内容 hash 寻址：未变文本零 API 调用，是"写入即索引 < 300ms"的关键。

### 3.2 LanceDB（lancedb/）

| 表 | id | text | 触发 |
|---|---|---|---|
| `note_vectors` | 相对路径 | 标题（向量取 `title\n正文`） | write/edit |
| `obs_vectors` | `sha256(path\0obs_text)[:32]` | observation 行文本 | write/edit（逐行） |

维度 1024（Qwen3-Embedding-0.6B）。upsert = 先按 id 删再插（LanceDB 无原生 upsert）。

### 3.3 同步索引流程（`_index_note`）

```
写文件 → content_hash → notes upsert → fts_replace
→ [有 embedding 端点时]
   remove_collisions_involving(path) + delete_by_path(path)   -- 清旧
   note 向量 upsert；逐条 obs：vec_cache → upsert_obs_vector
   D2：每条新 obs 向量搜 obs_vectors top-5 → cosine ≥ 阈值 → add_collision
```

## 四、检索（search.py）

### 4.1 FTS 通道（trigram 语义）

- trigram 分词把文本切成滑动 3 字窗口，**短语查询 = 字面子串匹配**；
- 查询构造：按空白切 token，`< 3` 字符的丢弃，其余引号包裹后 AND；
- 排序 `bm25(fts)` 升序（越小越相关）；
- 推论：**2 字短词（"端口"）在 FTS 通道永不命中**——由向量通道兜底；整句自然语言不是文档子串——同样靠向量。

### 4.2 向量通道

查询文本 embed 后在 `note_vectors` 搜 top-limit，返回 path/title/rank。embedding 端点不可用时优雅降级为空（hybrid 静默退化为 FTS-only）。

### 4.3 RRF 融合

```
score(d) = Σ_channels 1 / (rrf_k + rank_channel(d)),   rrf_k = 60
```

只用名次不用分值 → 无需两路分数对齐。双通道都命中的笔记排最前。`channels` 字段记录来源（显示为 `fts+vector`）。

### 4.4 实测基线（12 笔记中文语料，Recall@3）

| fts | vector | hybrid |
|---|---|---|
| 6/10 | 9/10 | 9/10 |

fts 落空的 4 条全是自然语句（"服务端口是多少"等），向量全部救回。详见 `06-evaluation.md`。

## 五、自愈与重建

| 场景 | 机制 | 时机 |
|---|---|---|
| 外部编辑（Obsidian/vim） | audit 比对磁盘 hash ≠ content_hash → 重建该笔记索引 | `memory_audit` |
| 外部删除 | audit 清理索引行 + 列入 `missing` 报告 | `memory_audit` |
| 外部改动（编辑/删除/新建） | 索引自愈后统一以一条 `external:` git 快照收编（记录历史，不改文件） | `memory_audit` |
| 向量索引损坏/丢失 | `reindex`（内部接口）或删除 `.index/` 全量重建 | 人工 |
| embedding 端点宕机期间写入 | 内容+FTS 正常，向量缺失自动在下一次成功写入/audit 补齐 | 自动 |
| git 不可用 | 快照跳过不阻塞写入；audit 的 `== git ==` 行显示最近失败原因 | 自动 |

自愈只补索引，**永不改动 markdown 文件**——文件是唯一真相，系统对它的唯一写路径是用户/agent 的显式写工具调用。

## 六、线程模型

HTTP 传输下 sync 工具在线程池并发执行：

- `IndexDB`：共享连接（`check_same_thread=False`）+ 实例 RLock 串行全部公开方法（可重入，方法间互调安全）；
- `VectorStore`：实例 Lock；
- `Store`：写/读/审计/重建等组合操作在 Store 级 RLock 下串行，原语级锁兜底组合间交错。