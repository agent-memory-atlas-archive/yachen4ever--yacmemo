> English | [简体中文](../09-agent-onboarding.md)

# Agent Onboarding Guide (AI-Specific)

> If you are an AI agent (Claude Code / Codex / Cursor / an in-house runtime...) and you are reading this, complete the onboarding self-check below in order, and clearly report the result to the user. This document is an expanded version of the README; for full onboarding instructions see [08-agent-config.md](08-agent-config.md).

## ① Self-check: do you already have the yacmemo MCP configured?

- **Check your tools**: if your available tools include yacmemo tools such as `memory_search` / `memory_read` / `memory_write` / `memory_context` → **already onboarded**, jump straight to ③;
- **Check the config**: in Claude Code run `claude mcp list`; in Codex run `codex mcp list`; in Cursor look at `.cursor/mcp.json`; other clients: check their respective MCP settings.

## ② Not onboarded: guide the user through configuration

Explain to the user that "yacmemo is a long-term memory layer shared across all their devices", confirm with the user the **user ID** matching their identity (e.g. `yachen`), then give the corresponding commands (replacing `<userID>`):

```bash
# Claude Code
claude mcp add --transport http yacmemo http://debsvc.local:9721/<userID>/mcp
# Codex CLI
codex mcp add yacmemo --url http://debsvc.local:9721/<userID>/mcp
# Other MCP clients: add the streamable HTTP endpoint http://<server>:9721/<userID>/mcp
```

After configuration, prompt the user to **restart the session or reconnect MCP**, then return to ① and the self-check will pass. For the same-machine stdio method and the endpoint list, see [08-agent-config.md](08-agent-config.md) and [05-deployment.md](05-deployment.md) §2.1.

## ③ Onboarded: weave memory logic into your workflow and show your status to the user

1. **Session start**: first call `memory_context` for a cold-start review, then show the user a one-line onboarding summary, for example:

   > ✅ yacmemo memory layer onboarded (user: yachen, contract v0.3.0) — 3 profile/preference entries; 12 active topics: "yacmemo deployment config", "backup strategy", ...; 2 exclusive must-reads injected; latest audit has no pending issues.

   An agent that carries an identity token (`Authorization: Bearer <device>_<agent>`; stdio uses the `YACMEMO_TOKEN` env var; mint tokens on the WebUI "Identities" page) automatically gets the `agents/` exclusive memory zone: `memory_context` injects its must-reads, and `memory_search` is scoped to the user tier + its own zone. Without a token the user tier keeps working as before.

2. **Follow memory discipline day to day** (full conventions: [01-architecture.md §8](01-architecture.md); tool specs: [02-mcp-tools.md](02-mcp-tools.md)):

   - Before answering factual questions, run `memory_search` first; if results carry ⚠, read both notes and merge with `memory_edit`, then answer;
   - Deduplicate before writing: if a note on the same topic already exists, **update it in place** with `memory_edit` / `memory_edit_section` instead of creating a duplicate note;
   - **Long-term memory writes only inside registered topic directories** — paths must carry the `topics/` prefix: `topics/<topic>/<note-name>`. Writing `女儿AI陪伴老师/abstract` gets intercepted; writing `topics/女儿AI陪伴老师/abstract` is correct (2026-09-19 TeleAgent field test: after being intercepted for the missing prefix, the agent idled for a round before self-correcting; the interception message now directly provides a retryable title, but don't rely on interception — write it right the first time); new topics require explicit user authorization before `topic_register` (out-of-bounds writes are hard-blocked; `force` does not exempt); running logs go in `journal/`;
   - **Exclusive must-reads go in your identity zone**: `agents/<agent>/shared/必读.md` (shared across that agent's devices) or `agents/<agent>/<device>/必读.md` (this machine only); hold pointers and discipline only — facts always go into topics/; when referencing another tier's file, substitute the real device name and use `<device>` angle-bracket placeholders instead of empty path segments;
   - **The abstract is the summary card** (`topics/<topic>/abstract.md`): keep it to a one-sentence current status, and update it in place with `memory_edit` when the status changes; write detailed content as module notes `topics/<topic>/<note-name>` — don't stuff long text into the abstract;
   - Use observation syntax for fact lines: `- [config] the service port is 9721`;
   - Registering / unregistering / archiving topics, deleting notes: **execute only when the user explicitly asks**.

3. **When unsure, ask**: if you can't find which topic something belongs in, or have doubts about memory content, explain to the user instead of guessing.

## ④ Integration contract version and self-updating

yacmemo's tool semantics and write conventions are versioned (the **integration contract version**); the server does not push — agents pull on their own:

1. **Declare**: record the contract version you onboarded against into your local onboarding prompt / USER.md — one line is enough: `yacmemo integration contract version: 0.1.3`;
2. **Discover**: the `memory_context` response header carries the current contract version, so every session start naturally performs the comparison;
3. **Update**: when you find yourself behind (or the recorded version is empty), call `integration_check(onboarded_version="<your-version>")` — it returns the incremental changes plus the **full write-convention quick reference**; use them to refresh your local onboarding prompt and update the recorded version number, then report one line to the user: "yacmemo integration conventions updated from 0.1.2 to 0.1.3". No need to wait for user instructions, no need to re-read the repo docs.

Mechanism details and the data source for version history: [02-mcp-tools.md §17](02-mcp-tools.md).

## ⑤ Desktop-agent addendum

For agents of the TeleAgent kind that come with their own local memory files (USER.md / MEMORY.md): the local files store pointers only, never copies of facts; templates and maintenance conventions in [08-agent-config.md](08-agent-config.md) §2.
