"""Store tests: write-path guards, edit anchors, move, read-related, journal exemption."""

from __future__ import annotations

import pytest

from yacmemo.store import AnchorError, Store, TitleConflict

NOTE_A = """# yacmemo部署配置

yacmemo 服务部署在 debsvc 上。

- [配置] 服务端口为 9721
- [配置] LLM 指向 m2ultra:11234
"""

NOTE_B = """# 备份策略

数据目录用 restic 每日备份。

- [运维] 备份目标是 NAS 的 backup 共享
"""


def test_write_creates_file_and_indexes(store: Store):
    r = store.write("yacmemo部署配置", NOTE_A)
    assert r["path"] == "yacmemo部署配置.md"
    assert (store.root / "yacmemo部署配置.md").is_file()
    assert store.db.get_note_by_title("yacmemo部署配置") is not None
    # FTS finds it
    assert store.db.fts_search("服务端口")[0]["path"] == "yacmemo部署配置.md"
    # vector + obs indexed
    assert store.vectors.search_note_vectors(
        store._embed_cached("yacmemo部署配置\n" + NOTE_A), 5)


def test_title_guard_refuses_near_duplicate(store: Store):
    store.write("yacmemo部署配置", NOTE_A)
    with pytest.raises(TitleConflict) as e:
        store.write("yacmemo部署配置-2", "# yacmemo部署配置-2\n端口改成 8080")
    assert e.value.matches and e.value.matches[0]["title"] == "yacmemo部署配置"
    assert store.db.guard_stats()["refused"] == 1


def test_title_guard_date_suffix_ignored(store: Store):
    """Same topic with a date suffix is still a conflict (dates stripped)."""
    store.write("yacmemo部署配置", NOTE_A)
    with pytest.raises(TitleConflict):
        store.write("yacmemo部署配置 2026-09-13", "内容")


def test_title_guard_force_bypass_records_event(store: Store):
    store.write("yacmemo部署配置", NOTE_A)
    r = store.write("yacmemo部署配置-2", "# yacmemo部署配置-2\n内容", force=True)
    assert r["forced"] is True
    assert store.db.guard_stats()["forced"] == 1


def test_journal_dir_exempt_from_guard(store: Store):
    store.write("yacmemo部署配置", NOTE_A)
    r = store.write("journal/yacmemo部署配置-0913", "# yacmemo部署配置-0913\n今天调了端口")
    assert r["forced"] is False
    assert r["path"].startswith("journal/")


def test_unrelated_titles_no_conflict(store: Store):
    store.write("yacmemo部署配置", NOTE_A)
    r = store.write("备份策略", NOTE_B)
    assert r["forced"] is False


def test_edit_anchor_must_exist(store: Store):
    store.write("yacmemo部署配置", NOTE_A)
    with pytest.raises(AnchorError):
        store.edit("yacmemo部署配置", "不存在的锚点", "x")


def test_edit_anchor_must_be_unique(store: Store):
    content = "# 配置\n\n- [配置] 端口为 9721\n\n- [配置] 端口为 9721\n"
    store.write("配置", content)
    with pytest.raises(AnchorError) as e:
        store.edit("配置", "端口为 9721", "x")
    assert "2 处" in str(e.value)


def test_edit_updates_index(store: Store):
    store.write("yacmemo部署配置", NOTE_A)
    store.edit("yacmemo部署配置", "服务端口为 9721", "服务端口改为 8080")
    content = (store.root / "yacmemo部署配置.md").read_text(encoding="utf-8")
    assert "服务端口改为 8080" in content
    # FTS reflects the new content
    hits = store.db.fts_search("8080")
    assert [h["path"] for h in hits] == ["yacmemo部署配置.md"]
    hits = store.db.fts_search("9721")
    assert hits == []


def test_edit_section_is_p2_stub(store: Store):
    store.write("yacmemo部署配置", NOTE_A)
    with pytest.raises(Exception, match="P2"):
        store.edit_section("yacmemo部署配置", "配置", "x")


def test_move_updates_all_indexes(store: Store):
    store.write("projects/yacmemo部署配置", NOTE_A)
    r = store.move("projects/yacmemo部署配置", "infra/yacmemo部署配置")
    assert r["new_path"] == "infra/yacmemo部署配置.md"
    assert not (store.root / "projects/yacmemo部署配置.md").exists()
    assert (store.root / "infra/yacmemo部署配置.md").is_file()
    # notes + fts follow the new path
    assert store.db.get_note("infra/yacmemo部署配置.md") is not None
    hits = store.db.fts_search("服务端口")
    assert [h["path"] for h in hits] == ["infra/yacmemo部署配置.md"]
    # title lookup still resolves to the new path
    assert store.resolve("yacmemo部署配置") == "infra/yacmemo部署配置.md"


def test_move_refuses_existing_target(store: Store):
    store.write("a笔记", "# a笔记\n内容A")
    store.write("b笔记", "# b笔记\n内容B")
    with pytest.raises(Exception, match="已存在"):
        store.move("a笔记", "b笔记")


def test_read_returns_related_by_link(store: Store):
    store.write("debsvc服务器", "# debsvc服务器\n\n- [主机] IP 192.168.5.7\n")
    note = "# yacmemo部署配置\n\n部署在 [[debsvc服务器]] 上。\n还引用了 [[不存在的笔记]]。\n"
    store.write("yacmemo部署配置", note)

    r = store.read("yacmemo部署配置")
    by_title = {x["title"]: x for x in r["related"]}
    assert by_title["debsvc服务器"]["via"] == "link"
    assert "IP 192.168.5.7" in by_title["debsvc服务器"]["note"]
    assert by_title["不存在的笔记"]["missing"] is True


def test_list_notes_sort_modes(store: Store):
    store.write("a笔记", "# a笔记\nA")
    store.write("b笔记", "# b笔记\nB")
    assert store.list_notes() == ["a笔记.md", "b笔记.md"]
    mtimes = store.list_notes(sort="mtime")
    assert set(mtimes) == {"a笔记.md", "b笔记.md"}


def test_write_without_embedding_still_indexes_fts(cfg, db):
    """FTS-only degradation: no embedding client configured."""
    s = Store(cfg, db, emb=None, vectors=None)
    s.write("端口配置", "# 端口配置\n\n服务端口为 9721\n")
    assert s.db.fts_search("端口配置")[0]["path"] == "端口配置.md"


def test_reindex_rebuilds_from_files(store: Store):
    store.write("yacmemo部署配置", NOTE_A)
    store.write("备份策略", NOTE_B)
    # simulate index loss
    store.db.clear_all()
    store.vectors.wipe()
    assert store.db.list_notes() == []

    r = store.reindex()
    assert r["indexed"] == 2 and r["failed"] == []
    assert store.db.get_note_by_title("yacmemo部署配置") is not None
    assert store.db.fts_search("restic")[0]["path"] == "备份策略.md"
