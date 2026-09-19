> [English](en/08-agent-config.md) | 简体中文

# Agent 记忆接入与本地配置维护

> 本文说明各 AI agent 如何接入 yacmemo 记忆层，以及**桌面 agent 本地配置文件（USER.md / MEMORY.md）的维护约定**——避免架构升级后本地配置描述过时。

## 一、接入方式（两层，缺一不可）

| 层 | 内容 | 作用 |
|---|---|---|
| **MCP 连接** | 客户端加一个 URL（`http://<host>:9721/<user>/mcp`）或 stdio | 数据路径：所有读写/检索都走 yacmemo，本地零安装零进程 |
| **系统提示约定块** | 把 [01-architecture.md](01-architecture.md) §八的「记忆使用约定」贴进 agent 系统提示 | 行为路径：告诉 agent 何时用哪个工具、怎么遵守主题/守卫纪律 |

> MCP 端点清单与 CLI 接入示例见 [05-deployment.md](05-deployment.md) §2.1；约定块原文见 01-architecture.md §八。

## 二、桌面 agent 的本地记忆文件（USER.md / MEMORY.md）

TeleAgent 这类桌面 agent 自带本地记忆机制：`USER.md`（用户指定长期记忆）+ `MEMORY.md`（系统推断核心事实）。**维护约定：本地文件只放"指针"**——不落事实副本、不重复维护数据，把位置和用法指向 yacmemo。yacmemo 架构变动时只需刷新这几个指针。

### USER.md 模板（当前）

```markdown
## 长期记忆（yacmemo）
- 长期记忆统一存 yacmemo（MCP，主题注册制），多终端多 agent 共享；本地文件只存指针、不存事实副本
- 接入契约版本: 0.2.1（每次会话开始与 memory_context 头部比对；落后即调 integration_check 自主更新本节，见 docs/09 §四）
- 会话开始先 memory_context 回顾主题体系；写入前先 memory_search 查重，已有同主题笔记用 memory_edit / memory_edit_section 就地更新
- 画像/偏好存 PROFILE.md 功能层：get_user_preference / update_user_preference 读写；SSH 别名、项目、设备、网络等背景一律 memory_search / memory_read 检索
- 使用约定见 yacmemo 主题卡「yacmemo 记忆使用约定」与仓库 docs/02-mcp-tools.md

## 运行环境
- 本机角色（如：TeleAgent 桌面端运行于 m5air）；全局 git 身份
```

### MEMORY.md 模板（当前）

```markdown
- 长期记忆统一存 yacmemo（MCP），本文件不再积累记忆；新增长期事实写入 yacmemo 对应主题（先 memory_search 查重，再 memory_edit 就地更新）
- 用户画像/偏好已存 yacmemo PROFILE.md 功能层（get_user_preference / update_user_preference 读写），不在此文件
```

## 三、什么时候需要更新这些文件

yacmemo 侧发生**结构性变化**后，逐个检查本地配置描述是否过时：

- 工具面增减（13 → 16：archive_topic / get_user_preference / update_user_preference 加入）；
- **接入契约版本前进**（2026-09-19 起）：不必等人工巡检——`memory_context` 头部版本与本地记录比对，落后即 `integration_check` 自主刷新（docs/09 §四）；本节其余条目用于契约机制覆盖不到的部署层变化；
- 画像/偏好从"主题"迁到 PROFILE.md 功能层（旧说法"画像在「用户画像」主题"已失效）；
- 主题目录化（`topics/<主题>/` + abstract.md）、archive/ 归档生命周期；
- 审计行为变化（journal/audit/ 快照落盘、处置记录、AIGC 头清理规则）；
- git 快照全自动后，agent 不再需要 SSH（删除工具在 MCP 里）。

检查口诀：**改完代码 → 同步 yacmemo 记忆 → 顺手确认 USER.md/MEMORY.md 描述仍准确**（不准确就更新指针）。

## 四、维护后的收尾

- 本地配置文件同样是 markdown：TeleAgent 的 AIGC hook 会在修改后注入 `AIGC:` frontmatter，改完用 `python3 ~/code/yacmemo/scripts/strip_aigc.py -r <目录>` 清理（见工作规则主题「文档与文件约定」）；
- yacmemo 仓库内的 README/docs 修改同理，提交前跑一次 strip_aigc.py。

## 五、历史变更对照（为什么现在长这样）

- 2026-09-16 之前：画像存「用户画像」主题，USER/MEMORY 指向主题；
- 2026-09-16：主题目录化 + 画像升格 PROFILE.md 功能层 →「用户画像」主题注销，本地配置更新为指向 PROFILE.md；
- 2026-09-16：审计历史快照落盘 journal/audit/ → agent 可回看历史审计与处置记录。