"""Search tests: FTS channel, RRF fusion, hybrid behavior, collision annotations."""

from __future__ import annotations

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
    try:
        searcher.search("x", kind="bogus")
        raise AssertionError("should reject unknown kind")
    except ValueError:
        pass
