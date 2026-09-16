"""P2 tests: edit_section, force confirmation ladder, audit self-healing."""

from __future__ import annotations

import pytest

from yacmemo.config import GuardConfig
from yacmemo.store import Store, StoreError

SECTION_NOTE = """# 配置

开头介绍。

## 网络

旧网络内容A
旧网络内容B

## 磁盘

磁盘内容
"""


def test_edit_section_replaces_body_keeps_heading(store: Store):
    store.write("配置", SECTION_NOTE)
    store.edit_section("配置", "网络", "新网络内容")

    text = (store.root / "配置.md").read_text(encoding="utf-8")
    assert "## 网络" in text
    assert "新网络内容" in text
    assert "旧网络内容A" not in text
    assert "## 磁盘" in text and "磁盘内容" in text
    # index follows
    assert store.db.fts_search("新网络内容")
    assert store.db.fts_search("旧网络内容A") == []


def test_edit_section_last_section_until_eof(store: Store):
    store.write("配置", SECTION_NOTE)
    store.edit_section("配置", "磁盘", "全新磁盘内容")
    text = (store.root / "配置.md").read_text(encoding="utf-8")
    assert "全新磁盘内容" in text and "磁盘内容\n" not in text.replace("全新磁盘内容\n", "")
    assert "## 网络" in text  # earlier section untouched


def test_edit_section_heading_not_found_lists_available(store: Store):
    store.write("配置", SECTION_NOTE)
    with pytest.raises(StoreError) as e:
        store.edit_section("配置", "不存在的节", "x")
    assert "网络" in str(e.value) and "磁盘" in str(e.value)


def test_edit_section_duplicate_heading_refused(store: Store):
    store.write("配置", "# 配置\n\n## 网络\nA\n\n## 网络\nB\n")
    with pytest.raises(StoreError, match="2 处"):
        store.edit_section("配置", "网络", "x")


def test_edit_section_ignores_h1_title(store: Store):
    store.write("配置", SECTION_NOTE)
    with pytest.raises(StoreError, match="memory_edit"):
        store.edit_section("配置", "配置", "x")


@pytest.fixture
def tight_store(cfg, db, emb, vectors) -> Store:
    """Store with force_confirm_threshold=2 to exercise the ladder quickly."""
    cfg.guard = GuardConfig(force_confirm_threshold=2)
    return Store(cfg, db, emb, vectors)


def test_force_confirmation_ladder(tight_store: Store):
    tight_store.write("主题A", "# 主题A\n内容A")
    # 1st and 2nd forced bypass: under threshold, plain force works
    tight_store.write("主题A-2", "# 主题A-2\nx", force=True)
    tight_store.write("主题A-3", "# 主题A-3\nx", force=True)
    # 3rd: threshold reached → bare force refused
    with pytest.raises(StoreError, match="人工确认"):
        tight_store.write("主题A-4", "# 主题A-4\nx", force=True)
    # with explicit confirmation → allowed
    r = tight_store.write("主题A-4", "# 主题A-4\nx", force=True, force_confirm=True)
    assert r["forced"] is True
    assert tight_store.db.guard_stats()["forced"] == 3


def test_no_conflict_needs_no_force_or_confirm(store: Store):
    r = store.write("独立主题", "# 独立主题\n内容")
    assert r["forced"] is False


def test_audit_resyncs_externally_edited_note(store: Store):
    store.write("yacmemo部署配置", "# yacmemo部署配置\n\n- [配置] 服务端口为 9721\n")
    store.write("端口配置说明", "# 端口配置说明\n\n- [配置] 端口为 8080\n")
    # FakeEmbedding: both 端口 observations collide → one open collision
    assert len(store.db.list_collisions(status="open")) == 1

    # out-of-band edit: the 端口 observation is gone
    p = store.root / "yacmemo部署配置.md"
    p.write_text("# yacmemo部署配置\n\n- [运维] 改用每日备份\n", encoding="utf-8")

    r = store.audit()
    assert r["resynced"] == ["yacmemo部署配置.md"]
    # collision recomputed from the edited side and no longer fires
    assert store.db.list_collisions(status="open") == []
    # fts reflects the external content
    assert store.db.fts_search("每日备份")
    assert store.db.fts_search("9721") == []


def test_audit_reports_and_prunes_externally_deleted_note(store: Store):
    store.write("yacmemo部署配置", "# yacmemo部署配置\n\n- [配置] 端口 9721\n")
    (store.root / "yacmemo部署配置.md").unlink()

    r = store.audit()
    assert r["missing"] == ["yacmemo部署配置.md"]
    assert store.db.get_note("yacmemo部署配置.md") is None
    assert store.db.fts_search("端口 9721") == []


def test_audit_clean_when_no_external_changes(store: Store):
    store.write("yacmemo部署配置", "# yacmemo部署配置\n内容")
    r = store.audit()
    assert r["resynced"] == [] and r["missing"] == []


def test_machine_zones_do_not_embed_observations(store: Store):
    """journal/audit/ 与 curator/ 是机器产物区：'- [时间] 处置行'会被
    parse_observations 当作伪 observation（类别=时间戳），但不得入 obs 空间。"""
    store.save("journal/audit/20260916-120000.md",
               "# 审计快照 20260916-120000\n\n- [2026-09-16 12:00] 已处理 D1:a|b\n")
    assert store.emb.calls == 1  # 仅 note 级一条；处置行未产生 embedding

    r = store.audit()
    assert r["collisions"] == []  # 审计自身落盘的快照不产生任何撞车


def test_machine_zone_pseudo_observations_never_d2(store: Store):
    """同 bucket 伪 observation 若入 obs 空间必撞（FakeEmbedding cosine=1.0，
    真 embedding 下处置行跨快照同理）；普通笔记的对照撞车照常检出。"""
    store.save("journal/audit/20260916-120000.md",
               "# 审计快照 A\n\n- [2026-09-16 12:00] 已处理 端口 相关 issue\n")
    store.save("journal/audit/20260916-130000.md",
               "# 审计快照 B\n\n- [2026-09-16 13:00] 已处理 端口 相关 issue\n")
    store.save("curator/提案-2026-09-16.md",
               "# 记忆质量提案\n\n- [2026-09-16 14:00] 已采纳 端口 相关提案\n")
    assert store.db.list_collisions(status="open") == []

    # 对照：正常笔记的同 bucket observation 照常检出
    store.write("yacmemo部署配置", "# yacmemo部署配置\n\n- [配置] 服务端口为 9721\n")
    store.write("端口配置说明", "# 端口配置说明\n\n- [配置] 端口为 8080\n")
    assert len(store.db.list_collisions(status="open")) == 1


def test_machine_zone_titles_never_d1(store: Store):
    """curator 报告归一化剥日期后标题同构（"提案-0916"与"提案-0917"都归一为
    "提案"），机器产物区不得进入 D1 候选。"""
    store.save("curator/提案-2026-09-16.md", "# 记忆质量提案（yachen，2026-09-16）\n\n提案内容\n")
    store.save("curator/提案-2026-09-17.md", "# 记忆质量提案（yachen，2026-09-17）\n\n提案内容二\n")
    r = store.audit()
    assert r["title_duplicates"] == []
