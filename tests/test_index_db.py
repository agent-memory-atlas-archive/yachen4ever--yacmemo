"""IndexDB tests: notes metadata, FTS5 trigram (Chinese), collisions, guard events."""

from __future__ import annotations

import sqlite3

import pytest

from yacmemo.index_db import IndexDB

pytestmark = pytest.mark.skipif(
    tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2]) < (3, 34),
    reason="FTS5 trigram tokenizer requires SQLite >= 3.34",
)


def test_notes_upsert_get_remove(db: IndexDB):
    db.upsert_note("a.md", "笔记A", "h1")
    db.upsert_note("a.md", "笔记A改", "h2")  # upsert updates
    row = db.get_note("a.md")
    assert row["title"] == "笔记A改"
    assert row["content_hash"] == "h2"

    db.remove_note("a.md")
    assert db.get_note("a.md") is None


def test_get_note_by_title(db: IndexDB):
    db.upsert_note("projects/部署.md", "yacmemo部署配置", "h")
    assert db.get_note_by_title("yacmemo部署配置")["path"] == "projects/部署.md"
    assert db.get_note_by_title("不存在") is None


def test_fts_chinese_substring_search(db: IndexDB):
    db.fts_replace("a.md", "yacmemo部署配置", "服务端口为 9721，LLM 指向 m2ultra。")
    db.fts_replace("b.md", "备份策略", "使用 restic 每日备份数据目录。")

    # trigram phrase matching = literal substring (>= 3 chars)
    hits = db.fts_search("服务端口", limit=10)
    assert [h["path"] for h in hits] == ["a.md"]

    hits = db.fts_search("restic", limit=10)
    assert [h["path"] for h in hits] == ["b.md"]

    # multi-token AND; tokens < 3 chars are dropped ("部署")
    hits = db.fts_search("yacmemo 部署", limit=10)
    assert [h["path"] for h in hits] == ["a.md"]

    assert db.fts_search("完全不存在的词组") == []


def test_fts_short_query_returns_empty(db: IndexDB):
    """Trigram cannot match tokens < 3 chars; expr builder must bail out cleanly."""
    db.fts_replace("a.md", "端口配置", "服务端口为 9721")
    assert db.fts_search("端口", limit=10) == []
    assert db.fts_search("ab", limit=10) == []


def test_fts_remove_and_move(db: IndexDB):
    db.fts_replace("a.md", "标题A", "正文内容端口配置")
    db.fts_remove("a.md")
    assert db.fts_search("端口配置") == []

    db.fts_replace("a.md", "标题A", "正文内容端口配置")
    db.fts_move("a.md", "b.md")
    hits = db.fts_search("端口配置")
    assert [h["path"] for h in hits] == ["b.md"]


def test_guard_events_stats(db: IndexDB):
    db.add_guard_event("refused", "标题X", "x.md", forced=False)
    db.add_guard_event("refused", "标题Y", "y.md", forced=False)
    db.add_guard_event("forced", "标题Z", "z.md", forced=True)
    stats = db.guard_stats()
    assert stats == {"refused": 2, "forced": 1, "uncovered": 0}


def test_collisions_add_list_prune(db: IndexDB):
    db.upsert_note("a.md", "A", "h1")
    db.upsert_note("b.md", "B", "h2")
    db.add_collision("obs", "a.md", "b.md", a_text="端口为8080",
                     b_text="端口为9721", score=0.91)

    assert len(db.collisions_for("a.md")) == 1
    assert len(db.list_collisions(status="open")) == 1

    # prune drops collisions whose notes disappeared
    db.remove_note("b.md")
    assert db.prune_stale_collisions() == 1
    assert db.list_collisions(status="open") == []


def test_remove_collisions_involving(db: IndexDB):
    db.upsert_note("a.md", "A", "h")
    db.upsert_note("b.md", "B", "h")
    db.add_collision("obs", "a.md", "b.md", score=0.9)
    db.remove_collisions_involving("a.md")
    assert db.list_collisions(status="open") == []


def test_vec_cache_roundtrip(db: IndexDB):
    vec = [0.5] * 1024
    db.put_cached_vector("hash1", vec)
    got = db.get_cached_vector("hash1")
    assert got is not None
    assert len(got) == 1024
    assert abs(got[0] - 0.5) < 1e-6
    assert db.get_cached_vector("missing") is None


def test_clear_all_keeps_guard_events_and_cache(db: IndexDB):
    db.upsert_note("a.md", "A", "h")
    db.put_cached_vector("h", [0.1] * 1024)
    db.add_guard_event("forced", "T", "t.md", forced=True)

    db.clear_all()

    assert db.list_notes() == []
    assert db.guard_stats()["forced"] == 1
    assert db.get_cached_vector("h") is not None


def test_exec_events_timeline_and_last_status(db: IndexDB):
    """agent 执行时间线：追加式，新在前；exec_last_status 给每问题最后一条。"""
    db.add_exec_event("D3:x.md|[[g]]", "D3", "executing", "开始", "r9000x_teleagent")
    db.add_exec_event("D3:x.md|[[g]]", "D3", "progress", "改了一半", "r9000x_teleagent")
    db.add_exec_event("P:f.md:1", "P", "executed", "完成")

    tl = db.list_exec_events("D3:x.md|[[g]]")
    assert [e["event"] for e in tl] == ["progress", "executing"]
    assert tl[0]["identity"] == "r9000x_teleagent"

    last = db.exec_last_status()
    assert last["D3:x.md|[[g]]"]["event"] == "progress"
    assert last["D3:x.md|[[g]]"]["updates"] == 2
    assert last["P:f.md:1"]["event"] == "executed"
    assert len(db.list_exec_events()) == 3
