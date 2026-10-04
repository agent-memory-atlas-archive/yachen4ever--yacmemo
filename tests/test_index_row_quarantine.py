"""越界索引行隔离：整索引循环（audit 正文取样 / _resync_stale_notes 自愈）
不得拿 notes 表里的 path 直接 `self.root / row["path"]` 去读盘。

reviewer 演示过的洞（旧版本构建写进 notes 表的越界 path）：

    索引里的路径   ['TOPICS.md', 'notes/a.md', '../outside/secret.md', ...]
    外部内容是否进了 FTS  ['../outside/secret.md']      ← 越界内容被读进索引
    文件消失后 越界行还在吗  False                        ← 被"外部已删除"静默删掉

守卫要求（与 resolve() 同一类洞，同一收口 _norm_rel）：
1. 越界行整行跳过——不 stat、不读、不删、不向量化，外部内容不得进索引；
2. 不删行：notes 表是派生数据，行本身是"谁写进了越界 path"的证据；
3. 不写 audit_actions：那张表的语义是人/agent 对 D1/D3/D4 的处置，
   记进去会污染处置状态、并可能把一条真正待办的报告滤掉；
4. 上报而不是静默丢弃：quarantined_index_rows 进 audit() 结果与审计快照。
"""

from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import (
    create_connected_server_and_client_session as connected,
)

from yacmemo.tools import register_tools

# 只出现在越界文件内容里的标记词（6 字，FTS trigram 能命中）。
# 断言的是"内容"而不是"路径"——审计快照会合法地引用越界路径字符串，
# 那是留给人的证据，不是泄漏。
SECRET = "绝密外部档案"
POISONED = "../outside/secret.md"


def _plant_out_of_root(store, path: str = POISONED,
                       body: str = f"# 外部\n{SECRET}\n") -> None:
    """模拟旧版本（有洞的）构建留下的越界索引行。

    content_hash 故意写错：修复前 _resync_stale_notes 会判定"外部修改"，
    把越界文件读进来重建索引（reviewer 演示的那一步）。
    """
    outside = (store.root / path).resolve()
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(body, encoding="utf-8")
    store.db.upsert_note(path, "外部档案", "0" * 64)


def _quarantined(res: dict) -> list[str]:
    return [e["path"] for e in res["quarantined_index_rows"]]


def test_audit_quarantines_out_of_root_row_without_indexing_it(store, emb,
                                                                vectors):
    _plant_out_of_root(store)
    res = store.audit()

    # 1. 外部内容没进 FTS：既没有该行的正文，也搜不到内容标记
    assert store.db.fts_body(POISONED) is None
    assert all(r["path"] != POISONED for r in store.db.fts_search(SECRET, 20))
    assert all(r["path"] != POISONED
               for r in store.db.like_search(SECRET, 20))

    # 1. 外部内容没进向量库（note_vectors + obs_vectors 都查）
    for hits in (vectors.search_note_vectors(emb.embed_one(SECRET), 500),
                 vectors.search_obs_vectors(emb.embed_one(SECRET), 500)):
        assert all(h.get("source_path") != POISONED for h in hits)
        assert all(SECRET not in (h.get("text") or "") for h in hits)

    # 4. 上报：audit() 结果里带结构化条目（路径 + 拒绝理由）
    assert POISONED in _quarantined(res)
    entry = next(e for e in res["quarantined_index_rows"]
                 if e["path"] == POISONED)
    assert entry["reason"], "隔离条目必须带拒绝理由"

    # 隔离行也不进"缺向量"清单——它每轮重试都不可能自愈，挂在那里只会
    # 训练人忽略审计；它的真问题在隔离清单里
    assert POISONED not in res["missing_vectors"]


def test_poisoned_row_is_not_deleted_by_audit(store):
    _plant_out_of_root(store)
    store.audit()
    assert store.db.get_note(POISONED) is not None, "越界行不得被 audit 删除"
    assert store.db.get_note(POISONED)["title"] == "外部档案"


def test_poisoned_row_survives_external_deletion_of_out_of_root_file(store):
    """「文件不在 → 删行」分支不得吃掉一个解析不了的行。

    连不上的行不是"文件被用户删掉"的证据：真删掉就再也看不到是谁把越界
    path 写进索引的了。
    """
    _plant_out_of_root(store)
    (store.root / POISONED).resolve().unlink()   # 外部文件真的被删了

    res = store.audit()

    assert store.db.get_note(POISONED) is not None
    assert POISONED not in res["missing"], "不得被记成外部删除"
    assert POISONED in _quarantined(res), "越界行改走隔离上报，不是删行"


def test_quarantine_writes_no_audit_action(store):
    """3. 隔离不进 audit_actions：那张表是人的处置状态，且会滤掉后续报告。

    **必须带阳性对照**。原版只断言"隔离跑完表没变"，那是断言一个从来没处在
    风险里的缺席：隔离逻辑整个不生效（表根本没被碰过）时它照样全绿——实测
    隔离关闭后本文件只剩 3 条通过，这就是其中之一。同一次审计里先种一条**真
    的合法处置**（D5：注册表指向不存在的 abstract），并要求四件事同时成立：

    1. 隔离真的发生了（POISONED 进隔离清单）——否则后半段又是空集合上的断言；
    2. 合法处置行原样留存（action/note 都没被隔离逻辑吞掉或改写）；
    3. 该处置**真的生效**：那条 D5 从此不再被列为待办——证明 audit_actions
       是一张活表、审计确实在读它，观察面不是死的；
    4. 越界行仍**没有**跟着进表，也没进 exec_status、没被"复审通过"封口。

    少了 1 就是空转，少了 2/3 就退回"缺席断言"，少 4 才是被修复前的行为。
    """
    _plant_out_of_root(store)
    # 阳性对照：一条真的 D5 问题（公开 API 造，issue_id 直接取自审计结果）
    store.topic_register("对照主题")
    (store.root / "topics/对照主题/abstract.md").unlink()
    res0 = store.audit()
    d5 = next(c for c in res0["dangling_cards"] if "对照主题" in c)
    store.record_audit_action(res0["audit_file"], d5, "resolved",
                              "对照主题", note="人工确认")

    res = store.audit()

    # 1. 隔离确实发生了（不是"因为隔离没跑所以什么都没发生"）
    assert POISONED in _quarantined(res), "隔离没发生，后面的缺席断言就是空转"

    # 2. 合法处置行原样留存——隔离不得吞掉人的处置
    kept = [a for a in store.db.list_audit_actions() if a["id"] == d5]
    assert len(kept) == 1, f"处置行被隔离逻辑动了: {store.db.list_audit_actions()}"
    assert (kept[0]["action"], kept[0]["note"]) == ("resolved", "人工确认"), kept[0]

    # 3. 处置真的生效：同一条 D5 不再重放（审计确实在读这张表）
    assert d5 not in res["dangling_cards"], "处置没生效，audit_actions 形同虚设"

    # 4. 越界行不进处置表，也不该因此被"复审通过"封口：隔离行没有 issue_id，
    #    也不在 exec 状态里
    assert not any(POISONED in a["id"] for a in store.db.list_audit_actions()), \
        "越界行被写进了人的处置表"
    assert POISONED not in (res.get("exec_status") or {})
    assert not any(POISONED in cid for cid in res["verified"])


def test_quarantine_surfaced_in_audit_snapshot_markdown(store):
    _plant_out_of_root(store)
    res = store.audit()

    md = (store.root / res["audit_file"]).read_text(encoding="utf-8")
    assert POISONED in md, "审计快照必须留证据，人/agent 才看得见"
    assert "越界索引行" in md
    assert "reindex()" in md, "快照要给出处置手段"


@pytest.mark.parametrize("bad", [
    "C:/windows/system32/notes.md",   # 盘符绝对路径（root / rel 会丢 root）
    "notes/../../escape.md",          # 中段穿越（_norm_rel 逐段判）
    "",                               # 空路径：非法的派生行
])
def test_other_escape_forms_are_quarantined_not_deleted(store, bad):
    store.db.upsert_note(bad, "逃逸行", "0" * 64)

    res = store.audit()

    assert bad in _quarantined(res)
    assert store.db.get_note(bad) is not None
    assert bad not in res["missing"]


def test_healthy_store_audit_has_empty_quarantine(store):
    """无假阳性：健康库的审计结果与修复前完全一致（隔离清单恒为空）。"""
    res = store.audit()

    assert res["quarantined_index_rows"] == []
    md = (store.root / res["audit_file"]).read_text(encoding="utf-8")
    assert "越界索引行（隔离·未读盘未删行）：0" in md
    assert "## 越界索引行" not in md, "没有隔离项时不该出现该小节"

    # 正常自愈照旧：外部新增的文件仍进 added 并建立索引
    (store.root / "notes" / "新笔记.md").write_text("# 新笔记\n内容\n",
                                                   encoding="utf-8")
    res2 = store.audit()
    assert "notes/新笔记.md" in res2["added"]
    assert store.db.get_note("notes/新笔记.md") is not None
    assert res2["quarantined_index_rows"] == []

    # 正常外部删除照旧：删掉文件后索引行被清理
    (store.root / "notes" / "b.md").unlink()
    res3 = store.audit()
    assert "notes/b.md" in res3["missing"]
    assert store.db.get_note("notes/b.md") is None
    assert res3["quarantined_index_rows"] == []


def test_noncanonical_but_in_root_row_is_not_a_false_positive(store):
    """'notes/./c.md' 这类可规范化的行仍指向库内文件——不得误判成越界。"""
    (store.root / "notes" / "c.md").write_text("# c\n内容C\n", encoding="utf-8")
    store.db.upsert_note("notes/./c.md", "c", "0" * 64)

    res = store.audit()

    assert res["quarantined_index_rows"] == []
    assert "notes/./c.md" in res["resynced"]


def test_quarantine_surfaces_through_the_mcp_audit_text(store, searcher):
    """守卫要求 4 的最后一环：memory_audit 是逐字段手渲染文本的，
    audit() 结果里多一个键，agent 侧不会自动看见——必须显式渲染出来。

    没有这条，隔离信息只停在 store 层，agent 端完全无感。
    """
    _plant_out_of_root(store)
    store.audit()          # 触发隔离登记

    mcp = FastMCP("quarantine-surface")
    register_tools(mcp, store, searcher)

    async def run():
        async with connected(mcp._mcp_server) as c:
            return await c.call_tool("memory_audit", {})

    text = asyncio.run(run()).content[0].text
    assert "越界索引行" in text, "隔离信息没进 memory_audit 的文本输出"
    assert POISONED in text
    assert "reindex" in text, "必须告诉 agent 处置手段"
