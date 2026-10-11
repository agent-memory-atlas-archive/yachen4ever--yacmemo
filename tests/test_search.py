"""Search tests: FTS channel, RRF fusion, hybrid behavior, collision annotations."""

from __future__ import annotations

import pytest

from yacmemo.search import Searcher
from yacmemo.store import Store

NOTE_A = """# yacmemo部署配置

- [配置] 服务端口为 9721
- [配置] LLM 指向 m2ultra:11234
"""

NOTE_B = """# 备份策略

- [运维] 使用 restic 每日备份
"""


def test_fts_channel_hits_keyword(store: Store, searcher: Searcher):
    store.write("notes/yacmemo部署配置", NOTE_A)
    hits = searcher.fts_channel("服务端口", limit=10)
    assert [h["path"] for h in hits] == ["notes/yacmemo部署配置.md"]


def test_rrf_fusion_prefers_multi_channel_hits(cfg, db, emb, vectors):
    s = Searcher(cfg, db, emb, vectors)
    ch1 = [{"path": "a", "title": "A", "rank": 1, "channels": ["fts"]},
           {"path": "b", "title": "B", "rank": 2, "channels": ["fts"]}]
    ch2 = [{"path": "b", "title": "B", "rank": 1, "channels": ["vector"]},
           {"path": "c", "title": "C", "rank": 2, "channels": ["vector"]}]
    merged = s._rrf([ch1, ch2], limit=10)
    # b appears in both channels → top; then a (rank1 fts); then c
    assert [m["path"] for m in merged] == ["b", "a", "c"]
    assert merged[0]["channels"] == ["fts", "vector"]


def test_hybrid_top_hit(store: Store, searcher: Searcher):
    store.write("notes/yacmemo部署配置", NOTE_A)
    store.write("notes/备份策略", NOTE_B)
    results = searcher.search("端口配置", limit=5)
    assert results, "hybrid should not be empty"
    assert results[0]["title"] == "yacmemo部署配置"


def test_vector_only_channel(store: Store, searcher: Searcher):
    store.write("notes/备份策略", NOTE_B)
    results = searcher.search("restic 备份", limit=5, kind="vector")
    assert results[0]["title"] == "备份策略"


def test_search_results_carry_collision_warnings(store: Store, searcher: Searcher):
    """FakeEmbedding puts all 端口 observations in the same direction → D2 fires."""
    store.write("notes/yacmemo部署配置", NOTE_A)
    note_c = """# 端口配置说明

- [配置] 端口为 8080
"""
    store.write("notes/端口配置说明", note_c)

    results = searcher.search("端口配置", limit=5)
    warned = [r for r in results if r.get("warnings")]
    assert warned, "expected at least one result carrying a ⚠ collision warning"
    joined = "\n".join(w for r in warned for w in r["warnings"])
    assert "[[" in joined and "memory_edit" in joined


def test_search_kind_validation(store: Store, searcher: Searcher):
    with pytest.raises(ValueError, match="未知检索类型"):
        searcher.search("x", kind="bogus")


def test_short_query_like_fallback(store: Store, searcher: Searcher):
    """<3 字查询走 LIKE 子串回退——trigram 死区不再吞掉"端口"类高频短词。"""
    store.write("notes/yacmemo部署配置", NOTE_A)
    hits = searcher.fts_channel("端口", limit=10)
    assert [h["path"] for h in hits] == ["notes/yacmemo部署配置.md"]
    results = searcher.search("端口", limit=5)
    assert results and results[0]["title"] == "yacmemo部署配置"


def test_short_query_empty_result_carries_hint(store: Store, searcher: Searcher):
    """LIKE 兜底也没命中的短查询要给提示，不许静默返回空。"""
    store.write("notes/备份策略", NOTE_B)
    results = searcher.search("端口", limit=5, kind="fts")
    assert results == []
    assert searcher.last_notice and "3 字" in searcher.last_notice
    # LIKE 能命中的短查询无提示
    searcher.search("备份", limit=5, kind="fts")
    assert searcher.last_notice is None


def test_vector_outage_sets_degradation_notice(store: Store, searcher: Searcher):
    """向量通道故障时检索结果必须带降级提示（此前完全静默）。"""
    store.write("notes/备份策略", NOTE_B)

    class _BrokenEmb:
        def embed_one(self, text):
            raise ConnectionError("[Errno 111] Connection refused")

        def embed(self, texts):
            raise ConnectionError("[Errno 111] Connection refused")

    orig = searcher.emb
    searcher.emb = _BrokenEmb()
    try:
        results = searcher.search("restic", limit=5)
        assert results  # FTS 通道照常命中
        assert searcher.last_notice and "向量通道不可用" in searcher.last_notice
    finally:
        searcher.emb = orig
    # 恢复后提示清空
    searcher.search("restic", limit=5)
    assert searcher.last_notice is None


def test_search_archive_hidden_only_on_request(store: Store, searcher: Searcher):
    """0.3.17：归档笔记（archive/）默认仍在检索结果里（WebUI 视野不变），
    include_archived=False（MCP 工具层）时隐去并留提示。"""
    store.write("notes/live.md", "# live\n\n- [配置] 服务端口为 8080\n")
    store.write("notes/old.md", "# old\n\n- [配置] 服务端口为 9090\n")
    ar = store.note_archive("notes/old.md", reason="旧")

    default = searcher.search("服务端口", kind="fts")
    assert ar["to"] in [r["path"] for r in default]

    searcher.last_notice = None
    kept = searcher.search("服务端口", kind="fts", include_archived=False)
    assert [r["path"] for r in kept] == ["notes/live.md"]
    assert searcher.last_notice and "归档" in searcher.last_notice


def test_warnings_mask_invisible_and_archived(store: Store, searcher: Searcher):
    """0.3.18 审计 P1：⚠ warnings 携带对方标题+50 字原文——对方已归档或属
    其他专属区时必须降级为"存在疑似重复"，不外泄内容。"""
    from yacmemo.identity import Identity
    store.write("notes/主撞车.md", "# 主撞车\n\n- [撞车] 数据库口令轮换周期为 90 天并留痕\n")
    store.write("notes/副撞车.md", "# 副撞车\n\n- [撞车] 数据库口令轮换周期为 90 天并留痕\n")
    ar = store.note_archive("notes/副撞车.md", reason="旧")
    cols = [c for c in store.db.list_collisions() if c["status"] == "open"]
    assert cols, "归档前应检出撞车"
    hits = searcher.search("数据库口令", kind="fts", include_archived=False)
    target = next(r for r in hits if r["path"] == "notes/主撞车.md")
    joined = " ".join(target["warnings"])
    assert "副撞车" not in joined and "90 天" not in joined
    assert "疑似重复" in joined

    # identity：对方在别的专属区，warnings 不外泄
    store.write("notes/空调台账.md", "# 空调台账\n\n- [撞车] 服务器机房空调温度设定为 24 摄氏度\n")
    store.write("agents/bob/shared/机房.md", "# 机房\n\n- [撞车] 服务器机房空调温度设定为 24 摄氏度\n")
    hits2 = searcher.search("机房空调", kind="fts",
                            identity=Identity(agent="zcode", device="m5air"))
    t2 = next(r for r in hits2 if r["path"] == "notes/空调台账.md")
    j2 = " ".join(t2["warnings"])
    assert "机房.md" not in j2 and "24 摄氏度" not in j2
    assert "疑似重复" in j2
