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
    store.write("notes/配置", SECTION_NOTE)
    store.edit_section("配置", "网络", "新网络内容")

    text = (store.root / "notes/配置.md").read_text(encoding="utf-8")
    assert "## 网络" in text
    assert "新网络内容" in text
    assert "旧网络内容A" not in text
    assert "## 磁盘" in text and "磁盘内容" in text
    # index follows
    assert store.db.fts_search("新网络内容")
    assert store.db.fts_search("旧网络内容A") == []


def test_edit_section_last_section_until_eof(store: Store):
    store.write("notes/配置", SECTION_NOTE)
    store.edit_section("配置", "磁盘", "全新磁盘内容")
    text = (store.root / "notes/配置.md").read_text(encoding="utf-8")
    assert "全新磁盘内容" in text and "磁盘内容\n" not in text.replace("全新磁盘内容\n", "")
    assert "## 网络" in text  # earlier section untouched


def test_edit_section_heading_not_found_lists_available(store: Store):
    store.write("notes/配置", SECTION_NOTE)
    with pytest.raises(StoreError) as e:
        store.edit_section("配置", "不存在的节", "x")
    assert "网络" in str(e.value) and "磁盘" in str(e.value)


def test_edit_section_duplicate_heading_refused(store: Store):
    store.write("notes/配置", "# 配置\n\n## 网络\nA\n\n## 网络\nB\n")
    with pytest.raises(StoreError, match="2 处"):
        store.edit_section("配置", "网络", "x")


def test_edit_section_ignores_h1_title(store: Store):
    store.write("notes/配置", SECTION_NOTE)
    with pytest.raises(StoreError, match="memory_edit"):
        store.edit_section("配置", "配置", "x")


@pytest.fixture
def tight_store(store: Store) -> Store:
    """种子已由 store 夹具就绪；仅调低 force_confirm_threshold 走阶梯。"""
    store.config.guard = GuardConfig(force_confirm_threshold=2)
    return store


def test_force_confirmation_ladder(tight_store: Store):
    tight_store.write("notes/主题A", "# 主题A\n内容A")
    # 1st and 2nd forced bypass: under threshold, plain force works
    tight_store.write("notes/主题A-2", "# 主题A-2\nx", force=True)
    tight_store.write("notes/主题A-3", "# 主题A-3\nx", force=True)
    # 3rd: threshold reached → bare force refused
    with pytest.raises(StoreError, match="人工确认"):
        tight_store.write("notes/主题A-4", "# 主题A-4\nx", force=True)
    # with explicit confirmation → allowed
    r = tight_store.write("notes/主题A-4", "# 主题A-4\nx", force=True, force_confirm=True)
    assert r["forced"] is True
    assert tight_store.db.guard_stats()["forced"] == 3


def test_no_conflict_needs_no_force_or_confirm(store: Store):
    r = store.write("notes/独立主题", "# 独立主题\n内容")
    assert r["forced"] is False


def test_audit_resyncs_externally_edited_note(store: Store):
    store.write("notes/yacmemo部署配置", "# yacmemo部署配置\n\n- [配置] 服务端口为 9721\n")
    store.write("notes/端口配置说明", "# 端口配置说明\n\n- [配置] 端口为 8080\n")
    # FakeEmbedding: both 端口 observations collide → one open collision
    assert len(store.db.list_collisions(status="open")) == 1

    # out-of-band edit: the 端口 observation is gone
    p = store.root / "notes/yacmemo部署配置.md"
    p.write_text("# yacmemo部署配置\n\n- [运维] 改用每日备份\n", encoding="utf-8")

    r = store.audit()
    assert r["resynced"] == ["notes/yacmemo部署配置.md"]
    # collision recomputed from the edited side and no longer fires
    assert store.db.list_collisions(status="open") == []
    # fts reflects the external content
    assert store.db.fts_search("每日备份")
    assert store.db.fts_search("9721") == []


def test_audit_reports_and_prunes_externally_deleted_note(store: Store):
    store.write("notes/yacmemo部署配置", "# yacmemo部署配置\n\n- [配置] 端口 9721\n")
    (store.root / "notes/yacmemo部署配置.md").unlink()

    r = store.audit()
    assert r["missing"] == ["notes/yacmemo部署配置.md"]
    assert store.db.get_note("yacmemo部署配置.md") is None
    assert store.db.fts_search("端口 9721") == []


def test_audit_clean_when_no_external_changes(store: Store):
    store.write("notes/yacmemo部署配置", "# yacmemo部署配置\n内容")
    r = store.audit()
    assert r["resynced"] == [] and r["missing"] == []


def test_machine_zones_do_not_embed_observations(store: Store):
    """journal/audit/ 与 curator/ 是机器产物区：'- [时间] 处置行'会被
    parse_observations 当作伪 observation（类别=时间戳），但不得入 obs 空间。"""
    before = store.emb.calls  # 夹具种子已消耗若干次嵌入
    store.save("journal/audit/20260916-120000.md",
               "# 审计快照 20260916-120000\n\n- [2026-09-16 12:00] 已处理 D1:a|b\n")
    assert store.emb.calls == before + 1  # 仅 note 级一条；处置行未产生 embedding

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
    store.write("notes/yacmemo部署配置", "# yacmemo部署配置\n\n- [配置] 服务端口为 9721\n")
    store.write("notes/端口配置说明", "# 端口配置说明\n\n- [配置] 端口为 8080\n")
    assert len(store.db.list_collisions(status="open")) == 1


def test_machine_zone_titles_never_d1(store: Store):
    """curator 报告归一化剥日期后标题同构（"提案-0916"与"提案-0917"都归一为
    "提案"），机器产物区不得进入 D1 候选。"""
    store.save("curator/提案-2026-09-16.md", "# 记忆质量提案（yachen，2026-09-16）\n\n提案内容\n")
    store.save("curator/提案-2026-09-17.md", "# 记忆质量提案（yachen，2026-09-17）\n\n提案内容二\n")
    r = store.audit()
    assert r["title_duplicates"] == []


def test_audit_flags_dangling_registry_card(store: Store):
    """注册表指向不存在的 abstract 必须被点名（D5）——restructure/手工编辑
    TOPICS.md 的遗留，此前无任何检测（2026-09-16 notecalc-iced 实例）。"""
    store.topic_register("悬空卡主题")
    (store.root / "topics/悬空卡主题/abstract.md").unlink()
    store.db.remove_collisions_involving("topics/悬空卡主题/abstract.md")
    r = store.audit()
    assert any("悬空卡主题" in c for c in r["dangling_cards"])
    # 处置后重跑不再重放
    store.record_audit_action(r["audit_file"],
                              r["dangling_cards"][0], "resolved", "悬空卡主题")
    r2 = store.audit()
    assert all("悬空卡主题" not in c for c in r2["dangling_cards"])


def test_audit_snapshot_same_day_merge(store: Store):
    """同日多次审计合并进当天一份快照（复审小节追加），处置记录仍聚在末尾。"""
    from datetime import datetime

    store.write("notes/yacmemo部署配置", "# yacmemo部署配置\n内容")
    f1 = store.audit()["audit_file"]
    f2 = store.audit()["audit_file"]
    assert f1 == f2  # 同日同文件，不再每次落新快照
    assert f1 == f"journal/audit/{datetime.now().strftime('%Y%m%d')}.md"
    files = list((store.root / "journal/audit").glob("*.md"))
    assert len(files) == 1
    content = (store.root / f1).read_text(encoding="utf-8")
    assert content.count("## 复审（") == 1
    assert content.count("## 处置记录") == 1
    # 处置仍正确追加（复审小节插入不破坏处置段）
    store.record_audit_action(f1, "D4:x.md", "resolved", "测试处置")
    content2 = (store.root / f1).read_text(encoding="utf-8")
    assert "已处理 测试处置" in content2
    assert content2.index("## 复审（") < content2.index("## 处置记录")


def test_audit_snapshot_sections_are_intact_lines(store: Store):
    """有发现时快照各问题段必须逐行完整——_sec 曾被改成返回字符串，
    被 lines += 逐字符拆行（2026-09-17 生产实爆：D5 段一字一行）。"""
    # 写入已被注册制拦截，改走外部直建文件：审计自愈入索引后 D4 依旧点名
    (store.root / "孤儿笔记.md").write_text("# 孤儿笔记\n内容\n",
                                            encoding="utf-8")
    r = store.audit()
    content = (store.root / r["audit_file"]).read_text(encoding="utf-8")
    assert "## 游离文件（D4）" in content.splitlines()
    assert "- `D4:孤儿笔记.md` — `孤儿笔记.md`" in content.splitlines()
    # 爆炸特征：存在单字符行（合法 markdown 快照没有）
    assert not [ln for ln in content.splitlines() if len(ln) == 1 and ln != " "]


def test_audit_does_not_flag_its_own_snapshot_links(store: Store):
    """审计快照会引用悬空链接原文，源笔记删除后快照不得被 D3 自指点名
    （机器产物区不参与 D3，与 D1/D2 同口径——2026-09-18 实测自指循环）。"""
    store.write("notes/链接源", "# 链接源\n引用 [[不存在目标]]\n")
    r1 = store.audit()
    assert len(r1["dangling_links"]) == 1
    (store.root / "notes/链接源.md").unlink()
    r2 = store.audit()
    assert r2["dangling_links"] == []


def test_audit_d1_lines_carry_paths(store: Store):
    """同题不同目录的撞车只有路径能区分——D1 快照行必须带路径
    （2026-09-18 实测：六行 [[同名标题]] 完全一样，无法定位文件）。"""
    store.write("notes/同名笔记", "# 同名笔记\nA\n")
    store.write("notes/其他/同名笔记", "# 同名笔记\nB\n", force=True)
    r = store.audit()
    assert r["title_duplicates"], "expected D1 pairs"
    content = (store.root / r["audit_file"]).read_text(encoding="utf-8")
    d1_lines = [ln for ln in content.splitlines() if ln.startswith("- `D1:")]
    assert d1_lines
    assert all("`notes/同名笔记.md`" in ln or "`notes/其他/同名笔记.md`" in ln
               for ln in d1_lines)


def test_disposition_refusal_leaves_no_orphan_row(store: Store):
    """处置先写快照后落库：快照写失败不得留下无轨迹的孤儿处置行。"""
    with pytest.raises(StoreError):
        store.record_audit_action("", "D4:孤儿.md", "resolved", "x")
    assert store.db.list_audit_actions() == []


def test_audit_reports_and_heals_missing_vectors(store: Store):
    """端点故障期写入的笔记 vector_ok=0：审计点名 + 自愈重试（设计 #3）。

    时序必须是"先断端点、后写入"——健康期写入的文本向量已入缓存，
    自愈重试会命中缓存直接成功，模拟不出真实故障。"""

    class _BrokenEmb:
        def embed_one(self, text):
            raise ConnectionError("endpoint down")

        def embed(self, texts):
            raise ConnectionError("endpoint down")

    orig = store.emb
    store.emb = _BrokenEmb()
    try:
        # 端点故障期间写入：向量索引失败且未入缓存
        store.write("notes/故障期笔记", "# 故障期笔记\n- [配置] 端口 9721\n")
        assert store.db.get_note("notes/故障期笔记.md") is not None  # 文件与 FTS 正常
        r = store.audit()
        assert r["missing_vectors"] == ["notes/故障期笔记.md"]
        content = (store.root / r["audit_file"]).read_text(encoding="utf-8")
        assert "## 缺向量笔记（已重试自愈）" in content.splitlines()
        assert "- `notes/故障期笔记.md`" in content.splitlines()
    finally:
        store.emb = orig

    # 端点恢复后，下一次审计自愈成功、不再点名
    r2 = store.audit()
    assert r2["missing_vectors"] == []
    assert store.db.get_note("notes/故障期笔记.md") is not None


def test_audit_prunes_blank_disposition_rows(store: Store):
    """全空处置行（body 解析失败事故产物）由审计识别删除。"""
    store.db.record_audit_action("", "", "", "", "", "")
    assert len(store.db.list_audit_actions()) == 1
    r = store.audit()
    assert r["pruned_blank_actions"] == 1
    assert store.db.list_audit_actions() == []
