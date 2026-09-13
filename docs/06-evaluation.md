# 检索评测

> 工具：`scripts/eval_search.py`。这是一份**测量**，不是门禁——退出码恒为 0。

## 一、方法

固定 12 篇中文语料（家庭基建/项目/工作流主题，含 observation 行），10 条真实风格的中文查询，每条标注期望命中的笔记，测 **Recall@3** 分通道统计：

- **fts**：SQLite FTS5 trigram（关键词式查询的字面子串匹配）；
- **vector**：Qwen3-Embedding 最近笔记；
- **hybrid**：RRF（k=60）融合。

语料与查询写在脚本顶部（`CORPUS` / `QUERIES`），可按自己的真实主题替换——**用你自己的记忆做评测集才有意义**。

## 二、运行

```bash
# FTS-only（任何能跑 python 的地方）
uv run python scripts/eval_search.py

# 三通道（需能访问 omlx；模型 ID 必须与服务端 /v1/models 一致）
uv run python scripts/eval_search.py \
  --embedding-url http://192.168.5.2:11235/v1 \
  --embedding-model "Qwen3-Embedding-0.6B-4bit-DWQ" \
  --embedding-key sk-xxx \
  --keep          # 保留语料目录便于人工检查
```

端点不可达时 vector/hybrid 列自动标 `-` 跳过，FTS 列照常。结尾附 `audit` 输出（语料上的 D1/D2/D3 应全为 0）。

## 三、基线结果（2026-09-14，12 篇语料，m2ultra omlx 实测）

| query | expected | fts | vector | hybrid |
|---|---|---|---|---|
| yacmemo 端口 | yacmemo部署配置 | ✓ | ✓ | ✓ |
| 服务端口是多少 | yacmemo部署配置 | ✗ | ✗ | ✗ |
| debsvc IP 地址 | debsvc服务器 | ✓ | ✓ | ✓ |
| 数据库服务器的 IP 是什么 | debsvc服务器 | ✗ | ✓ | ✓ |
| restic | 备份策略 | ✓ | ✓ | ✓ |
| 怎么备份数据 | 备份策略 | ✗ | ✓ | ✓ |
| VLAN 划分 | 家庭网络拓扑 | ✓ | ✓ | ✓ |
| M2 Ultra 内存多大 | m2ultra推理服务器 | ✗ | ✓ | ✓ |
| agent 记忆接入 | 个人agent方案 | ✓ | ✓ | ✓ |
| Qwen3 embedding 部署 | Qwen3-Embedding部署 | ✓ | ✓ | ✓ |
| **Recall@3** | | **6/10** | **9/10** | **9/10** |

## 四、解读

1. **FTS 的 6 条命中全是关键词式查询，4 条落空全是自然语句**——trigram 短语匹配就是字面子串，"服务端口是多少"不是"服务端口为 9721"的子串。这是机制必然，不是 bug。
2. **向量通道把 4 条 FTS 落空全部救回**——混合检索的必要性有了直接数据。任何单通道方案（纯 FTS 或纯向量）都会在另一半查询类型上失败。
3. **唯一残余 miss**（"服务端口是多少"）是语义近邻竞争：语料中"Qwen3-Embedding部署"同样含"端口 11235"，期望笔记被挤出 top-3。缓解方向（P4 视数据决定）：查询改写（agent 检索时给关键词式 query）、提高 limit、或调 RRF 权重。
4. 语料上 audit 干净（title-dups=0, collisions=0, dangling=0）——守卫与检测器在无违约语料上无误报。

## 五、给 agent 的查询措辞约定

基线数据直接支持一条系统提示约定：**检索优先发关键词式 query**（"端口 9721"优于"端口是多少"）。已写入 `01-architecture.md` 第八节第 1 条的使用说明与 `02-mcp-tools.md` 的工具 docstring。

## 六、扩展

- 把 `CORPUS`/`QUERIES` 换成自己导出的真实笔记与真实问题，跑 `--keep` 人工核对；
- `kind="fts"/"vector"` 强制单通道定位问题（hybrid 差于单通道时优先查 RRF 实现）；
- P4 结束后重跑一次，对比真实记忆量（数百篇）下的指标衰减。
