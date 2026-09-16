---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'b3bc1025-3171-430c-b5fc-e71794a56d5d'
  PropagateID: 'b3bc1025-3171-430c-b5fc-e71794a56d5d'
  ReservedCode1: '6ed09f6a-6ba2-48b4-935e-13a41391e1da'
  ReservedCode2: '6ed09f6a-6ba2-48b4-935e-13a41391e1da'
---

# WebUI 控制台

> 服务端自带，浏览器打开 `http://<host>:9721/ui/` 即用。与 MCP 同进程同端口，无独立部署。
> 前端为 Vue 3 + Naive UI 工程（`frontend/`），需构建：`scripts/build_webui.sh` → 产物落 `yacmemo/webui/dist/`（`app.py` 直接 serve）。**未构建时服务照常启动**，`/ui/` 返回 503 构建指引，MCP/API 不受影响。

## 一、总体说明

- WebUI 与 MCP 端点共享同一 Starlette 应用和同一批用户上下文（Store/Searcher/IndexDB 实例）——**网页上看到和改到的就是 agent 在用的那份数据**，没有第二条数据路径；
- 路由顺序：`/api/*`、`/ui/*` 先于用户 MCP 挂载注册；配置层同时把 `api`/`ui`/`health` 设为用户 id 保留字，杜绝遮蔽；
- 错误约定：业务性拒绝（如守卫拦截）返回 HTTP 200 + `{"ok": false, "error": "..."}`；未知用户返回 404；
- 无独立鉴权，遵循"内网自用"信任边界（见 [05-deployment.md](05-deployment.md) 安全边界）。

## 二、页面导览

顶部选择器切换 memory 用户（config.toml 里的 `[[users]]`），左侧菜单五页：**主题 / 搜索 / 审计 / �画像 / 设置**。

### 2.1 主题浏览

左侧**主题树**：注册主题 → 主题内笔记（`abstract.md` 与 agent 增设的模块 md）逐层展开，已归档主题单独分组；右侧选中即看，合并了旧"笔记"页的全部能力：

- **查看**：正文默认 markdown 渲染，`编辑` 切到源码 textarea，`预览` 切回渲染；
- **编辑**：`保存` 走 `store.save`——整篇覆盖，标题跟随首行 `# 标题`，索引同步重建（FTS/向量/冲突重算）；
- **删除**：需 confirm 确认；删除文件 + 清理全部索引。git 里仍可找回。abstract.md 不可从 UI 删除；
- **新建笔记无 UI 入口**：新建走 MCP `memory_write`（守卫更严，防重复标题），或 Obsidian 手建后由审计自愈入索引。

### 2.2 搜索

手动验证检索效果的页面。`hybrid`（默认）/`fts`/`vector` 三通道切换，结果含得分、通道来源与 ⚠ 撞车标注。使用建议同 MCP：关键词式查询走 fts 更稳，自然语句靠 vector。

### 2.3 审计

审计页提供**两种审计引擎**，按需选择：

- **确定性审计**（默认按钮）：快速、零 LLM、含自愈——等价 `memory_audit`；另提供"全量重建索引"按钮（`POST /api/{user}/reindex`）；
- **深度审查**（curator）：把注册表、主题卡与审计结果交给 `[curator]` 配置的 LLM，产出提案报告（约 1-2 分钟）；报告落盘 `curator/提案-<日期>.md`；**同日重跑不新建文件**——结果以"复审（HH:MM）"小节追加进当天报告（标题保持每日唯一，不触发 D1 告警）。

审计页下方是**质量提案区**：历史报告按日期 chips 切换、markdown 渲染，裁决后建议在报告末尾追加执行记录。输出六类：

**语义撞车的裁决工作流**——撞车按"笔记对"分组（同一对笔记的多条匹配合并展示），每组提供「打开 A」「打开 B」跳转与两个裁决按钮：

1. 两篇是同一主题的两份拷贝 → 把有价值的内容并进保留篇、删除另一篇（笔记页删除按钮），点「✓ 已处理」；
2. 两篇主题不同、只是恰好都有这条事实 → 点「忽略」；
3. 一篇是另一篇的旧版本 → 新内容并入保留篇，点「✓ 已处理」。

> 撞车检测只对 `- [类别] 内容` 形态的 observation 行做；GFM 任务清单（`- [x]` / `- [ ]`）是勾选框不是 observation，不参与（见 [03-storage-and-search.md](03-storage-and-search.md)）。

| 输出 | 含义 | 处置 |
|---|---|---|
| 新发现文件 | 磁盘上有、从未入索引的 .md，已自动建索引 | 无需操作 |
| 外部修改 | 你在 Obsidian/vim 改过的，已自动重建索引 | 无需操作 |
| 外部删除 | 你直接删掉的，索引已清理 | 无需操作 |
| 标题重复 | 归一化后近似标题的两篇 | 人工合并后点「已处理」，或「忽略」 |
| 语义撞车 | 跨笔记的相似 observation 对（含双方文本与分数） | **人工裁决**：合并后点「已处理」或「忽略」（同步撞车状态） |
| 悬空链接 | `[[目标]]` 不存在 | 补齐/删除后「已处理」，非笔记引用则「忽略」 |
| 游离文件 | 未归入任何注册主题的散笔记 | 归位后点「已处理」，或「忽略」 |

> 免注册区（journal/archive/curator）不判游离。底部为守卫统计（refused / forced 次数）。

**历史审计快照**——每次点「确定性审计」自动落一份 markdown 快照到 `journal/audit/<时间戳>.md`（免注册区，自动 git 快照、可 Obsidian 打开）：记入概览计数、问题清单（每条带稳定 id）与处置记录。页面上方「历史审计快照」目录按时间倒序列出，点击任一条回看当时的问题与处理情况。

**处置记录**——对每条待处理问题（D1/D2/D3/D4）可点「已处理」或「忽略」：处置行（时间 + 动作 + 备注）自动追加进当次快照的《处置记录》节，随文件留存可追溯；已处置项不再出现在后续审计（D2 同步 index 撞车状态，其余写入持久化处置表），避免反复打扰。处置只记状态、绝不删除内容——合并/归位等实际操作仍需在笔记页/Obsidian 完成。

### 2.4 画像与偏好

PROFILE.md 的可视化编辑：左侧列出全部小节（身份 / 沟通风格 / 材料与文档偏好……），点击查看，`编辑` 后整段保存；`+ 新建小节` 输入小节名创建。等价 MCP 的 `get_user_preference` / `update_user_preference`，同样走写路径（自动 git 快照）。

### 2.5 设置

三块合一页：

- **使用记录**：每次 MCP 工具调用的留痕——顶部卡片（近 14 天调用/错误/客户端数）+ 表格（时间/用户/工具/摘要/客户端 UA/IP/耗时），可按工具过滤；守卫拒绝算正常业务结果不记错误；
- **健康总览**：embedding 配置状态（未配置 = FTS-only 模式）、各用户笔记数/open 撞车数/守卫统计/主题数；
- **config.toml 在线编辑**：保存前自动做 TOML 语法 + 结构校验（非法配置直接拒绝）、备份原文件为 `config.toml.bak-<时间戳>`、保持 600 权限；可选"保存并重启服务"（systemd 重启，约 3 秒离线）。删除用户属危险操作，不提供按钮，请 SSH 手工处理。

## 三、API 参考

所有响应为 JSON，业务失败返回 `{"ok": false, "error": "..."}`（HTTP 200），未知用户 404。

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `/api/overview` | — | 用户列表（笔记数/撞车数/守卫统计）+ embedding 状态 + 今日调用数 |
| GET | `/api/usage` | `limit` `user` `tool` | 调用日志（默认 100 条，按时间倒序） |
| GET | `/api/usage/clients` | — | 客户端汇总（UA + IP + 调用数 + 最近活跃） |
| GET | `/api/usage/days` | — | 近 14 天每日调用/错误数 |
| GET | `/api/{user}/notes` | `path` `sort` | 笔记列表（含 mtime/size） |
| POST | `/api/{user}/notes` | `{title, content, force, force_confirm}` | 新建（走写路径守卫） |
| GET | `/api/{user}/note` | `path` | 读取单篇（path 或标题） |
| PUT | `/api/{user}/note` | `{path, content}` | 整篇保存（标题随首行标题） |
| DELETE | `/api/{user}/note` | `path` | 删除（文件 + 全部索引） |
| GET | `/api/{user}/search` | `q` `limit` `kind` | 检索（含 ⚠ warnings） |
| POST | `/api/{user}/audit` | — | 运行审计（含自愈，会改动索引）；返回 `audit_file`（本次快照路径） |
| GET | `/api/{user}/audit/runs` | — | 历史审计快照列表（journal/audit/*.md，时间倒序） |
| POST | `/api/{user}/audit/action` | `{file, id, action, label, note?}` | 记录处置（追加快照处置记录；D2 同步撞车状态） |
| POST | `/api/{user}/collision` | `{id, status}` | 撞车裁决：`resolved` / `dismissed` |
| POST | `/api/{user}/curator` | — | 触发深度审查（同步等待，约 1-2 分钟），返回报告 markdown |
| GET | `/api/{user}/proposals` | — | 列出 curator 提案报告 |
| GET | `/api/config` | — | 读取 config.toml 原文（含密钥，仅内网管理用途） |
| POST | `/api/config` | `{content, restart?}` | 校验（TOML + load_config）→ 备份 → 保存；`restart=true` 时延迟重启服务 |

`{user}` 为 config.toml 中的用户 id。脚本化示例：

```bash
curl -s http://debsvc.local:9721/api/yachen/search?q=端口 | python -m json.tool
curl -s -X POST http://debsvc.local:9721/api/yachen/audit
```

## 四、使用日志（usage.db）

- 位置：`[server].data_dir/usage.db`（默认 `data/usage.db`，相对服务启动目录）；
- 表 `call_log`：`id / ts / user_id / client / ip / tool / summary / duration_ms / ok / error`；
- **滚动保留最近 2 万条**，无需维护；
- 写入方：`tools.py` 在每次 MCP 工具调用后记录（stdio 调用 client 记为 `stdio`；HTTP 调用取请求 UA 与远端 IP）；
- `ok` 语义：意外异常 = 0；守卫拒绝等业务结果 = 1（守卫行为另有 `guard_events` 表可查）。

## 五、构建与实现说明

- **前端工程**：Vue 3 + Naive UI + Vite（`frontend/`），源码 `src/App.vue` + `src/components/`（五页组件）+ `src/composables/api.js`（统一 fetch 封装）；
- **构建**：`scripts/build_webui.sh`（npm ci + vite build）→ 产物落 `yacmemo/webui/dist/`，与 `app.py` 的 `STATIC_DIR` 一致；**注意** vite outDir 用 `new URL('../../yacmemo/webui/dist', import.meta.url)`，相对 vite.config.js 解析，别改成 `../webui/dist`（那是仓库根，服务读不到）；
- **分包**：manualChunks 把 `vue` 与 `naive-ui` 各自成 chunk——业务代码迭代不会使大依赖缓存失效；
- **开发模式**：`cd frontend && npm run dev`（Vite dev server 端口 5173，`/api` 代理到本机 9721）；
- **未构建行为**：`dist/` 不存在时服务正常启动，`/ui/` 返回 503 + 构建指引（PlainTextResponse），MCP/API 全功能可用；
- **服务器无 npm**：在开发机构建后 scp：`scp -r yacmemo/webui/dist <server>:/srv/yacmemo/yacmemo/webui/`（dist 不进 git）；
- 后端 handler 为 async，Store 的阻塞操作经 `run_in_threadpool` 执行，不会阻塞 MCP 事件循环；跨线程安全由 IndexDB/VectorStore/Store 的实例锁保证；
- 静态资源仅挂载 `/ui/assets`（Vite 产物）；`/ui/` 由 handler 直接回 `index.html`。

## 六、常见操作

- **看 agent 这两周干了什么**：设置页 → 使用记录区按工具过滤 `memory_write`，summary 列就是写入标题清单；
- **裁决一条撞车**：审计页 → 读双方文本 → 若已合并点"已处理"；确认是误报点"忽略"；裁决前可点路径跳到主题页核对；
- **排查"搜不到"**：搜索页切 `fts` / `vector` 分别试 → 设置页健康总览确认 embedding 是否配置 → 审计页看文件是否入索引；
- **手动改了文件**：审计页点一下运行，索引即对齐；
- **升级前端依赖/改组件**：开发机 `scripts/build_webui.sh` → scp dist → 刷新即生效（无需重启服务）。