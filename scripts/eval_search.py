"""P0 acceptance: Chinese retrieval baseline on a fixed sample corpus (docs/06 §5.2, §11-P0).

Builds a fixed 12-note Chinese corpus, then measures top-3 recall of 10 real-style
queries per channel (fts / vector / hybrid).

Usage:
    uv run python scripts/eval_search.py                 # FTS-only (offline dev box)
    uv run python scripts/eval_search.py \
        --embedding-url http://m2ultra:11235/v1 \
        --embedding-model <model> [--embedding-key sk-...]   # full 3-channel baseline

Run this on the box that can reach omlx for the full baseline; the FTS column is
meaningful anywhere. Exit code is always 0 — this is a measurement, not a gate.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import tempfile
from pathlib import Path

from yacmemo.config import Config, EmbeddingConfig, GuardConfig, MemoryConfig, SearchConfig
from yacmemo.embedding import EmbeddingClient
from yacmemo.index_db import IndexDB
from yacmemo.search import Searcher
from yacmemo.store import Store
from yacmemo.vector import VectorStore

CORPUS: dict[str, str] = {
    "yacmemo部署配置": """# yacmemo部署配置

yacmemo 记忆服务部署在 debsvc 上，systemd 托管。

- [配置] 服务端口为 9721
- [配置] embedding 指向 m2ultra:11235
""",
    "debsvc服务器": """# debsvc服务器

内网 Linux 主机，跑 yacmemo 与 teleagent。

- [主机] IP 192.168.5.7
- [主机] Debian 12，16GB 内存
""",
    "Qwen3-Embedding部署": """# Qwen3-Embedding部署

omlx 提供 OpenAI 兼容 embedding 服务。

- [配置] 端口 11235
- [配置] 模型 Qwen3-Embedding-0.6B，1024 维
""",
    "备份策略": """# 备份策略

家庭数据用 restic 快照，保留 30 天。

- [运维] 每日凌晨备份到 NAS 的 backup 共享
""",
    "m2ultra推理服务器": """# m2ultra推理服务器

- [主机] Apple M2 Ultra，192GB 统一内存
- [配置] mlx-serve 端口 11234，跑 Qwen3.8 Flash Next
""",
    "个人agent方案": """# 个人agent方案

自研 agent runtime，通过 MCP 接入工具。

- [设计] 记忆接入走 yacmemo MCP server（stdio）
- [设计] 主模型 Qwen3.8 Flash Next
""",
    "SQLite调优笔记": """# SQLite调优笔记

- [经验] 开 WAL 提升并发读写
- [经验] FTS5 用 trigram 分词支持中文子串
""",
    "多用户设置": """# 多用户设置

- [配置] 用户2账号单独一个 memory 目录
- [配置] 每个 MCP 实例绑定自己的 --root
""",
    "家庭网络拓扑": """# 家庭网络拓扑

- [网络] 主路由下划三个 VLAN：办公 / IoT / 访客
- [网络] esxi 宿主机 192.168.5.3
""",
    "Obsidian工作流": """# Obsidian工作流

- [工具] 用 Obsidian 打开 memory 目录浏览笔记
- [工具] wiki-link 图谱可视化关联
""",
    "证书续期流程": """# 证书续期流程

- [运维] acme.sh 自动续期 Let's Encrypt 证书
- [运维] 续期后 reload nginx
""",
    "相册同步方案": """# 相册同步方案

- [方案] 手机照片经 Immich 同步到 NAS
- [方案] ML 索引跑在 m2ultra 上
""",
}

# (query, expected_note_title)
QUERIES: list[tuple[str, str]] = [
    ("yacmemo 端口", "yacmemo部署配置"),            # keyword-style → fts should hit
    ("服务端口是多少", "yacmemo部署配置"),            # natural phrasing → fts miss expected
    ("debsvc IP 地址", "debsvc服务器"),               # keyword-style
    ("数据库服务器的 IP 是什么", "debsvc服务器"),      # natural phrasing
    ("restic", "备份策略"),                            # exact keyword
    ("怎么备份数据", "备份策略"),                      # natural phrasing
    ("VLAN 划分", "家庭网络拓扑"),                     # keyword-style
    ("M2 Ultra 内存多大", "m2ultra推理服务器"),        # natural phrasing
    ("agent 记忆接入", "个人agent方案"),             # mixed
    ("Qwen3 embedding 部署", "Qwen3-Embedding部署"),   # mixed
]


def build(root: Path, emb: EmbeddingClient | None, seed: bool = True):
    config = Config(
        memory=MemoryConfig(root=str(root)),
        embedding=EmbeddingConfig(),
        search=SearchConfig(),
        guard=GuardConfig(),
    )
    db = IndexDB(config.sqlite_path)
    vectors = VectorStore(config.lancedb_path, 1024) if emb else None
    store = Store(config, db, emb, vectors)
    # 0.1.1 起写入受主题注册制约束：语料登记为评测主题，写入其目录
    store.topic_register("检索评测语料", description="搜索基线固定语料（eval_search）")
    if seed:
        for title, content in CORPUS.items():
            store.write(f"topics/检索评测语料/{title}", content)
    searcher = Searcher(config, db, emb, vectors)
    return store, searcher


def probe_embedding(args) -> EmbeddingClient | None:
    if not args.embedding_url:
        return None
    emb = EmbeddingClient(
        base_url=args.embedding_url, api_key=args.embedding_key or "",
        model=args.embedding_model or "", dimensions=1024, timeout=10,
    )
    try:
        emb.embed_one("连通性测试")
    except Exception as e:
        print(f"[warn] embedding endpoint unreachable ({e}) — vector/hybrid skipped")
        return None
    return emb


def recall_at_k(results: list[dict], expected_title: str, k: int = 3) -> bool:
    return any(r["title"] == expected_title for r in results[:k])


def negative_cases(store: Store, searcher: Searcher,
                   emb: EmbeddingClient | None) -> list[tuple[str, bool]]:
    """负向检索断言（docs/06 §七，2026-09-19 增补；atlas rubric #7）：
    召回基线只测"该回来的要回来"，这里测"不该回来的不得回来"。"""
    cases: list[tuple[str, bool]] = []
    topic_dir = "topics/检索评测语料"

    # N1 已删除笔记不得被任何通道召回
    store.write(f"{topic_dir}/临时口令记录",
                "# 临时口令记录\n\n- [配置] 临时口令 hunter2\n")
    store.delete_note("临时口令记录")
    kinds = ["fts", "hybrid"] + (["vector"] if emb else [])
    for kind in kinds:
        hits = [r["title"] for r in
                searcher.search("hunter2 临时口令", limit=10, kind=kind)]
        cases.append((f"已删除笔记不得被召回 [{kind}]",
                      "临时口令记录" not in hits))

    # N2 已人工处置（dismissed）的 D2 冲突对不得再出 ⚠ 警告
    # （D2 依赖向量通道，FTS-only 运行跳过）
    if emb:
        a, b = "Kong部署记录", "API网关决策"
        store.write(f"{topic_dir}/{a}", f"# {a}\n\n- [决策] 网关用 Kong\n")
        store.write(f"{topic_dir}/{b}", f"# {b}\n\n- [决策] 网关用 Kong\n")
        q = "网关 Kong 决策"
        pair_paths = {f"{topic_dir}/{a}.md", f"{topic_dir}/{b}.md"}

        def _warned():
            return any(r.get("warnings") for r in
                       searcher.search(q, limit=10, kind="hybrid")
                       if r["title"] in (a, b))

        warned_before = _warned()
        for c in store.db.list_collisions(status="open"):
            if {c["a_path"], c["b_path"]} <= pair_paths:
                store.db.resolve_collision(c["id"], "dismissed")
        cases.append(("冲突对先警告、处置后不再警告 [hybrid]",
                      warned_before and not _warned()))

    # N3 跨用户隔离：独立第二个 store（空语料）不得召回本库内容
    root2 = Path(tempfile.mkdtemp(prefix="yacmemo-eval-u2-"))
    try:
        _, searcher2 = build(root2, emb, seed=False)
        hits2 = [r["title"] for r in
                 searcher2.search("yacmemo 端口 9721", limit=10, kind="fts")]
        cases.append(("跨用户不得串库 [fts]",
                      "yacmemo部署配置" not in hits2))
    finally:
        shutil.rmtree(root2, ignore_errors=True)

    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--embedding-url", default=None)
    parser.add_argument("--embedding-model", default=None)
    parser.add_argument("--embedding-key", default=None)
    parser.add_argument("--keep", action="store_true", help="keep the corpus dir")
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="yacmemo-eval-"))
    emb = probe_embedding(args)
    store, searcher = build(root, emb)

    ver = sqlite3.sqlite_version
    print(f"SQLite {ver} | corpus: {len(CORPUS)} notes | "
          f"embedding: {'available' if emb else 'unavailable (FTS-only run)'}")
    print(f"corpus dir: {root}")
    print()

    header = f"{'query':<28} {'expected':<22} {'fts':^5} {'vector':^7} {'hybrid':^7}"
    print(header)
    print("-" * len(header))

    totals = {"fts": [0, 0], "vector": [0, 0], "hybrid": [0, 0]}
    for query, expected in QUERIES:
        marks = {}
        for kind in ("fts", "vector", "hybrid"):
            if kind == "vector" and emb is None:
                marks[kind] = "-"
                continue
            results = searcher.search(query, limit=5, kind=kind)
            ok = recall_at_k(results, expected)
            totals[kind][1] += 1
            totals[kind][0] += int(ok)
            marks[kind] = "✓" if ok else "✗"
        print(f"{query:<28} {expected:<22} {marks['fts']:^5} "
              f"{marks['vector']:^7} {marks['hybrid']:^7}")

    print("-" * len(header))
    for kind in ("fts", "vector", "hybrid"):
        hit, total = totals[kind]
        note = " (skipped)" if total == 0 else ""
        print(f"Recall@3 [{kind:>6}]: {hit}/{total}{note}")

    print("\nNegative assertions（不该召回的不得召回 / 处置过的不再警告）:")
    for name, ok in negative_cases(store, searcher, emb):
        print(f"  {'✓' if ok else '✗'} {name}")

    audit = store.audit()
    print(f"\naudit: title-dups={len(audit['title_duplicates'])} "
          f"collisions={len(audit['collisions'])} "
          f"dangling={len(audit['dangling_links'])}")

    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    else:
        print(f"corpus kept at: {root}")


if __name__ == "__main__":
    main()
