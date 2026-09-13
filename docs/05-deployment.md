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
ExecStart=/srv/yacmemo/.venv/bin/yacmemo-server --config /srv/yacmemo/config.toml
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now yacmemo
```

## 二、客户端（你的每台电脑，任意 agent）

所有支持远程 MCP 的客户端只需添加 URL，**无需安装 python/uv/本仓库**：

| 客户端 | 配置方式 |
|---|---|
| Claude Code | `claude mcp add --transport http yacmemo http://debsvc.local:9721/yachen/mcp` |
| Codex CLI | `codex mcp add yacmemo --url http://debsvc.local:9721/yachen/mcp` |
| Cursor / Claude Desktop 等 | mcp.json 中 `"yacmemo": {"url": "http://debsvc.local:9721/yachen/mcp"}`（类型 remote/http） |
| 自研 runtime | 任意 MCP 客户端库连 streamable HTTP；或直接用 `mcp` SDK |

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

服务端自带，浏览器打开 `http://debsvc.local:9721/ui/` 即用（`/` 自动跳转）：

- **笔记**：markdown 渲染浏览、在线编辑/新建/删除（守卫与索引同步生效）；
- **搜索**：手动验证三通道检索，⚠ 撞车标注可见，点击跳转；
- **审计**：人工触发的全量审计与撞车裁决（已合并/忽略按钮）；
- **使用记录**：全部 MCP 工具调用的留痕（时间/用户/工具/摘要/客户端 UA/IP/耗时），落盘在 `[server].data_dir/usage.db`（默认保留最近 2 万条，自动滚动）；
- **健康**：embedding 状态、各用户笔记数/撞车数/守卫统计、客户端清单、近 14 天调用量。

WebUI 与 MCP 同进程同端口，无独立鉴权——遵循"内网自用"的信任边界；如需暴露更广，前置反代加认证（同下文安全边界）。

## 三、系统提示约定块

贴进每个会写记忆的 agent 的系统提示（原文见 `01-architecture.md` 第八节）：

```text
# 记忆使用约定（yacmemo）
写入前：
1. 先查后写。写任何记忆前，先用 memory_search 查是否已有同主题笔记。
2. 已有同主题笔记 → memory_edit / memory_edit_section 增量修改，绝不新建重复笔记。
3. 新建时标题 = 主题名（如"yacmemo部署配置"），禁止日期后缀和"-2"/"新"等尾巴
   （时间线流水放 journal/ 目录）。
写入时：
4. 写提炼后的结论，不贴对话原文；一篇笔记一个主题。
5. 事实行用 observation 语法：- [配置] 服务端口为 9721
6. 与其他笔记相关时写关系：- 部署于 [[debsvc服务器]]
检索时：
7. memory_search 结果带 ⚠ 标注时，先读两篇，用 memory_edit 合并，然后才回答用户。
8. 探索一个主题用 memory_read 的相关笔记链路，不要只凭单条搜索结果下结论。
```

不守约也有兜底：守卫拒绝 + force 两级确认 + audit 自愈（见 `04-consistency.md`）。

## 四、数据管理

- **git**：每个 memory_root 一个仓库（`.index/` 已 ignore）。服务端 `git init` + 定期 commit 即可；真正的事实历史在 git；
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
