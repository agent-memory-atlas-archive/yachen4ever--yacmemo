# yacmemo

Personal memory layer with API-enforced consistency — markdown-first, local-only, agent-agnostic.

## What it is

yacmemo gives **any AI agent on any of your machines** one shared, self-consistent long-term memory:

- **Markdown is the source of truth** — notes are plain files on your server: human-readable, git-versioned, Obsidian-compatible. The SQLite + LanceDB indexes are derived and rebuildable at any time.
- **One service, every device** — a single MCP server runs where the data lives (streamable HTTP). Claude Code, Codex, Cursor, your own runtime — any MCP client just adds a URL. Nothing to install client-side.
- **Zero generative LLM in the loop** — the only model call is a 0.6B embedding. Structure comes from conventions, consistency comes from deterministic API guards, judgment comes from your main model at read time.
- **Consistency is enforced, not hoped for** — `memory_write` refuses near-duplicate titles, `memory_edit` requires unique anchors, collisions are flagged at search time and never silently hidden.

## Architecture

```
your machines (any MCP agent)
   │  add one URL, nothing to install:
   │  http://debsvc.local:9721/yachen/mcp
   ▼
yacmemo-server (single process, streamable HTTP, stateless sessions)
   ├── /yachen/mcp → Store(root=.../yachen/memory)
   └── /wife/mcp   → Store(root=.../wife/memory)
         store.py      CRUD + write guards + sync indexing
         search.py     FTS5 trigram + vector, RRF fusion
         detectors.py  deterministic D1/D3 checks
         index_db.py   SQLite: metadata/FTS/collisions/guard events
         vector.py     LanceDB: note + observation vectors
         embedding.py  the only model call (0.6B, ~50ms)
   ▼
markdown files (source of truth, git-versioned)
```

## Quick start

### Server (where your data lives)

```bash
git clone <your-repo> yacmemo && cd yacmemo
uv sync
cp config.example.toml config.toml   # set embedding endpoint + user roots
uv run yacmemo-server --config config.toml
curl http://127.0.0.1:9721/health    # → {"status":"ok","users":["wife","yachen"]}
```

### Clients (every machine, every agent)

```
http://debsvc.local:9721/yachen/mcp
http://debsvc.local:9721/wife/mcp
```

```bash
# Claude Code
claude mcp add --transport http yacmemo http://debsvc.local:9721/yachen/mcp
# Codex CLI
codex mcp add yacmemo --url http://debsvc.local:9721/yachen/mcp
```

Same-box agents can use stdio instead: `uv run yacmemo-mcp --root /path/to/memory`.

## MCP tools (8)

| Tool | Purpose |
|---|---|
| `memory_search` | Hybrid retrieval (FTS trigram + vector, RRF); inlines ⚠ duplicate/contradiction warnings |
| `memory_read` | Full note + related notes (wiki-links + semantic neighbors) |
| `memory_write` | New note; **refuses near-duplicate titles** (force needs two-step confirmation) |
| `memory_edit` | In-place update with a **unique** text anchor |
| `memory_edit_section` | Replace one `##` section |
| `memory_move` | Move file, indexes follow |
| `memory_audit` | Self-healing consistency audit (external edits/deletes, D1/D2/D3, guard stats) |
| `memory_list` | Directory tree / recent changes |

Full specs: [docs/02-mcp-tools.md](docs/02-mcp-tools.md). Agent usage conventions: [docs/01-architecture.md](docs/01-architecture.md) §8.

## Docs

| Doc | Content |
|---|---|
| [01-architecture.md](docs/01-architecture.md) | Design, decision record, principles |
| [02-mcp-tools.md](docs/02-mcp-tools.md) | Tool specifications |
| [03-storage-and-search.md](docs/03-storage-and-search.md) | File format, index schema, hybrid retrieval, self-healing |
| [04-consistency.md](docs/04-consistency.md) | Three defense layers, force ladder, metrics |
| [05-deployment.md](docs/05-deployment.md) | systemd, client configs, backup, security |
| [06-evaluation.md](docs/06-evaluation.md) | Retrieval baseline & how to rerun |

Legacy v1 (three-tier extraction) is frozen in [`legacy/`](legacy/) — kept as a decision record.

## Tech stack

Python 3.11+ · mcp SDK (FastMCP) · SQLite (FTS5 trigram, WAL) · LanceDB · Qwen3-Embedding-0.6B via any OpenAI-compatible endpoint · rapidfuzz. Single service process; no daemons, no queues, no cron, no graph DB, no second LLM.

## License

MIT
