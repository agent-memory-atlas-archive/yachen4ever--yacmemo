> English | [简体中文](../00-user-guide.md)

# User Guide

> This guide is written for **users**: how to get yacmemo up and running, and how to use it well. Design and implementation details live in docs 01–06; this document only covers "how to".
> You interact with yacmemo in two ways: **talking with an agent** (the primary way — the agent calls the tools) and **editing files directly** (Obsidian/vim, available anytime). Both operate on the same set of markdown files.

---

## 1. Five-Minute Setup

### 1. What you need

- A machine that stays on to host the data and the service (hereafter "the server", e.g. debsvc);
- An embedding endpoint reachable on the LAN (optional — without it the system still works, just without semantic retrieval and collision detection);
- Any MCP-capable agent (Claude Code, Codex, Cursor, Claude Desktop, in-house runtimes all work).

### 2. One-time setup on the server

```bash
git clone <your-repo> /srv/yacmemo && cd /srv/yacmemo
uv sync
cp config.example.toml config.toml
```

Edit `config.toml` — three things:

```toml
[embedding]                          # your embedding service
base_url = "http://m2ultra:11235/v1"
api_key = "sk-..."
model = "Qwen3-Embedding-0.6B-4bit-DWQ"   # must match the ID returned by /v1/models exactly

[server]
host = "0.0.0.0"
port = 9721

[[users]]                            # one entry per person; the id is used as the URL path
id = "yachen"
root = "/srv/yacmemo/yachen/memory"
```

Start and verify:

```bash
uv run yacmemo-server --config config.toml
curl http://127.0.0.1:9721/health     # → {"status":"ok","users":[...]}
```

For long-term use, systemd is recommended (see [05-deployment.md](05-deployment.md) §1.3).

### 3. One-time setup on each computer

Add the remote MCP in your agent (Claude Code shown as an example):

```bash
claude mcp add --transport http yacmemo http://debsvc.local:9721/yachen/mcp
```

Cursor / Claude Desktop: add `"yacmemo": {"url": "..."}` to mcp.json; in-house runtimes connect to this URL with any MCP client library. Changing the path = changing the user (`/user2/mcp`).

### 4. Paste in the conventions block

Paste the "memory usage conventions" passage from Section 8 of [01-architecture.md](01-architecture.md) into the agent's system prompt / CLAUDE.md / AGENTS.md. This determines the quality of what the agent writes to memory — it works without pasting (the guards provide a safety net), but the experience is much better with it.

### 5. Verify it works

Tell the agent:

> "Help me remember: the IP of my home server debsvc is 192.168.5.7"

Then open a new session and ask:

> "What is my home server's IP?"

If it answers 192.168.5.7, the whole chain works.

---

## 2. Daily Usage

### 2.1 What to record: conclusions, not processes

The value of memory is "no need to re-explain next time". Worth recording:

- **Facts and configuration**: "the yacmemo service port is 9721", "the NAS backup share path is /volume1/backup";
- **Decisions and reasons**: "chose restic over timeshift, because backups need to span machines";
- **Preferences**: "write commit messages in Chinese", "run tests before deploying".

Not worth recording: conversation transcripts, temporary debug output, one-off intermediate states. **The agent has already distilled the conclusions; you just need to tell it to remember.**

### 2.2 How to record: just talk to the agent

```
"Remember this in memory: yacmemo's service port has been changed to 8080, the reason is..."
```

The agent will: first check whether a note on the same topic exists → if yes, update it with `memory_edit` → if not, create it with `memory_write`. You don't need to specify file names or paths, but knowing these conventions helps you cooperate better:

- **Title = topic name** (e.g. "yacmemo deployment config"); don't add dates or "-2"-style suffixes — the guard rejects near-duplicate titles; that's the anti-duplication mechanism at work, not a malfunction;
- **One subject, one note**: port configuration and backup strategy are two notes — don't mix them into one;
- **A topic must exist before you can write**: writes are confined to directories of registered topics (a hard constraint of the topic registry; agents cannot bypass it) — to have the agent remember something new long-term, first say "add X to long-term memory" (see 2.5); running-log type content goes in `journal/` (a registry-free zone, named by date, never merged).

### 2.3 How to search

```
"In my memory, where is yacmemo's port configuration?"
"How did we solve the LanceDB deletion error before?"
```

Retrieval fuses two channels: FTS + semantic. Field-tested experience (see [06-evaluation.md](06-evaluation.md)):

- **Keyword-style queries hit more often**: "yacmemo port", "restic backup";
- **Two-character short words can be found too** ("port" automatically falls back to substring matching); if the vector channel fails you'll see a degradation hint — in that case, switching to keywords of ≥3 characters is more reliable;
- Natural sentences ("what was the port again?") also hit most of the time via the semantic channel;
- Once found, the agent follows the "related notes" links to read the context — no need to piece together clues yourself.

### 2.4 How to update

When a fact changes, just say:

> "yacmemo's port has been changed to 9721 — update the memory."

The agent uses `memory_edit` to modify in place (the old value is replaced; no two contradictory records are left behind). To rewrite a whole section, use `memory_edit_section`. **If you find two mutually contradictory old records in memory** (historical leftovers), just say "these two contradict each other, merge them" — the agent will handle it according to the ⚠ markers.

### 2.5 Topic registration: primary vs. secondary

Long-term memory is organized by **topics**, and topics are declared explicitly by you. To have something recorded long-term, tell the agent:

> "Add the notecalc project to long-term memory"

The agent calls `topic_register` to register it: recorded into the TOPICS.md registry + creates `topics/<topic>/abstract.md` (the topic directory). Detailed notes on that topic are then written into the topic directory (the agent can add module notes as needed), and when the current status changes the **abstract is updated in place**. **Registration is also the authorization to write**: writes outside topic directories are hard-blocked, so "register first, write later" is the only path to having the agent remember something new long-term. The interception message carries **near-miss diagnostics** — when the agent writes a wrong path (e.g. missing the `topics/` prefix), the error directly provides the corrected write path, so one round of self-correction, no guessing.

- `topic_list` / the WebUI health page let you check which topics exist at any time (active/archived groups);
- **The project is wrapped up**: "archive notecalc-iced" — the agent calls `archive_topic`: the whole topic directory moves into `archive/`, **still searchable**, just no longer injected into session reviews; use unregistration only when you want it completely removed (the notes become stray files, handled via audit adjudication);
- The audit page names "stray files" (loose notes belonging to no topic) — have the agent relocate or delete them;
- The audit also names "notes missing vectors" and automatically retries to fill them in — notes written while the embedding endpoint was down don't lose semantic retrieval forever;
- At session start the agent first calls `memory_context` to review your profile & preferences and all active topics — a new session no longer starts from zero.

### 2.5.1 Profile & preferences: a memory-layer feature, not a topic

Your profile and collaboration preferences (identity, communication style, document formats, etc.) live in the root-level `PROFILE.md`; **it is not a topic** — it is a functional file of the memory layer. Tell the agent:

> "Remember: all my reports must use formal official style."

The agent calls `update_user_preference` to maintain it section by section (e.g. "materials & documentation preferences"). At the start of every session, `memory_context` **injects the profile up front** — any agent "knows you" from step one, without digging through topics.

### 2.6 Weekly curator proposals

In the early hours of every Saturday, the curator (a configurable LLM reviewer) automatically runs a whole-store quality review and produces the Proposal Report. **It only proposes, never executes** — proposals are work items for agents: **there is no "Adopt" step; dispatching is adopting**. In the "Quality Proposals" area of the WebUI audit page, each proposal has exactly two actions:

- **Copy Execution Instruction** — paste it to any agent and work starts; the agent reports progress (started/progress/done/blocked) back to the system in real time, and the full timeline is shown under the item;
- **Ignore** — a false positive or something you don't want done; one click settles it permanently.

Once every finding is executed or dismissed, the proposal is **closed automatically**: a settled marker is stamped at the top of the file and it no longer appears in agents' search results (so old reports can't be dug up and executed twice). Manual deep review anytime: WebUI audit page → "Deep Review Now" (about 1–2 minutes); historical proposals are listed by date and filterable by status. Which LLM the curator uses and which users it covers are configured in the config.toml on the WebUI "Settings" page (saving auto-validates + backs up; changes take effect after a restart). Of course, you can also direct things yourself at any time (as this guide does throughout).

### 2.7 The audit habit

```
"Run a memory audit for me."
```

The agent calls `memory_audit`, which outputs six kinds of information:

| Output | Meaning | What you do |
|---|---|---|
| External changes (index rebuilt) | Files you edited directly in Obsidian/vim; the index has been aligned automatically | Nothing |
| External deletions (index cleaned) | Files you deleted directly; the index has been cleaned | Nothing |
| Duplicate titles / semantic collisions | Two notes that seem to cover the same topic | Decide which one to keep and have the agent merge |
| Dangling links | A `[[link]]` points to a non-existent note | Have the agent create the target or remove the link |
| Dangling topic cards | The abstract file pointed to by the registry does not exist (usually leftovers from manual registry edits / directory reorganization) | Have the agent fix the registry path or rebuild the card |
| Stray files | Loose files that exist but belong to no registered topic | Decide whether to register them as a topic, relocate them into a topic directory, or delete them |

Once a week is recommended, or anytime memory feels "a bit messy".

> **The WebUI audit page is more convenient**, and the whole page follows one role split: **humans only judge (dismiss false positives / dispatch to agents), agents only execute (and report progress), the system only verifies (re-checks confirmed automatically)**. Each issue carries a status tag (pending / executing / executed / verified / dismissed) and two actions — "Copy Execution Instruction" embeds the reporting convention, so pasting it to any agent starts the work; "Ignore" settles a false positive permanently. For issues an agent has fixed, the next audit not reporting them anymore means automatic "verified" — no human sign-off needed; a verified issue that reappears is flagged as regressed. Filter by status; snapshots are taken **once per day** (`journal/audit/<date>.md`; re-running on the same day appends a "Re-review" section), the "Judgment & execution log" merges human dispositions and agent reports chronologically, and expired snapshots are auto-cleaned weekly by the curator (7 days kept by default) — see [07-webui.md](07-webui.md) §2.3 for details.

---

## 3. Editing Files Directly (Without an Agent)

The memory directory is plain markdown; you can bypass the agent entirely:

- **Obsidian**: open the memory directory as a vault and the wiki-link graph works out of the box;
- **vim/any editor**: edit freely.

The system self-heals:

- You edit a file → at the next `memory_audit` (initiated by you or the agent) the index is rebuilt automatically, and parts whose content hash changed are re-vectorized;
- You delete a file → the audit cleans the index and reports it;
- You create a file → the audit picks it up into the index when it recomputes.

These external changes are also absorbed into git as an `external:` snapshot during the audit — index and history stay consistent.

Three rules of discipline:

1. **Don't touch the `.index/` directory** — it's a derived index; touching it is harmless and deleting it is recoverable, but there's no need to go near it;
2. **Start new files with a "# title" line** — the title serves as the identifier for retrieval and merging;
3. **Don't copy-paste your way into near-duplicate titles** ("XX" and "XX-2") — the audit will flag them as duplicates, and you'll have to merge them by hand again.

---

## 4. Multiple Users

One `[[users]]` entry per person, same server, same port:

```toml
[[users]]
id = "user2"
root = "/srv/yacmemo/user2/memory"
```

- On user2's devices, the agent adds `http://debsvc.local:9721/user2/mcp`;
- **Isolation is physical** (separate directories), not field filtering — two people can have identically named notes without affecting each other, and neither can search the other's memory;
- Just paste the same conventions block into each person's system prompt.

---

## 5. Backup and Migration

- **Backup = backing up the two memory directories**. `.index/` doesn't need backing up (rebuildable); the only truly non-regenerable things are the markdown and the git history;
- Git history is automatic: every write/edit/delete/topic operation automatically produces a commit (no manual upkeep, the repo is always clean); accidental deletions can be recovered from history;
- **Migration**: copy the memory directory to the new machine → update the root paths in the config → start. The indexes rebuild automatically.

---

## 6. WebUI Console

Open `http://debsvc.local:9721/ui/` in a browser (bundled with the server, nothing to install):

- **Notes**: browse by user in the left-hand list; the body renders as markdown; "Edit" modifies the source file directly and syncs the index; "＋" creates a new note (the near-duplicate-title guard applies here too; if rejected, tick "Force" and save again — clicking the button on the web page counts as the human confirmation); "Delete" also cleans the index (still recoverable from git);
- **Search**: manually verify retrieval quality anytime; supports switching between the hybrid/fts/vector channels; ⚠ markers are directly visible; clicking a result jumps to the note;
- **Audit**: dual mode — "Deterministic Audit" for quick self-healing + rule detection (duplicate titles/semantic collisions/dangling links/stray files), "Deep Review" produces a proposal report from the configured LLM; both areas run the full workflow: each issue/proposal carries a status tag (pending/executing/executed/verified/dismissed), filters by status, and an expandable agent execution timeline; a human makes exactly two judgments — "Ignore" a false positive, or "Copy Execution Instruction" to dispatch an agent; issues an agent fixed are verified automatically on the next audit;
- **Usage Log**: a usage log of every MCP tool call **and every WebUI console change** (save/create/delete/profile edits, client marked as `webui`) — time, user, tool, content summary, client UA, IP, duration, pre-change content hash. The most important page to watch during the trial period: which clients are active, what they called, what they wrote, whether anything errored;
- **Health**: embedding configuration status, per-user note counts/collision counts/guard statistics, the **topic memory overview table**, curator proposal counts, client inventory, call volumes and error counts over the last 14 days;
- **Settings**: online config.toml editing (users / embedding model / curator LLM) — auto-validated and backed up before saving, with an optional service restart.

For a full description of page functions and APIs, see [07-webui.md](07-webui.md).

**Suggested posture during the trial period**: glance at the Notes page to see what the agent has been writing; use the Search page to verify recall quality; click through the Audit page once a week; observe two weeks of call composition on the Usage Log page — this is exactly the raw data P4 will collect.

---

## 7. FAQ

**Q: The agent's note write was rejected, saying "a note with a near-duplicate title already exists"?**
This is not a malfunction — it's the anti-duplication mechanism doing its job. It means memory already contains a note on the same topic — have the agent "update it" instead of creating a new one. If you're confident it's a different topic, the agent can override with `force=true` (more than 3 forces within 24 hours triggers a second confirmation; all countable audit metrics).

**Q: What does the ⚠ in search results mean?**
"This note and another one may be talking about the same thing." Just have the agent read both and merge. The system never deletes on its own — it would rather make you take an extra look than let a correct memory disappear.

**Q: How should I handle "semantic collisions" in the audit?**
Two copies of the same topic → decide which one to keep and dispatch the merge to an agent (copy the execution instruction); different topics that happen to share one fact → click "Ignore". Collisions are grouped by note pair with both sides' text shown, so you can compare before judging.

**Q: Can't find something you're sure was recorded?**
Troubleshoot in order: ① switch to a keyword-style query ("port 9721" instead of "what's the port"); ② use `memory_list` to check whether the file exists; ③ use `memory_audit` to see whether external changes haven't been aligned; ④ check the embedding endpoint via `/health` and the config — when it's down, semantic retrieval (natural-sentence queries) stops working and results carry a "vector channel unavailable" degradation hint, while keyword retrieval (including the substring fallback for two-character words) keeps working;

**Q: What happens if the embedding service goes down?**
Writes continue as normal, keyword retrieval as normal; natural-sentence retrieval and collision detection pause, and are automatically backfilled after recovery (on the next write or audit). No manual intervention needed.

**Q: Is the index broken / can I delete it?**
Yes you can. Everything in `.index/` is derived data; after deleting it, restart the service (or have the agent run an audit/reindex) and it is fully rebuilt from the markdown. The memory itself is untouched.

**Q: If I switch agents (say, from Claude Code to something else), do I lose the memory?**
No. Memory lives in markdown files on the server; every agent accesses the same data through the same URL. That is precisely this project's reason for existing.

**Q: Will two computers having agents write memory at the same time conflict?**
Concurrent requests are serialized on the server (locked), so SQLite/LanceDB won't be corrupted; when two machines write the same note simultaneously, the later write wins (like every editor). Normal use never hits race conditions.

**Q: I want to reset everything and start over?**
Delete the two memory directories (or open a new branch in git) → restart the service. Index and memory reset to zero together. Deleting `.index/` alone resets only the index.

**Q: How do I delete a specific memory?**
Tell the agent "delete X" and it will call `memory_delete` (it executes only on your explicit request); the WebUI Notes page also has a delete button. A deletion automatically produces a git snapshot, so it can be recovered from history anytime.

---

## 8. Good-Habit Checklist

- [ ] The conventions block is pasted into the system prompt
- [ ] Run `memory_audit` once a week (or click once on the WebUI audit page), and confirm the `== git ==` line shows "enabled" (snapshot mechanism healthy)
- [ ] The memory directory has an off-site backup (local git snapshots guard against accidental deletion, not disk failure)
- [ ] Before letting the agent record heavily, trial it for a week with a few real items to observe its naming and merging habits
- [ ] Keep `journal/` for running logs only; don't bury notes that deserve their own topic in there
- [ ] Once memory grows past a few hundred notes, re-run the [evaluation script](06-evaluation.md) to see the hit rate on real data
