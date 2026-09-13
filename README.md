# yacmemo

Personal memory layer with three-tier extraction and consistency checking.

## What it does

yacmemo gives your AI agent a persistent, searchable, self-consistent memory:

- **Layer 1** — Agent writes `.md` work logs (free format, no frontmatter required)
- **Layer 2** — A local LLM reads those `.md` files, splits them into independent topics, and extracts entities + events with provenance pointers back to the source
- **Layer 3** — The LLM scans for contradictions between old and new facts, auto-invalidates stale ones (high confidence) or flags for human review (low confidence)

All data stays on your machine. The `.md` files are the source of truth; SQLite + LanceDB are derived indexes that can be rebuilt at any time.

## Architecture

```
memory/                        ← your .md files (Layer 1)
├── projects/
│   ├── 01-data-portal.md      ← Agent writes this
│   └── 01-data-portal/        ← LLM creates split files (Layer 2)
│       ├── feat-a.md
│       └── fix-b.md
.index/
├── memory.db                  ← SQLite (nodes, edges, events, consistency_log)
└── lancedb/                   ← vector index (entity + event embeddings)
```

## Quick start

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for dependency management
- A local LLM server with OpenAI-compatible API (tested with mlx-serve + Ling-3.0-tiny-MLX-4bit)
- An embedding server with OpenAI-compatible API (tested with omlx + Qwen3-Embedding-0.6B)
- `ripgrep` installed on the system

### Setup

```bash
# Clone and install
git clone <your-repo> yacmemo && cd yacmemo
uv sync

# Configure
cp config.example.toml config.toml
# Edit config.toml: set LLM/embedding endpoints, memory_root path

# Initialize (first-time extraction of all .md files)
uv run yacmemo-enhancer --config config.toml --init

# Start the enhancer service (cron + webhook)
uv run yacmemo-enhancer --config config.toml

# In another terminal, start the MCP server (for your AI agent)
uv run yacmemo-mcp
```

### MCP tools

| Tool | Description |
|---|---|
| `memory_search` | Semantic search across entities, events, and edges |
| `memory_grep` | Regex search through .md files (ripgrep) |
| `memory_read` | Read a .md file |
| `memory_write` | Write a .md file (triggers async extraction) |
| `memory_edit` | Edit a .md file (triggers async extraction) |
| `memory_list` | List the .md file tree |
| `memory_history` | View all versions of an entity (including invalidated) |
| `memory_consistency_status` | List pending contradiction reviews |
| `memory_consistency_resolve` | Confirm or dismiss a flagged contradiction |

## Configuration

See `config.example.toml` for all options. Key settings:

- `storage.memory_root` — directory containing your `.md` files
- `llm.model` — model name for extraction + consistency checking
- `llm.enable_thinking` — `false` for speed (recommended for Ling-3.0-tiny)
- `consistency.auto_invalidate` — `true` to auto-invalidate high-confidence contradictions

## Tech stack

| Component | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | LLM I/O bound, not CPU bound |
| LLM | Ling-3.0-tiny-MLX-4bit (7.9B/1.3B activated) | 74% less memory, 3-5x faster than 35B |
| Embedding | Qwen3-Embedding-0.6B-4bit-DWQ (1024-dim) | Lightweight, effective |
| Storage | SQLite (WAL) + LanceDB | Zero-ops, rebuildable from .md |
| MCP | mcp SDK (stdio) | Standard agent interface |
| Scheduler | APScheduler (cron) | Built-in, no external deps |

## License

MIT
