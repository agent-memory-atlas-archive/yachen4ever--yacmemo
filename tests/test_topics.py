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
    assert r["card"] == "topics/女儿教育/abstract.md"
    assert (tstore.root / r["card"]).is_file()
    topics = tstore.load_topics()
    assert [t["title"] for t in topics] == ["笔记主题", "女儿教育"]
    # new abstract is searchable
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


def test_topic_unregister_removes_block_keeps_header(tstore: Store):
    header_head = tstore.topics_file().read_text(encoding="utf-8").splitlines()[0]
    r = tstore.topic_unregister("笔记主题")
    assert r["card"] == "notes/a.md"
    text = tstore.topics_file().read_text(encoding="utf-8")
    assert "笔记主题" not in text
    assert text.splitlines()[0] == header_head  # header preserved
    assert tstore.load_topics() == []
    # notes untouched
    assert (tstore.root / "notes" / "a.md").is_file()
    # former related file becomes stray
    assert tstore.audit()["stray"] == ["notes/a.md", "notes/b.md"]


def test_topic_unregister_unknown_title_lists_existing(tstore: Store):
    with pytest.raises(StoreError) as e:
        tstore.topic_unregister("不存在的主题")
    assert "笔记主题" in str(e.value)


def test_re_register_after_unregister(tstore: Store):
    tstore.topic_unregister("笔记主题")
    r = tstore.topic_register("笔记主题", description="重新注册")
    assert r["card"] == "topics/笔记主题/abstract.md"
    assert len(tstore.load_topics()) == 1


def test_archive_topic_full_flow(tstore: Store):
    """archive: abstract moves to archive/<topic>/, registry gets 状态 line,
    topic stays searchable, out of context and stray detection."""
    tstore.topic_register("旧项目", description="已被替代的老项目")
    r = tstore.archive_topic("旧项目")
    assert r["archived"] is True
    assert r["card"] == "archive/旧项目/abstract.md"
    assert (tstore.root / r["card"]).is_file()
    assert not (tstore.root / "topics/旧项目/abstract.md").exists()

    topics = tstore.load_topics()
    assert [t["archived"] for t in topics] == [False, True]  # 笔记主题 active, 旧项目 archived

    # archived abstract is in a free zone -> never stray
    assert tstore.audit()["stray"] == []
    # still searchable
    assert tstore.db.fts_search("已被替代的老项目")
    # memory_context excludes archived topics' abstracts (registry full text
    # stays — only the abstract digest section is filtered)
    ctx = tstore.memory_context()
    assert "### 旧项目" not in ctx
    assert "### 笔记主题" in ctx or "内容A" in ctx  # active topic still present


def test_archive_topic_registry_block_format(tstore: Store):
    """The 状态: archived line lands after the registry fields."""
    tstore.topic_register("旧项目", description="x")
    tstore.archive_topic("旧项目")
    text = tstore.topics_file().read_text(encoding="utf-8")
    block = text.split("## 旧项目", 1)[1].split("## ")[0]
    assert "- 状态: archived" in block
    assert block.index("- 注册:") < block.index("- 状态: archived")


def test_archive_topic_unknown_or_already_archived(tstore: Store):
    with pytest.raises(StoreError, match="没有活跃主题"):
        tstore.archive_topic("不存在")
    tstore.topic_register("旧项目", description="x")
    tstore.archive_topic("旧项目")
    with pytest.raises(StoreError, match="没有活跃主题"):
        tstore.archive_topic("旧项目")  # twice -> not active anymore


def test_archive_moves_whole_dir_and_rewrites_card(store: Store):
    """归档必须整个主题目录一起走（目录即归属），且注册表卡路径同步改写
    ——2026-09-17 实爆：只移 abstract + 不改卡行 → D5 每次点名、其余模块
    笔记在卡修正后变游离。"""
    store.topic_register("归档测试", description="用于归档验证")
    store.write("topics/归档测试/模块笔记", "# 归档测试/模块笔记\n- [事实] 模块内容\n")

    r = store.archive_topic("归档测试")
    assert r["card"] == "archive/归档测试/abstract.md"
    assert (store.root / "archive/归档测试/abstract.md").is_file()
    assert (store.root / "archive/归档测试/模块笔记.md").is_file()
    assert not (store.root / "topics/归档测试").exists()

    reg = (store.root / "TOPICS.md").read_text(encoding="utf-8")
    assert "- 卡: archive/归档测试/abstract.md" in reg
    assert "- 状态: archived" in reg

    # 检索仍可用、无游离、无悬空卡
    assert store.db.fts_search("模块内容")
    a = store.audit()
    assert a["stray"] == []
    assert a["dangling_cards"] == []
    # context 不再注入 abstract 摘要头（注册表条目本身仍全量可见）
    assert "### 归档测试（" not in store.memory_context()
