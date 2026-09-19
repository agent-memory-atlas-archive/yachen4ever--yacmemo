> English | [简体中文](../06-evaluation.md)

# Retrieval Evaluation

> Tool: `scripts/eval_search.py`. This is a **measurement**, not a gate — the exit code is always 0.

## 1. Method

A fixed corpus of 12 Chinese notes (home infrastructure / projects / workflows topics, including observation lines) and 10 real-style Chinese queries, each annotated with the expected note to hit; measures **Recall@3** per channel:

- **fts**: SQLite FTS5 trigram (literal substring matching for keyword-style queries);
- **vector**: Qwen3-Embedding nearest notes;
- **hybrid**: RRF (k=60) fusion.

The corpus and queries live at the top of the script (`CORPUS` / `QUERIES`) and can be replaced with your own real topics — **an evaluation set drawn from your own memories is the only meaningful one**.

## 2. Running

```bash
# FTS-only（任何能跑 python 的地方）
uv run python scripts/eval_search.py

# 三通道（需能访问 omlx；模型 ID 必须与服务端 /v1/models 一致）
uv run python scripts/eval_search.py \
  --embedding-url http://192.168.5.2:11235/v1 \
  --embedding-model "Qwen3-Embedding-0.6B-4bit-DWQ" \
  --embedding-key sk-xxx \
  --keep          # 保留语料目录便于人工检查
```

When the endpoint is unreachable, the vector/hybrid columns are automatically marked `-` and skipped; the FTS column runs as usual. An `audit` output is appended at the end (D1/D2/D3 on the corpus should all be 0).

## 3. Baseline Results (2026-09-14, 12-note corpus, measured on m2ultra omlx)

| query | expected | fts | vector | hybrid |
|---|---|---|---|---|
| yacmemo 端口 (yacmemo port) | yacmemo部署配置 (yacmemo deployment config) | ✓ | ✓ | ✓ |
| 服务端口是多少 (what is the service port) | yacmemo部署配置 (yacmemo deployment config) | ✗ | ✗ | ✗ |
| debsvc IP 地址 (debsvc IP address) | debsvc服务器 (debsvc server) | ✓ | ✓ | ✓ |
| 数据库服务器的 IP 是什么 (what is the database server's IP) | debsvc服务器 (debsvc server) | ✗ | ✓ | ✓ |
| restic | 备份策略 (backup policy) | ✓ | ✓ | ✓ |
| 怎么备份数据 (how to back up data) | 备份策略 (backup policy) | ✗ | ✓ | ✓ |
| VLAN 划分 (VLAN layout) | 家庭网络拓扑 (home network topology) | ✓ | ✓ | ✓ |
| M2 Ultra 内存多大 (how much memory does the M2 Ultra have) | m2ultra推理服务器 (m2ultra inference server) | ✗ | ✓ | ✓ |
| agent 记忆接入 (agent memory integration) | 个人agent方案 (personal agent setup) | ✓ | ✓ | ✓ |
| Qwen3 embedding 部署 (Qwen3 embedding deployment) | Qwen3-Embedding部署 (Qwen3-Embedding deployment) | ✓ | ✓ | ✓ |
| **Recall@3** | | **6/10** | **9/10** | **9/10** |

## 4. Interpretation

1. **All 6 FTS hits are keyword-style queries; all 4 misses are natural sentences** — trigram phrase matching is literal substring matching, and "服务端口是多少" ("what is the service port") is not a substring of "服务端口为 9721" ("the service port is 9721"). This is a mechanical consequence, not a bug.
2. **The vector channel rescued all 4 FTS misses** — direct evidence for why hybrid retrieval is necessary. Any single-channel approach (pure FTS or pure vector) fails on the other half of the query types.
3. **The only residual miss** ("服务端口是多少") is semantic-neighbor competition: "Qwen3-Embedding部署" in the corpus also contains "端口 11235" ("port 11235"), and the expected note was pushed out of the top-3. Mitigation directions (P4 will decide based on data): query rewriting (agents issue keyword-style queries when retrieving), raising the limit, or tuning RRF weights.
4. audit is clean on the corpus (title-dups=0, collisions=0, dangling=0) — the guards and detectors produce no false positives on a corpus with no violations.

## 5. Query-Phrasing Convention for Agents

The baseline data directly supports one system-prompt convention: **prefer keyword-style queries when retrieving** ("端口 9721" ("port 9721") beats "端口是多少" ("what is the port")). Already written into the usage instructions in `01-architecture.md` Section 8, item 1, and the tool docstrings in `02-mcp-tools.md`.

## 6. Extending

- Swap `CORPUS`/`QUERIES` for your own exported real notes and real questions, run with `--keep`, and check the results by hand;
- `kind="fts"/"vector"` forces a single channel to isolate problems (if hybrid underperforms a single channel, inspect the RRF implementation first);
- After P4 ends, rerun once more to compare metric decay at real memory volume (hundreds of notes).

## 7. Negative Retrieval Assertions (added 2026-09-19)

Recall baselines only test "what should come back does come back"; negative assertions test **"what must not come back does not come back"** — without the latter, silent leaks may only surface after years in service. Reference: item 7 of the agent-memory-atlas rubric (Negative retrieval assertion: committed evaluation cases assert that specific content must not be retrieved).

`eval_search.py` now contains three of them, output in the `Negative assertions` section after the recall table:

| # | Assertion | Verification path |
|---|---|---|
| N1 | A deleted note must not be recalled by any channel | write a note containing a unique passphrase → `memory_delete` → search fts/hybrid(/vector) for its keywords and assert it does not appear |
| N2 | A manually dispositioned (dismissed) D2 collision pair must no longer show ⚠ | two notes with the same fact → search to confirm ⚠ → set the disposition semantics to dismissed in the WebUI → search again and assert no ⚠ (D2 depends on the vector channel; automatically skipped in FTS-only runs) |
| N3 | No cross-user leakage between stores | search this store's unique content from an independent second, empty store and assert it does not appear (isolation is guaranteed by a separate root per user; see 01-architecture §9/§15) |

Convention: negative assertions are treated exactly like the recall baseline — **any ✗ is handled as a regression**; "occasional" failures are not allowed.

Also: since 0.1.1, writes are subject to the topic registry; the evaluation corpus is now registered as the topic "检索评测语料" (retrieval-evaluation corpus) and written into the `topics/检索评测语料/` directory (note titles unchanged, so the baseline numbers remain comparable with the 2026-09-14 old baseline).
