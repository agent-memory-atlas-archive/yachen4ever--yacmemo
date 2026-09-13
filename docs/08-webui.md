# WebUI 控制台

> 服务端自带，浏览器打开 `http://<host>:9721/ui/` 即用。与 MCP 同进程同端口，无独立部署、无构建链。
> 代码位置：`yacmemo/webui/`（API：`app.py`；前端：`static/` 单页应用 + 本地化 marked.js v12）。

## 一、总体说明

- WebUI 与 MCP 端点共享同一 Starlette 应用和同一批用户上下文（Store/Searcher/IndexDB 实例）——**网页上看到和改到的就是 agent 在用的那份数据**，没有第二条数据路径；
- 路由顺序：`/api/*`、`/ui/*` 先于用户 MCP 挂载注册；配置层同时把 `api`/`ui`/`health` 设为用户 id 保留字，杜绝遮蔽；
- 错误约定：业务性拒绝（如守卫拦截）返回 HTTP 200 + `{"ok": false, "error": "..."}`；未知用户返回 404；
- 无独立鉴权，遵循"内网自用"信任边界（见 [05-deployment.md](05-deployment.md) 安全边界）。

## 二、页面导览

### 2.1 笔记

- **列表**：左侧列出当前用户全部笔记（标题 + 最近修改时间），顶部输入框按文件名实时过滤，`＋` 新建；
- **查看**：正文默认 markdown 渲染（marked.js），取消勾选"渲染"看纯文本；
- **编辑**：`编辑` 进入源码 textarea，`保存` 走 `store.save`——整篇覆盖，标题跟随首行 `# 标题`，索引同步重建（FTS/向量/冲突重算），`取消` 丢弃；
- **新建**：`＋` 输入主题名后进入编辑器。近似标题守卫在 WebUI 同样生效：被拒时提示已有笔记，勾选"强制(force)"再保存即可——**网页上点按钮的就是人工确认**，等价于 MCP 的 `force=true, force_confirm=true`，同样记入 guard_events；
- **删除**：需 confirm 确认；删除文件 + 清理全部索引（FTS/向量/冲突）。git 里仍可找回。

### 2.2 搜索

手动验证检索效果的页面。`hybrid`（默认）/`fts`/`vector` 三通道切换，结果含得分、通道来源与 ⚠ 撞车标注，点击任意结果跳转到笔记页。使用建议同 MCP：关键词式查询走 fts 更稳，自然语句靠 vector。

### 2.3 审计

人工触发的全量审计（等价于 `memory_audit`，含自愈行为）。另提供"全量重建索引"按钮（等价 `POST /api/{user}/reindex`，清空并重建 FTS/向量/撞车记录，笔记文件不受影响）。输出六类：

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
| 标题重复 | 归一化后近似标题的两篇 | 建议合并：点标题进编辑器手工合并 |
| 语义撞车 | 跨笔记的相似 observation 对（含双方文本与分数） | **人工裁决**：合并后点"已合并"，或点"忽略" |
| 悬空链接 | `[[目标]]` 不存在 | 创建目标或让 agent 清理链接 |

底部为守卫统计（refused / forced 次数）。

### 2.4 使用记录

每次 MCP 工具调用的留痕——**试用期最重要的观察页**：

- 顶部卡片：近 14 天调用数、错误数、客户端数；
- 表格字段：时间 / 用户 / 工具 / 内容摘要（写入的标题、搜索的词、编辑的路径）/ 客户端 UA / IP / 耗时；
- 可按工具过滤；`summary` 示例：`title=端口配置 force=False`、`query=服务端口`；
- 结果列：意外异常显示 ✗ 与错误消息；守卫拒绝算正常业务结果，不记为错误。

### 2.5 健康

- embedding 配置状态（未配置 = FTS-only 模式，语义检索与 D2 撞车检测关闭）；
- 各用户卡片：笔记数、open 撞车数、守卫统计（拒绝/force）；
- 客户端清单（UA + IP + 调用数 + 最近活跃）；
- 近 14 天每日调用量与错误数。

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
| POST | `/api/{user}/audit` | — | 运行审计（含自愈，会改动索引） |
| POST | `/api/{user}/collision` | `{id, status}` | 撞车裁决：`resolved` / `dismissed` |

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

## 五、实现说明

- 前端为无构建单页应用（原生 JS + marked.js v12，已本地化到 `static/marked.min.js`，MIT）；新增功能直接改 `static/app.js`；
- 后端 handler 为 async，Store 的阻塞操作经 `run_in_threadpool` 执行，不会阻塞 MCP 事件循环；跨线程安全由 IndexDB/VectorStore/Store 的实例锁保证；
- 静态资源经 `StaticFiles` 挂载在 `/ui/static`；`marked.min.js` 升级 = 替换该文件。

## 六、常见操作

- **看 agent 这两周干了什么**：使用记录页 → 按工具过滤 `memory_write`，summary 列就是写入标题清单；
- **裁决一条撞车**：审计页 → 读双方文本 → 若已合并点"已合并"；确认是误报点"忽略"；裁决前可点路径跳到笔记页核对；
- **排查"搜不到"**：搜索页切 `fts` / `vector` 分别试 → 健康页确认 embedding 是否配置 → 审计页看文件是否入索引；
- **手动改了文件**：审计页点一下运行，索引即对齐。
