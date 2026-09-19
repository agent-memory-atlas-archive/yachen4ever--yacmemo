> English | [简体中文](../03-storage-and-search.md)

# Storage and Search

> Code locations: `store.py` (CRUD/guards/indexing), `index_db.py` (SQLite), `vector.py` (LanceDB), `search.py` (hybrid retrieval), `embedding.py` (embedding client).

## 1. Roles of the Four Stores

| Store | Question it answers | Nature |
|---|---|---|
| markdown files | What happened | **source of truth**, human-readable, Obsidian-friendly |
| git repository | When it happened and what changed | history layer; every change is committed automatically (see [01-architecture.md](01-architecture.md) §4.4) |
| SQLite | What we know (metadata / full text / collisions / guard events / vector cache) | derived index, rebuildable |
| LanceDB | What is semantically related to what | derived index, rebuildable |

Data flows one way: files → indexes. The write order is **files first, then indexes**: the worst a crash can do is leave the index lagging (repaired by `memory_audit` self-healing or `reindex`); files are never corrupted.

## 2. File Format

> The topic registry `TOPICS.md` and the profile & preferences `PROFILE.md` sit at the memory_root root (topic registry: [01-architecture.md](01-architecture.md) §13; PROFILE is a memory-layer feature file — not registered, and excluded from stray detection); `journal/`, `archive/`, and `curator/` are registry-free zones — excluded from topic stray detection.

- One note per topic; filename = title (Chinese allowed, illegal characters sanitized);
- The first-line `# 标题` (# Title) heading is the source of the title; no hard frontmatter requirement;
- The body is free-form. Two **optional** syntaxes enhance retrieval and consistency:
  - observation fact lines: `- [类别] 事实 #标签` (`- [category] fact #tag`; D2 collision detection and read's relatedness summaries depend on them); GFM task-list items `- [x]` / `- [ ]` are checkboxes and are **not** treated as observations (so checklist-style notes do not create collision noise);
  - Links: `[[另一篇笔记标题]]` (`[[another note's title]]`; read's 1-hop relatedness and D3 dangling-link detection depend on them);
- The `journal/` directory: a timeline stream, exempt from the duplicate-title interception.

## 3. Index Structure (`<memory_root>/.index/`, not in git)

### 3.1 SQLite (index.db)

```sql
notes(path PK, title, content_hash, updated_at)   -- 一行一篇笔记
fts(FTS5: title, body, path UNINDEXED, tokenize='trigram')
collisions(id, kind, a_path, b_path, a_text, b_text, score, detected_at, status)
guard_events(id, ts, kind, attempted_title, matched_path)   -- refused / forced
vec_cache(content_hash PK, vector BLOB)                     -- float32 向量缓存
```

- `notes.content_hash`: the basis for incremental updates and the comparison baseline for audit self-healing;
- `fts` is a standalone table (not external content) — trading size for simplicity, since a full rebuild is cheap;
- `guard_events` is **preserved** across `clear_all` and reindex (the violation rate is a cumulative metric);
- `vec_cache` is addressed by content hash: unchanged text costs zero API calls — the key to "index-on-write < 300ms".

### 3.2 LanceDB (lancedb/)

| Table | id | text | Trigger |
|---|---|---|---|
| `note_vectors` | relative path | title (vector computed from `title\n正文` — title, newline, body) | write/edit |
| `obs_vectors` | `sha256(path\0obs_text)[:32]` | observation line text | write/edit (line by line) |

Dimension 1024 (Qwen3-Embedding-0.6B). upsert = delete by id first, then insert (LanceDB has no native upsert).

### 3.3 Synchronous Indexing Flow (`_index_note`)

```
写文件 → content_hash → notes upsert → fts_replace
→ [有 embedding 端点时]
   remove_collisions_involving(path) + delete_by_path(path)   -- 清旧
   note 向量 upsert；逐条 obs：vec_cache → upsert_obs_vector
   D2：每条新 obs 向量搜 obs_vectors top-5 → cosine ≥ 阈值 → add_collision
```

## 4. Retrieval (search.py)

### 4.1 FTS Channel (trigram semantics)

- trigram tokenization slices text into sliding 3-character windows, so **phrase queries = literal substring matching**;
- Query construction: split on whitespace into tokens, drop tokens shorter than 3 characters, wrap the rest in quotes and AND them;
- Ordered by `bm25(fts)` ascending (smaller = more relevant);
- Corollary: **2-character short words ("端口", "port") never hit in the FTS channel** — the vector channel backstops them; there is also a LIKE substring fallback as a second backstop (added 2026-09-18, so short words still hit while the vector channel is down); a whole natural-language sentence is not a substring of any document — that also relies on vectors.

### 4.2 Vector Channel

The query text is embedded, then searched against `note_vectors` for the top-limit, returning path/title/rank. When the embedding endpoint is unavailable, this degrades to empty (hybrid degrades to FTS-only), and a degradation notice is emitted via `searcher.last_notice` into both MCP results and the WebUI search page — no longer silent (the silent degradation exposed by the real 2026-09-18 endpoint outage).

### 4.3 RRF Fusion

```
score(d) = Σ_channels 1 / (rrf_k + rank_channel(d)),   rrf_k = 60
```

Only ranks are used, never scores → no need to align scores across the two channels. Notes hit by both channels rank first. The `channels` field records the sources (displayed as `fts+vector`).

### 4.4 Measured Baseline (12-note Chinese corpus, Recall@3)

| fts | vector | hybrid |
|---|---|---|
| 6/10 | 9/10 | 9/10 |

The 4 fts misses are all natural-language sentences ("服务端口是多少" — "what is the service port" — and the like); the vector channel recovered all of them. See `06-evaluation.md` for details.

## 5. Self-Healing and Rebuild

| Scenario | Mechanism | Trigger |
|---|---|---|
| External edits (Obsidian/vim) | audit compares disk hash ≠ content_hash → rebuilds that note's index | `memory_audit` |
| External deletions | audit cleans up index rows + lists them in the `missing` report | `memory_audit` |
| External changes (edit/delete/create) | after index self-healing, consolidated as a single `external:` git snapshot (records history, does not modify files) | `memory_audit` |
| Vector index corrupted/lost | `reindex` (internal interface) or delete `.index/` for a full rebuild | manual |
| Writes during an embedding-endpoint outage | content + FTS proceed normally; `notes.vector_ok=0` marks the missing vectors; audit names them and retries the embeddings (converges once the endpoint recovers) | `memory_audit` |
| git unavailable | snapshots are skipped without blocking writes; the audit's `== git ==` line shows the latest failure reason | automatic |

Self-healing only patches indexes and **never modifies markdown files** — files are the single source of truth, and the system's only write path to them is the user/agent's explicit write tool call.

## 6. Threading Model

Under the HTTP transport, sync tools execute concurrently in a thread pool:

- `IndexDB`: a shared connection (`check_same_thread=False`) + an instance RLock serializing all public methods (reentrant, so calls between methods are safe);
- `VectorStore`: an instance Lock;
- `Store`: composite operations such as write/read/audit/rebuild serialize under a Store-level RLock, with the primitive-level locks backstopping interleaving between composites.
