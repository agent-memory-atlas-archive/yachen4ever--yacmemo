> English | [简体中文](../05-deployment.md)

# Deployment Guide

> Target shape: **one service (debsvc), for every computer, for every agent**. Memory data is stored in a single place; zero installation on every client.

## 1. Server Side (debsvc)

### 1.1 Installation

```bash
git clone <your-repo> /srv/yacmemo && cd /srv/yacmemo
uv sync
cp config.example.toml config.toml
```

Key items in `config.toml` (already in .gitignore):

```toml
[embedding]
base_url = "http://m2ultra:11235/v1"       # 你的 omlx
api_key = "sk-..."
model = "Qwen3-Embedding-0.6B-4bit-DWQ"    # 必须与 /v1/models 返回的 ID 一致
dimensions = 1024

[server]
host = "0.0.0.0"    # 供局域网各机器访问
port = 9721

[[users]]
id = "yachen"
root = "/srv/yacmemo/yachen/memory"

[[users]]
id = "user2"
root = "/srv/yacmemo/user2/memory"
```

Notes:

- The user `id` is used as the URL path; only `[A-Za-z0-9_-]` is allowed (1–32 characters), validated at startup;
- A `root` directory that does not exist is created automatically; `.index/` (SQLite + LanceDB) is generated inside each root and kept out of git;
- Leaving embedding empty = FTS-only mode (semantic retrieval and D2 collision detection switch off automatically; everything else remains fully functional).

### 1.2 First Start and Verification

```bash
mkdir -p /srv/yacmemo/yachen/memory /srv/yacmemo/user2/memory
uv run yacmemo-server --config config.toml
curl http://127.0.0.1:9721/health
# → {"status":"ok","users":["user2","yachen"]}
```

### 1.3 Running Persistently under systemd

```ini
# /etc/systemd/system/yacmemo.service
[Unit]
Description=yacmemo memory MCP server
After=network-online.target

[Service]
WorkingDirectory=/srv/yacmemo
# git 快照需要 HOME：systemd 默认不设，git 读不到 ~/.gitconfig 的
# safe.directory 豁免，会对非本用户属主的 memory 目录报 dubious ownership
# 导致快照静默降级（代码层已有 pwd 回填兑底，这里显式声明更稳）
Environment=HOME=/root
ExecStart=/srv/yacmemo/.venv/bin/yacmemo-server --config /srv/yacmemo/config.toml
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now yacmemo
```

### 1.4 Memory Repository and git Snapshots

The memory directory is a git repository: every write/edit/move/delete/topic operation automatically produces a commit, and the repository stays git-clean:

- The first write runs `git init` automatically; `.index/` is added to `.gitignore` automatically;
- Commit identity: the `[[users]]` keys `git_user_name` / `git_user_email` (optional) → defaults `<id>` / `<id>@yacmemo.com`; an identity already configured on the repository (local/global) is never overwritten;
- External edits (Obsidian/vim) are consolidated as `external:` snapshots at the next audit;
- When git is unavailable, only the snapshot is skipped and writes are never blocked; the `== git ==` line at the end of the audit output shows the most recent failure reason — after deployment, glance at it once to confirm the feature is "enabled";
- **The unit must have HOME** (see the comment in 1.3);
- No remote: the memory repository is purely local; remote backup (private remote / periodic `git bundle`) is listed as a follow-up feature.

## 2. Clients (Every Computer of Yours, Any Agent)

Any client that supports remote MCP only needs a URL added — **no need to install python/uv/this repository**:

| Client | How to configure |
|---|---|
| Claude Code | `claude mcp add --transport http yacmemo http://debsvc.local:9721/yachen/mcp` |
| Codex CLI | `codex mcp add yacmemo --url http://debsvc.local:9721/yachen/mcp` |
| Cursor / Claude Desktop etc. | in mcp.json: `"yacmemo": {"url": "http://debsvc.local:9721/yachen/mcp"}` (type remote/http) |
| TeleAgent desktop | the official JSON only covers the stdio form — try a direct `url` connection first; if that fails, bridge via mcp-proxy (see 2.1.1) |
| In-house runtime | connect to streamable HTTP with any MCP client library; or use the `mcp` SDK directly |

#### 2.1.0 Identity token (agent+device exclusive memory, since 2026-09-24)

Add a request header to the URL to state the identity and unlock the `agents/` exclusive memory zone (tier model: [02-mcp-tools.md §0](02-mcp-tools.md)):

```
Authorization: Bearer <device>_<agent>        # e.g. r9000x_teleagent
```

| Client | Configuration |
|---|---|
| Claude Code | `claude mcp add --transport http yacmemo <URL> --header "Authorization: Bearer r9000x_teleagent"` |
| mcp.json-style clients | `"yacmemo": {"url": "...", "headers": {"Authorization": "Bearer r9000x_teleagent"}}` |
| same-machine stdio | environment variable `YACMEMO_TOKEN=r9000x_teleagent` |

The token is a deterministic `<device>_<agent>` concatenation (lowercase letters/digits/dashes); the WebUI "Identities" page validates names and generates config snippets. Legacy setups without a token keep working on the user tier, but the exclusive zone is invisible and unwritable. Re-installing a machine does not mean a new token — a different device name is simply a new identity.

#### 2.1.1 TeleAgent Desktop Integration

TeleAgent's MCP JSON (Settings → Tool Settings → Import from JSON) documents only the `command/args/env` fields (stdio form). Two paths:

**① Try a remote direct connection first** (newer clients mostly support remote MCP already — paste it and see whether the green "Connected" indicator lights up):

```json
{
  "mcpServers": {
    "yacmemo": {
      "url": "http://192.168.5.7:9721/yachen/mcp"
    }
  }
}
```

**② If unsupported, use a stdio bridge** (verified end-to-end on Windows: stdio → mcp-proxy → HTTP → debsvc, with all 8 tools and vector retrieval working). Prerequisite: the machine running TeleAgent has [uv](https://docs.astral.sh/uv/) installed (a one-time `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` will do):

```json
{
  "mcpServers": {
    "yacmemo": {
      "command": "uvx",
      "args": ["--with", "mcp<2", "mcp-proxy", "--transport", "streamablehttp",
               "http://192.168.5.7:9721/yachen/mcp"]
    }
  }
}
```

Machines with Node can also use `npx -y mcp-remote http://192.168.5.7:9721/yachen/mcp`.

Notes:

- `--with "mcp<2"` must be kept — mcp-proxy is not yet compatible with mcp SDK 2.x;
- On user2's devices, swap the path to `/user2/mcp`;
- For the first write-type operations, TeleAgent pops up an "Awaiting Authorization" prompt; during the trial period, consider choosing "Always allow" for the memory tools;
- After connecting, check the WebUI usage page to confirm the call left a trace (the client column shows the caller's UA).

- On user2's devices, just swap the path to `/user2/mcp` — **same service, same port**;
- Replace `debsvc.local` with the actual hostname/IP (192.168.5.7);
- Stateless sessions (stateless HTTP): client reconnects, proxies, and multi-window concurrency all work without session affinity.

### 2.1 Same-Machine stdio Mode (Optional)

Agents running on the debsvc machine itself can use stdio to skip one network hop:

```bash
uv run yacmemo-mcp --root /srv/yacmemo/yachen/memory
```

The tool surface is exactly the same as in HTTP mode (the same `register_tools`).

### 2.2 WebUI Console

The frontend is a Vue 3 + Naive UI project and **needs building** (servers usually have no npm, so build on a dev machine):

```bash
# 一条命令：本机构建 + scp 到 debsvc（推荐）
scripts/deploy_webui.sh         # = npm ci + build + tar 管道同步 dist，末尾自检 /ui/

# 或分步手动
scripts/build_webui.sh          # → 产物落 yacmemo/webui/dist/
scp -r yacmemo/webui/dist debsvc:/srv/yacmemo/yacmemo/webui/   # dist 不进 git
```

Frontend-only updates need no service restart (static files are read from disk per request); **backend code updates** = `git pull && systemctl restart yacmemo`. The version number and commit hash are shown at the bottom of the sidebar (injected at build time by vite from package.json + git).

Without a build the service still runs normally, `/ui/` returns a 503 with build instructions, and MCP/API are unaffected. After building, open `http://debsvc.local:9721/ui/` in a browser (`/` redirects automatically). The five pages:

- **Topics**: the topic tree (registered topics → notes within a topic), with markdown rendering on the right, online editing (whole-file save, index synced) / deletion (confirm); the abstract cannot be deleted from the UI;
- **Search**: manually verify three-channel retrieval; ⚠ collision flags are visible;
- **Audit**: two tabs — "Deterministic Audit" (disposition cards grouped by D1–D5, disposition history, historical snapshots) and "Quality Proposals" (curator deep review, structured display of items, manual adopt/ignore + copy execution instructions for an agent to carry out); see [07-webui.md](07-webui.md) §2.3 for details;
- **Profile**: view/edit/create each section of PROFILE.md;
- **Settings**: usage log (tool filter, last-14-days overview) + health overview + full index rebuild (maintenance card) + config.toml online editing (validation + backup + optional restart).

For a complete description of the pages and APIs see [07-webui.md](07-webui.md). The WebUI shares the process and port with MCP and has no separate authentication — it follows the "personal use on the internal network" trust boundary; if broader exposure is needed, put an authenticating reverse proxy in front (same security boundary as below).

### 2.3 curator Quality Curation (Optional)

Once the `[curator]` config section points at the main-model endpoint, deploy the weekly timer:

```bash
# /etc/systemd/system/yacmemo-curator.service（Type=oneshot，ExecStart=.venv/bin/yacmemo-curator --config ...）
# /etc/systemd/system/yacmemo-curator.timer（OnCalendar=Sat *-*-* 04:00:00, Persistent=true）
systemctl enable --now yacmemo-curator.timer
```

Each week it produces 《curator/提案-<日期>.md》 (curator/proposal-<date>.md) — **proposals only, never executes**; adjudication happens on the WebUI audit page or by letting an agent execute. See [01-architecture.md](01-architecture.md) §13.

## 3. System-Prompt Convention Block

Paste into the system prompt of every agent that will write memories (original text: `01-architecture.md`, Section 8):

```text
# 记忆使用约定（yacmemo）
会话开始：
0. 先调 memory_context 回顾画像/偏好与主题体系；需要时用 topic_list 查看主题清单。
写入前：
1. 先查后写。写任何记忆前，先用 memory_search 查是否已有同主题笔记。
2. 已有同主题笔记 → memory_edit / memory_edit_section 增量修改，绝不新建重复笔记。
   old_string 从 memory_read 返回的 [正文开始]/[正文结束] 块内逐字复制（勿凭记忆重打）；
   "相关笔记"等标记之后的内容是工具附加信息，不是文件内容，不可作锚点。
3. 新建时标题 = 主题名（如"yacmemo部署配置"），禁止日期后缀和"-2"/"新"等尾巴
   （时间线流水放 journal/ 目录）。
写入时：
4. 写提炼后的结论，不贴对话原文；一篇笔记一个主题。状态/部署/选型类信息**就地更新已有笔记**，不新建带日期的快照（标题守卫会拦截同名新笔记）；过程性记录（调研/评估/排查）放 journal/ 或不存。
5. 事实行用 observation 语法：- [配置] 服务端口为 9721
6. 与其他笔记相关时写关系：- 部署于 [[debsvc]]
检索时：
7. memory_search 结果带 ⚠ 标注时，先读两篇，用 memory_edit 合并，然后才回答用户。
8. 探索一个主题用 memory_read 的相关笔记链路，不要只凭单条搜索结果下结论。
主题：
9. 主题的注册、注销与归档都只在用户明确要求时操作（"把 X 加入长期记忆" / "X 不用长期记录了" / "X 归档吧"）→ topic_register / topic_unregister / archive_topic；主题现状写入 abstract（topics/<主题>/abstract.md）并就地更新，目录内可按模块增设详细 md。
10. 只在注册主题内写笔记；journal/、archive/、curator/ 之外发现游离文件时提示用户归位。
删除：
11. memory_delete 仅在用户明确要求时调用（"删掉 X"/"X 不用记了"）；每次删除自动产生 git 快照，历史可恢复。
```

Even if the conventions are broken there is a backstop: guard refusals + two-stage force confirmation + audit self-healing (see `04-consistency.md`).

## 4. Data Management

- **git**: one repository per memory_root (`.index/` is ignored). **Snapshots are fully automatic**: every write/edit/delete/topic operation commits automatically and the repository is always git-clean (see 1.4); no manual maintenance needed;
- **Backup**: back up the two memory directories (skipping `.index` is fine — indexes are rebuildable);
- **Obsidian**: use Syncthing/a shared mount to sync the memory directory to a desktop machine and browse it directly;
- **Index rebuild**: after deleting `.index/`, the self-healing triggered by any `memory_audit` or a restart performs a full rebuild (use `reindex` when content is large);
- **Migration**: changing machines = copy the memory directory + update the config paths.

## 5. Security Boundary (Internal-Network Personal Use Assumption)

- The service has no authentication; binding `0.0.0.0` means any device on the LAN can read and write the corresponding memories — the home LAN is the trust boundary; for broader exposure, front it with an authenticating reverse proxy;
- User isolation = URL path = disk directory, with the boundary fixed at Store construction time (tests cover that the two users cannot see each other);
- `memory_read`/`memory_search` only operate inside the bound root; there is no path-traversal escape (`..` and paths outside the root are refused).

## 6. Acceptance Checklist (P3)

- [ ] `systemctl status yacmemo` is healthy and `/health` returns both users
- [ ] Laptop A (Claude Code) writes → phone/another machine (Cursor) can find it by search
- [ ] Open /ui/ in a browser: notes can be browsed and edited, the audit runs, and the usage log shows the write just made
- [ ] The user2 instance and the yachen instance cannot see each other (both writing the same title causes no conflict)
- [ ] Manually edit a file in the memory directory → `memory_audit` reports "external modification rebuilt"
- [ ] Disconnect omlx and write → content persists and FTS can still find it; after recovery, vectors are backfilled automatically
