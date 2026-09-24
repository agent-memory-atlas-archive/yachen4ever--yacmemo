> English | [简体中文](../08-agent-config.md)

# Agent Memory Integration and Local Config Maintenance

> This document explains how each AI agent connects to the yacmemo memory layer, and the **maintenance conventions for desktop agents' local config files (USER.md / MEMORY.md)** — so that local config descriptions do not go stale after architecture upgrades.

## 1. Integration (Two Layers, Both Required)

| Layer | Content | Role |
|---|---|---|
| **MCP connection** | the client adds one URL (`http://<host>:9721/<user>/mcp`) or stdio | data path: all reads/writes/searches go through yacmemo; zero local installation, zero local processes |
| **System-prompt convention block** | paste the "Memory Usage Conventions" from [01-architecture.md](01-architecture.md) §8 into the agent's system prompt | behavior path: tells the agent when to use which tool and how to observe topic/guard discipline |

> For the MCP endpoint list and CLI integration examples see [05-deployment.md](05-deployment.md) §2.1; for the original convention block see 01-architecture.md §8.

## 2. Desktop Agents' Local Memory Files (USER.md / MEMORY.md)

Desktop agents such as TeleAgent ship their own local memory mechanisms: `USER.md` (user-designated long-term memory) + `MEMORY.md` (system-inferred core facts). **Maintenance convention: the local files hold only "pointers"** — no copies of facts, no duplicate data maintenance; they point to yacmemo for location and usage. When the yacmemo architecture changes, only these pointers need refreshing.

### USER.md Template (current)

```markdown
## 长期记忆（yacmemo）
- 长期记忆统一存 yacmemo（MCP，主题注册制），多终端多 agent 共享；本地文件只存指针、不存事实副本
- 接入契约版本: 0.3.1（每次会话开始与 memory_context 头部比对；落后即调 integration_check 自主更新本节，见 docs/09 §四）
- identity token: <device>_<agent>（MCP 配置 Authorization: Bearer 头 / stdio 环境变量 YACMEMO_TOKEN；WebUI 身份页生成；专属必读随 memory_context 自动注入）
- 会话开始先 memory_context 回顾主题体系；写入前先 memory_search 查重，已有同主题笔记用 memory_edit / memory_edit_section 就地更新
- 画像/偏好存 PROFILE.md 功能层：get_user_preference / update_user_preference 读写；SSH 别名、项目、设备、网络等背景一律 memory_search / memory_read 检索
- 使用约定见 yacmemo 主题卡「yacmemo 记忆使用约定」与仓库 docs/02-mcp-tools.md

## 运行环境
- 本机角色（如：TeleAgent 桌面端运行于 m5air）；全局 git 身份
```

### MEMORY.md Template (current)

```markdown
- 长期记忆统一存 yacmemo（MCP），本文件不再积累记忆；新增长期事实写入 yacmemo 对应主题（先 memory_search 查重，再 memory_edit 就地更新）
- 用户画像/偏好已存 yacmemo PROFILE.md 功能层（get_user_preference / update_user_preference 读写），不在此文件
```

## 3. When These Files Need Updating

After a **structural change** on the yacmemo side, check each local config description for staleness:

- Tool surface additions/removals (13 → 16: archive_topic / get_user_preference / update_user_preference added);
- Identity exclusive memory zone launched (2026-09-24, contract 0.3.0): add a token to the MCP config (docs/05 §2.1.0); the agents/ zone is auto-injected by memory_context;
- **The integration contract version advances** (since 2026-09-19): no need to wait for manual inspection — compare the version at the head of `memory_context` against the locally recorded one, and if it lags, run `integration_check` to refresh autonomously (docs/09 §4); the other items in this section cover deployment-layer changes the contract mechanism cannot see;
- Profile & preferences moved from a "topic" to the PROFILE.md feature layer (the old phrasing "the profile lives in the 用户画像 (User Profile) topic" is obsolete);
- Topic directory layout (`topics/<topic>/` + abstract.md) and the archive/ archiving lifecycle;
- Audit behavior changes (journal/audit/ snapshots written to disk, disposition records, AIGC header cleanup rules);
- Once git snapshots became fully automatic, agents no longer need SSH (the delete tool is in MCP).

Checking mantra: **after changing code → sync the yacmemo memory → confirm along the way that the USER.md/MEMORY.md descriptions are still accurate** (update the pointers if not).

## 4. Wrap-Up After Maintenance

- The local config files are markdown too: TeleAgent's AIGC hook injects an `AIGC:` frontmatter after modifications; when done, clean up with `python3 ~/code/yacmemo/scripts/strip_aigc.py -r <目录>` (`<目录>` = target directory) (see the working-rules topic 文档与文件约定 (Document & File Conventions));
- The same applies to README/docs changes inside the yacmemo repository: run strip_aigc.py once before committing.

## 5. Historical Change Log (Why Things Look This Way Now)

- Before 2026-09-16: the profile lived in the 用户画像 (User Profile) topic; USER/MEMORY pointed at the topic;
- 2026-09-16: topic directory layout + the profile promoted to the PROFILE.md feature layer → the 用户画像 topic was unregistered, and the local config was updated to point at PROFILE.md;
- 2026-09-16: audit history snapshots written to journal/audit/ → agents can look back at historical audits and disposition records.
