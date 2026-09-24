> [English](en/05-deployment.md) | 简体中文

# 部署指南

> 目标形态：**一个服务（debsvc），所有电脑，所有 agent**。记忆数据单点存放，各端零安装。

## 一、服务端（debsvc）

### 1.1 安装

```bash
git clone <your-repo> /srv/yacmemo && cd /srv/yacmemo
uv sync
cp config.example.toml config.toml
```

`config.toml`（已在 .gitignore）关键项：

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

注意：

- 用户 `id` 用作 URL 路径，仅允许 `[A-Za-z0-9_-]`（1–32 位），启动时校验；
- `root` 目录不存在会自动创建；`.index/`（SQLite + LanceDB）生成在各 root 内，不进 git；
- embedding 留空 = FTS-only 模式（语义检索与 D2 撞车检测自动关闭，其余全功能）。

### 1.2 首次启动与验证

```bash
mkdir -p /srv/yacmemo/yachen/memory /srv/yacmemo/user2/memory
uv run yacmemo-server --config config.toml
curl http://127.0.0.1:9721/health
# → {"status":"ok","users":["user2","yachen"]}
```

### 1.3 systemd 常驻

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

### 1.4 记忆仓库与 git 快照

memory 目录就是 git 仓库，每次写入/编辑/移动/删除/主题操作自动产生一条 commit，仓库保持 git-clean：

- 首次写入自动 `git init`，`.index/` 自动入 `.gitignore`；
- commit 身份：`[[users]]` 的 `git_user_name` / `git_user_email`（可选）→ 默认 `<id>` / `<id>@yacmemo.com`；仓库已有身份（local/global）绝不覆盖；
- 外部编辑（Obsidian/vim）在下次 audit 时以 `external:` 快照统一收编；
- git 不可用时只跳过快照、不阻塞写入；audit 输出末尾的 `== git ==` 行会显示最近一次失败原因，部署后建议看一眼确认"启用"；
- **unit 必须有 HOME**（见 1.3 注释）；
- 无远程：记忆仓库纯本地，远程备份（私有 remote / 定期 `git bundle`）列为后续功能。

## 二、客户端（你的每台电脑，任意 agent）

所有支持远程 MCP 的客户端只需添加 URL，**无需安装 python/uv/本仓库**：

| 客户端 | 配置方式 |
|---|---|
| Claude Code | `claude mcp add --transport http yacmemo http://debsvc.local:9721/yachen/mcp` |
| Codex CLI | `codex mcp add yacmemo --url http://debsvc.local:9721/yachen/mcp` |
| Cursor / Claude Desktop 等 | mcp.json 中 `"yacmemo": {"url": "http://debsvc.local:9721/yachen/mcp"}`（类型 remote/http） |
| TeleAgent 桌面版 | 官方 JSON 仅 stdio 形态——先试 `url` 直连，不行用 mcp-proxy 桥接（见 2.1.1） |
| 自研 runtime | 任意 MCP 客户端库连 streamable HTTP；或直接用 `mcp` SDK |

#### 2.1.0 identity token（agent+设备专属记忆，2026-09-24 起）

在 URL 后加请求头即可表明身份，启用 `agents/` 专属记忆区（层级模型见 [02-mcp-tools.md §0](02-mcp-tools.md)）：

```
Authorization: Bearer <device>_<agent>        # 如 r9000x_teleagent
```

| 客户端 | 配置方式 |
|---|---|
| Claude Code | `claude mcp add --transport http yacmemo <URL> --header "Authorization: Bearer r9000x_teleagent"` |
| mcp.json 类客户端 | `"yacmemo": {"url": "...", "headers": {"Authorization": "Bearer r9000x_teleagent"}}` |
| 同机 stdio | 环境变量 `YACMEMO_TOKEN=r9000x_teleagent` |

token 为确定性拼接 `<device>_<agent>`（小写字母/数字/短横线），可在 WebUI「身份」页校验并生成配置片段。不携带 token 的旧配置照常可用 user 层，但专属区不可见不可写。同一 agent 换机/重装无需换 token——换个 device 名即新 identity。

#### 2.1.1 TeleAgent 桌面版接入

TeleAgent 的 MCP JSON（设置 → 工具设置 → 从 JSON 导入）文档化字段只有 `command/args/env`（stdio 形态）。两条路径：

**① 先试远程直连**（新版客户端多已支持远程 MCP，粘贴后看是否亮绿色"已连接"）：

```json
{
  "mcpServers": {
    "yacmemo": {
      "url": "http://192.168.5.7:9721/yachen/mcp"
    }
  }
}
```

**② 不支持则用 stdio 桥接**（已在 Windows 上端到端验证：stdio → mcp-proxy → HTTP → debsvc，8 工具与向量检索均正常）。前提：运行 TeleAgent 的机器装有 [uv](https://docs.astral.sh/uv/)（`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` 一次即可）：

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

装了 Node 的机器也可用 `npx -y mcp-remote http://192.168.5.7:9721/yachen/mcp`。

注意：

- `--with "mcp<2"` 必须保留——mcp-proxy 尚不兼容 mcp SDK 2.x；
- user2 的设备把路径换成 `/user2/mcp`；
- 首次写入类操作 TeleAgent 会弹【等待授权】，试用期建议对记忆工具选"一直允许"；
- 连接成功后到 WebUI 使用记录页确认调用留痕（客户端列会显示调用方 UA）。

- 用户2的设备把路径换成 `/user2/mcp` 即可，**同一台服务、同一个端口**；
- `debsvc.local` 换成实际主机名/IP（192.168.5.7）；
- 无状态会话（stateless HTTP）：客户端重连、代理、多窗口并发都无需会话亲和。

### 2.1 同机 stdio 模式（可选）

跑在 debsvc 本机的 agent 可用 stdio 省一层网络：

```bash
uv run yacmemo-mcp --root /srv/yacmemo/yachen/memory
```

工具面与 HTTP 模式完全一致（同一份 `register_tools`）。

### 2.2 WebUI 控制台

前端为 Vue 3 + Naive UI 工程，**需构建**（服务器通常无 npm，在开发机做）：

```bash
# 一条命令：本机构建 + scp 到 debsvc（推荐）
scripts/deploy_webui.sh         # = npm ci + build + tar 管道同步 dist，末尾自检 /ui/

# 或分步手动
scripts/build_webui.sh          # → 产物落 yacmemo/webui/dist/
scp -r yacmemo/webui/dist debsvc:/srv/yacmemo/yacmemo/webui/   # dist 不进 git
```

纯前端更新无需重启服务（静态文件按请求读盘）；**后端代码更新** = `git pull && systemctl restart yacmemo`。版本号与 commit 号在侧边栏底部展示（构建期由 vite 从 package.json + git 注入）。

未构建时服务照常运行，`/ui/` 返回 503 构建指引，MCP/API 不受影响。构建完成后浏览器打开 `http://debsvc.local:9721/ui/`（`/` 自动跳转），五页：

- **主题**：主题树（注册主题 → 主题内笔记），右侧 markdown 渲染、在线编辑（整篇保存，索引同步）/删除（confirm），abstract 不可从 UI 删；
- **搜索**：手动验证三通道检索，⚠ 撞车标注可见；
- **审计**：双 Tab——「确定性审计」（D1–D5 分组处置卡片、处置历史、历史快照）与「质量提案」（curator 深度审查、条目结构化展示、人工采纳/忽略 + 复制执行指令交 agent 落实）；详见 [07-webui.md](07-webui.md) §2.3；
- **画像**：PROFILE.md 各小节的查看/编辑/新建；
- **设置**：使用记录（工具过滤、近 14 天概览）+ 健康总览 + 全量重建索引（维护卡片）+ config.toml 在线编辑（校验 + 备份 + 可选重启）。

页面与 API 的完整说明见 [07-webui.md](07-webui.md)。WebUI 与 MCP 同进程同端口，无独立鉴权——遵循"内网自用"的信任边界；如需暴露更广，前置反代加认证（同下文安全边界）。

### 2.3 curator 质量策展（可选）

`[curator]` 配置节指向主模型端点后，部署每周 timer：

```bash
# /etc/systemd/system/yacmemo-curator.service（Type=oneshot，ExecStart=.venv/bin/yacmemo-curator --config ...）
# /etc/systemd/system/yacmemo-curator.timer（OnCalendar=Sat *-*-* 04:00:00, Persistent=true）
systemctl enable --now yacmemo-curator.timer
```

每周产出《curator/提案-<日期>.md》——**只提案，绝不执行**；裁决走 WebUI 审计页或让 agent 执行。详见 [01-architecture.md](01-architecture.md) §十三。

## 三、系统提示约定块

贴进每个会写记忆的 agent 的系统提示（原文见 `01-architecture.md` 第八节）：

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

不守约也有兜底：守卫拒绝 + force 两级确认 + audit 自愈（见 `04-consistency.md`）。

## 四、数据管理

- **git**：每个 memory_root 一个仓库（`.index/` 已 ignore）。**快照全自动**：每次写入/编辑/删除/主题操作自动 commit，仓库永远 git-clean（见 1.4），无需人工维护；
- **备份**：备份两个 memory 目录（含 `.index` 可省，索引可重建）；
- **Obsidian**：Syncthing/共享挂载把 memory 目录同步到桌面机，直接打开浏览；
- **索引重建**：删除 `.index/` 后由任意一次 `memory_audit` 触发的自愈或重启即可全量重建（内容多时用 `reindex`）；
- **迁移**：换机 = 拷贝 memory 目录 + 改 config 路径。

## 五、安全边界（内网自用假设）

- 服务无鉴权，绑定 `0.0.0.0` 意味着 LAN 内任意设备可读写对应记忆——家庭内网是信任边界；若需暴露更广，前置反代加认证；
- 用户隔离 = URL 路径 = 磁盘目录，Store 构造时固定边界（有测试覆盖双用户互不可见）；
- `memory_read`/`memory_search` 只在绑定 root 内活动，无路径穿越出口（`..` 与根外路径拒绝）。

## 六、验收清单（P3）

- [ ] `systemctl status yacmemo` 正常，`/health` 返回两个用户
- [ ] 笔记本 A（Claude Code）写入 → 手机/另一台（Cursor）能搜到
- [ ] 浏览器打开 /ui/ ：笔记可浏览编辑、审计可运行、使用记录能看到刚才那次写入
- [ ] user2 实例与 yachen 实例互不可见（各写同名标题不冲突）
- [ ] 直接在 memory 目录手动改一个文件 → `memory_audit` 报告"外部修改已重建"
- [ ] 断开 omlx 写入 → 内容仍在、FTS 可搜；恢复后向量自动补齐