"""Topic registry tests: TOPICS.md parse/register/context + stray detection."""

from __future__ import annotations

import pytest

from yacmemo.store import Store, StoreError


def test_load_topics_parses_registry(tstore: Store):
    topics = tstore.load_topics()
    assert len(topics) == 1
    t = topics[0]
    assert t["title"] == "笔记主题"
    assert t["card"] == "notes/a.md"
    assert t["related"] == ["notes/b.md"]
    assert t["registered"] == "2026-09-14"


def test_topic_register_appends_and_indexes(tstore: Store):
    r = tstore.topic_register("女儿教育", description="启蒙阶段记录",
                              related="notes/b.md")
    assert r["card"] == "topics/女儿教育/主题卡.md"
    assert (tstore.root / r["card"]).is_file()
    topics = tstore.load_topics()
    assert [t["title"] for t in topics] == ["笔记主题", "女儿教育"]
    # new card is searchable
    assert tstore.db.fts_search("启蒙阶段记录")


def test_topic_register_refuses_duplicate_title(tstore: Store):
    with pytest.raises(StoreError, match="已存在"):
        tstore.topic_register("笔记主题")


def test_topic_register_with_existing_card(tstore: Store):
    (tstore.root / "notes" / "c.md").write_text("# c\nC\n", encoding="utf-8")
    r = tstore.topic_register("笔记主题二", card_path="notes/c.md")
    assert r["card"] == "notes/c.md"


def test_memory_context_compiles_registry_and_cards(tstore: Store):
    ctx = tstore.memory_context()
    assert "主题记忆注册表" in ctx
    assert "## 笔记主题" in ctx
    assert "内容A" in ctx  # card excerpt included


def test_audit_stray_detection(tstore: Store):
    # notes/b.md is covered via 相关; a.md is the card
    assert tstore._stray_files(tstore.load_topics()) == []
    # an uncovered file appears as stray
    (tstore.root / "散文件.md").write_text("# 散文件\nx\n", encoding="utf-8")
    assert tstore._stray_files(tstore.load_topics()) == ["散文件.md"]
    # free zones are exempt
    (tstore.root / "journal" / "2026-09-14-流水.md").parent.mkdir(
        parents=True, exist_ok=True)
    (tstore.root / "journal" / "2026-09-14-流水.md").write_text(
        "# 流水\n", encoding="utf-8")
    assert "journal/2026-09-14-流水.md" not in tstore._stray_files(tstore.load_topics())
    # audit() must expose stray (regression: the field was once silently missing)
    assert tstore.audit()["stray"] == ["散文件.md"]
