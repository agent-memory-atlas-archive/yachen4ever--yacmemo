"""路径入口面（path ingress）回归：守卫判过的字符串必须就是用掉的那个串。

2026-10-03 审查 S1/S1b/S2/S3/S5/S6 的成对修复。`test_path_guard.py` 钉的是
"收口本身"（越界必拒 + 合法写法不误伤），`test_path_guard_regressions.py` 钉
的是收口**周围**的洞；本文件钉的是第三个层面——**收口的返回值有没有被用**，
以及守卫压根不在场的入口。逐条对应：

- S1 `_index_row_ok()` 只判不返：调用方拿 notes 表原串去 `self.root / row`
  （pathlib 遇绝对右操作数直接丢 root），绝对索引行 fail-open，越界内容进
  FTS/向量库。修法是校验即返回值（`rel = ...`），越界整行隔离。
- S1b `issue_id="P:<file>:<index>"` 的 <file> 是第二条路径入口，且校验排在
  `add_exec_event` 之后——被拒的越界 id 照样在事件表里留下"已执行"。
- S2 `read()` 的 wiki-link related 段直接用未校验的 `target["path"]` 取
  observation：被污染的索引行把 root 外的正文原样吐给 agent。
- S3 TOPICS.md 的 `卡:` **从不调用守卫**，而消费端一律 `self.root / t["card"]`；
  匿名 agent 一次普通 memory_edit（TOPICS.md 在注册区内）即可让越界内容进
  每次冷启动的 memory_context 与 curator 的 LLM 提示词。
- S5 `rglob` 遍历不看路径语义：root 内指向库外的符号链接文件被原样读进索引。
- S6 日志里直接打原始路径：路径里的换行能伪造出第二行日志。

断言一律建立在**可观察后果**上（文件被没被读、内容进没进索引/向量、日志
行数），不靠报错文案——文案会变，越界没发生才是不可辩的证据。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from yacmemo.store import Store, StoreError

# 只出现在越界文件内容里的标记词。断言的是"内容"不是"路径"——审计快照
# 合法地引用越界路径字符串（那是留给人的证据）。
SECRET = "绝密外部档案"
LINK_TITLE = "机密"


# ---- 工具 ----------------------------------------------------------------


@pytest.fixture
def outsider(store: Store, tmp_path):
    """root 之外、只差一层的真实诱饵文件（root = tmp_path/memory）。"""
    d = tmp_path / "outside"
    d.mkdir(exist_ok=True)
    secret = d / "secret.md"
    secret.write_text(f"# 外部\n- [机密] {SECRET}\n", encoding="utf-8")
    return {"dir": d, "secret": secret}


@pytest.fixture
def reads(monkeypatch):
    """记录本用例期间每一次 `Path.read_text` 的目标——"有没有读盘"是这里
    最硬的证据：越界文件只要被读过，就一定在这里留下一条。"""
    seen: list[Path] = []
    orig = Path.read_text

    def spy(self, *a, **kw):
        seen.append(self)
        return orig(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", spy)
    yield seen
    monkeypatch.setattr(Path, "read_text", orig)


def _read_targets(seen: list[Path]) -> set[str]:
    return {str(p) for p in seen}


def _quarantined(res: dict) -> list[str]:
    return [e["path"] for e in res["quarantined_index_rows"]]


def _assert_not_indexed(store: Store, vectors, emb, rel: str) -> None:
    """该路径既没有正文、也没进 FTS/向量库，还不被当"缺向量"反复点名。"""
    assert store.db.fts_body(rel) is None
    assert all(r["path"] != rel for r in store.db.fts_search(SECRET, 20))
    assert all(r["path"] != rel for r in store.db.like_search(SECRET, 20))
    for hits in (vectors.search_note_vectors(emb.embed_one(SECRET), 500),
                 vectors.search_obs_vectors(emb.embed_one(SECRET), 500)):
        assert all(h.get("source_path") != rel for h in hits)
        assert all(SECRET not in (h.get("text") or "") for h in hits)


# ---- S1：索引行的路径必须用校验返回值，绝对行必须隔离 ----------------------


def test_index_row_guard_returns_the_validated_path_or_none(store: Store):
    """守卫的 API 契约：返回值就是可用的相对路径，越界返回 None。

    修前它返回 bool，调用方只能继续用 notes 表里的原串——判的是一个串、
    用的是另一个串。这条把"必须返回值"钉在签名上。
    """
    q: list[dict] = []
    assert store._index_row_ok("notes/a.md", q) == "notes/a.md"
    assert store._index_row_ok("notes/./a.md", q) == "notes/a.md"
    assert q == []

    for bad in ("../outside/secret.md", "/etc/passwd",
                str(store.root.parent / "outside" / "secret.md"),
                "C:/windows/win.ini", ""):
        assert store._index_row_ok(bad, q) is None, f"{bad!r} 没被拒"
    assert len(q) == 5, q


def test_absolute_index_row_is_quarantined_and_never_read(store: Store, outsider,
                                                          vectors, emb, reads):
    """绝对索引行 = 索引损坏，必须隔离：不读盘、不入索引、不删行。

    这是 S1 的正身：修前 `_norm_rel("/…/secret.md")` 剥掉前导斜杠、判定"在库
    内"、放行，调用方再 `self.root / "/…/secret.md"` 时 pathlib 把 root 丢
    掉，审计真的把库外文件读进了 FTS。
    """
    abs_path = str(outsider["secret"])
    store.db.upsert_note(abs_path, LINK_TITLE, "0" * 64)

    res = store.audit()

    assert abs_path in _quarantined(res)
    entry = next(e for e in res["quarantined_index_rows"]
                 if e["path"] == abs_path)
    assert entry["reason"], "隔离条目必须带拒绝理由"
    assert abs_path not in _read_targets(reads), "越界文件被读盘了"
    _assert_not_indexed(store, vectors, emb, abs_path)
    assert abs_path not in res["resynced"] and abs_path not in res["missing"]
    assert abs_path not in res["added"]
    assert abs_path not in res["missing_vectors"], "隔离行不该反复点名"
    assert store.db.get_note(abs_path) is not None, "越界行不得被 audit 删除"
    assert abs_path not in {p for p in store.list_notes()}


def test_absolute_index_row_is_quarantined_in_resync_call_site(store: Store,
                                                               outsider, reads):
    """第二个调用点（_resync_stale_notes 自愈）同样隔离——两个 site 都要覆盖。

    这一路尤其危险：它对"文件不在 → 删行"负责，解析不了的行落进那个分支
    就再也看不到是谁把越界 path 写进索引的。
    """
    abs_path = str(outsider["secret"])
    store.db.upsert_note(abs_path, LINK_TITLE, "0" * 64)

    resynced, missing, quarantined = store._resync_stale_notes()

    assert [e["path"] for e in quarantined] == [abs_path]
    assert abs_path not in resynced and abs_path not in missing
    assert abs_path not in _read_targets(reads)
    assert store.db.get_note(abs_path) is not None, "越界行被自愈逻辑删掉了"
    assert store.db.fts_body(abs_path) is None


def test_audit_only_ever_reads_paths_inside_root(store: Store, outsider, reads):
    """端到端不变量：一次 audit() 期间读过的每个文件都在 root 之内。

    比逐条断言更强——它对**所有**入口生效（含 S5 的符号链接），任何一条
    新加的读路径只要拼错就会在这里现形。
    """
    outside_dir = outsider["dir"].resolve()
    store.db.upsert_note(str(outsider["secret"]), LINK_TITLE, "0" * 64)
    store.db.upsert_note("../outside/secret.md", "另一个机密", "0" * 64)

    store.audit()

    leaks = [p for p in _read_targets(reads)
             if p.startswith(str(outside_dir))]
    assert not leaks, f"audit() 读了 root 外的文件: {leaks}"


# ---- S1b：issue_id 里的 P:<file> 是第二条路径入口 -------------------------


@pytest.mark.parametrize("issue_id", [
    "P:../outside/secret.md:1",       # 穿越
    "P:..\\outside\\secret.md:1",      # 反斜杠形态
    "P:/../../outside/secret.md:1",   # 前导斜杠 + 穿越
])
def test_p_issue_id_out_of_root_is_refused_without_side_effects(
        store: Store, outsider, issue_id, reads):
    """拒绝的越界 issue_id 不得在事件表里留下任何痕迹。

    修前校验排在 `add_exec_event` **之后**：调用抛错，事件却已经落库，
    exec_last_status 之后会拿它参与复审封口与结案调和——守卫拒掉的 id
    在系统里看起来像发生过。
    """
    with pytest.raises(StoreError):
        store.audit_exec_report(issue_id, "executed", note="汇报")

    assert str(outsider["secret"]) not in _read_targets(reads)
    assert not store.db.exec_last_status(), store.db.exec_last_status()
    assert not any(issue_id in e["issue_id"]
                   for e in store.db.list_exec_events(issue_id))


def test_p_issue_id_with_absolute_file_is_refused(store: Store, outsider, reads):
    """绝对路径的提案文件同样拒——它不是"少写了斜杠"，是指向库外的路径。

    修前：`_norm_rel` 剥掉前导斜杠放行，`_mark_proposal_settled` 拿原串
    `self.root / "/tmp/…/secret.md"` 真的把库外文件读进来解析成提案条目。
    """
    issue_id = f"P:{outsider['secret']}:1"

    with pytest.raises(StoreError):
        store.audit_exec_report(issue_id, "executed", note="汇报")

    assert str(outsider["secret"]) not in _read_targets(reads)
    assert not store.db.exec_last_status()
    assert str(outsider["secret"]).startswith("/")


def test_p_issue_id_in_root_still_works_and_settles(store: Store):
    """守门不能误伤：库内提案照常汇报、照常标结案。"""
    rel = "curator/提案-测试.md"
    store.save(rel, "# 提案-测试\n\n## 提案\n\n1. **[]** 修掉这个问题\n")
    store.audit_exec_report(f"P:{rel}:1", "executed", note="已修")

    assert store.db.exec_last_status()[f"P:{rel}:1"]["event"] == "executed"
    assert "已结案" in store.read(rel)["content"]


# ---- S2：wiki-link 的 related 段不得吐库外正文 ----------------------------


def test_wiki_link_to_poisoned_row_leaks_no_path_or_observation(
        store: Store, outsider, reads):
    """被污染的索引行经 [[链接]] 进来时，path 与 observation 都不得出现。

    修前 `_first_observation(target["path"])` 与 `"path": target["path"]`
    用的都是未校验原串：库外文件的第一条观察被原样写进 memory_read 返回值。
    """
    abs_path = str(outsider["secret"])
    store.db.upsert_note(abs_path, LINK_TITLE, "0" * 64)
    store.save("notes/链接源.md", f"# 链接源\n引用 [[{LINK_TITLE}]]。\n")

    out = store.read("notes/链接源.md")

    hit = [r for r in out["related"] if r["title"] == LINK_TITLE]
    assert len(hit) == 1, out["related"]
    assert "path" not in hit[0], f"越界路径出现在返回值里: {hit[0]}"
    assert SECRET not in (hit[0].get("note") or ""), hit[0]
    assert SECRET not in str(out), "库外观察泄漏进 memory_read 输出"
    assert abs_path not in _read_targets(reads)
    assert hit[0].get("quarantined") is True, "坏行必须显式标记，不能静默"


def test_healthy_wiki_link_related_section_unchanged(store: Store):
    """回归：正常链接的 related 段照旧给 path + 首条观察。"""
    store.save("notes/b.md", "# b\n- [经验] 端口 8080 是本地服务\n")
    store.save("notes/链接源.md", "# 链接源\n引用 [[b]]。\n")

    hit = [r for r in store.read("notes/链接源.md")["related"]
           if r["title"] == "b"]
    assert hit and hit[0]["path"] == "notes/b.md", hit
    assert "8080" in hit[0]["note"]


# ---- S3：TOPICS.md 的 `卡:` 是守卫之外的入口 ------------------------------


def _poison_registry_card(store: Store, card: str = "../outside/secret.md") -> None:
    """用一次普通 memory_edit 改写 TOPICS.md 的 `卡:`——攻击者的全部动作。

    TOPICS.md 落在注册区内、编辑不经过注册制硬约束，所以这一步对匿名 agent
    也是放行的：不需要任何预置的坏索引行。
    """
    store.edit("TOPICS.md", "- 现状: 测试主题",
               f"- 现状: 测试主题\n\n## 越界主题\n- 卡: {card}")


def test_invalid_topic_card_is_neutralised_but_recorded(store: Store, outsider):
    """非法卡：置空 + 留证，不抛（抛就会让每次冷启动的 memory_context 崩）。"""
    _poison_registry_card(store)

    topics = {t["title"]: t for t in store.load_topics()}
    bad = topics["越界主题"]
    assert bad["card"] == "", "非法卡必须被置空，不得原样透传给消费端"
    assert bad["card_invalid"] == "../outside/secret.md", "原值要留证"
    assert bad["card_error"], "必须记下拒绝理由"
    # 同一个注册表里的正常主题不受影响
    assert topics["笔记主题"]["card"] == "notes/a.md"
    assert not topics["笔记主题"]["card_invalid"]


def test_invalid_topic_card_does_not_leak_into_memory_context(store: Store,
                                                               outsider):
    """冷启动上下文与 curator 材料都不得含库外内容。"""
    _poison_registry_card(store)
    store.reindex()

    ctx = store.memory_context()
    assert SECRET not in ctx, "越界卡内容泄漏进 memory_context"
    assert "notes/a.md" in ctx, "正常主题的卡摘要不能被误伤"
    assert "笔记主题" in ctx


def test_invalid_topic_card_is_safe_for_curator_material(store: Store, outsider):
    """curator.build_material 消费同一个 `卡:`（我不可改 curator.py）。

    它的写法是 `p = store.root / t["card"] if t["card"] else None`，所以
    store 侧把卡置空就足以让它安全退化——这条钉住这个契约。
    """
    from yacmemo.curator import build_material

    _poison_registry_card(store)

    mat = build_material(store)
    assert SECRET not in mat, "越界卡内容泄漏进 curator 材料"
    assert "越界主题" in mat, "主题本身仍应在审阅材料里可见"


def test_audit_reports_invalid_topic_cards_under_its_own_key(store: Store,
                                                              outsider):
    """独立上报键：与 quarantined_index_rows 来源不同，不混、不写处置行。"""
    _poison_registry_card(store)
    before = store.db.list_audit_actions()

    res = store.audit()

    got = res["invalid_topic_cards"]
    assert [e["title"] for e in got] == ["越界主题"], got
    assert got[0]["card"] == "../outside/secret.md"
    assert got[0]["reason"]
    # 不复用隔离清单（那是 notes 表派生数据的问题），也不复用 D5（悬空卡）
    assert res["quarantined_index_rows"] == []
    assert not any("越界主题" in a["id"] for a in res["dangling_cards"])
    # 不写 audit_actions：那张表是人的 D1/D3/D4 处置状态
    assert store.db.list_audit_actions() == before

    md = (store.root / res["audit_file"]).read_text(encoding="utf-8")
    assert "注册表卡路径非法" in md
    assert "../outside/secret.md" in md, "快照要留证据，人才知道改哪一行"
    assert "注册表卡路径非法（已置空·未读盘）：0" not in md


@pytest.mark.parametrize("kind", ["traversal", "absolute"])
def test_archive_topic_with_invalid_card_does_not_raise(store: Store, outsider,
                                                         kind):
    """归档越界卡主题不得抛裸异常，更不得把库外文件搬进 archive/。

    修前 `t["card"]` 原样进 archive_topic：`posixpath.dirname` 给出库外目录，
    那里确实 is_dir()，于是 rglob + move 拿越界路径去搬——`..` 形态撞上
    move() 的守卫抛 StoreError（callable 却整条链坏掉），绝对路径形态连
    守卫都过得了（前导斜杠被容忍），最后落到一个跟"归档"毫无关系的路径上。
    """
    card = (str(outsider["secret"]) if kind == "absolute"
            else "../outside/secret.md")
    _poison_registry_card(store, card)

    out = store.archive_topic("越界主题")

    assert out["archived"] is True
    assert out["card"] == ""
    assert outsider["secret"].is_file(), "库外文件被动了"
    assert not any((store.root / "archive").rglob("secret.md")), \
        "库外内容被搬进了 archive/"
    assert "状态: archived" in (store.root / "TOPICS.md").read_text(encoding="utf-8")


def test_healthy_registry_audit_reports_no_invalid_cards(store: Store):
    """回归：无假阳性。健康库的 audit() 不多报这个键。"""
    res = store.audit()
    assert res["invalid_topic_cards"] == []
    md = (store.root / res["audit_file"]).read_text(encoding="utf-8")
    assert "## 注册表卡路径非法" not in md


# ---- S5：rglob 遍历必须逐个过守卫 ----------------------------------------


@pytest.fixture
def symlinked_escape(store: Store, outsider):
    """root 内一个指向库外的符号链接 .md——read() 侧守卫拒它，审计却不拒。"""
    link = store.root / "notes" / "escape.md"
    try:
        link.symlink_to(outsider["secret"].resolve())
    except (OSError, NotImplementedError) as e:      # pragma: no cover
        pytest.skip(f"本平台无法创建符号链接: {e}")
    stray_link = store.root / "escape" / "逃逸.md"
    stray_link.parent.mkdir(exist_ok=True)
    try:
        stray_link.symlink_to(outsider["secret"].resolve())
    except (OSError, NotImplementedError) as e:      # pragma: no cover
        link.unlink()
        pytest.skip(f"本平台无法创建符号链接: {e}")
    return {"in_topic_dir": link, "stray": stray_link}


def test_symlinked_escape_is_never_indexed(store: Store, outsider, symlinked_escape,
                                           vectors, emb, reads):
    """库外内容不得经符号链接进 FTS/向量库，也不得被当成库内笔记列出。"""
    res = store.audit()

    for rel in ("notes/escape.md", "escape/逃逸.md"):
        assert store.db.get_note(rel) is None, f"{rel} 被建了索引行"
        assert rel not in res["added"]
        _assert_not_indexed(store, vectors, emb, rel)
        assert rel not in store.list_notes()
    assert "escape/逃逸.md" not in res["stray"], "越界文件不该被报成游离文件"
    assert not any(str(p).startswith(str(outsider["dir"].resolve()))
                   for p in _read_targets(reads)), "库外文件被读盘了"


def test_reindex_skips_symlinked_escape(store: Store, outsider, symlinked_escape,
                                        vectors, emb):
    """reindex() 同样收口：重建索引是全量遍历，最容易绕过守卫。"""
    out = store.reindex()

    assert out["indexed"] > 0, "正常文件照常入索引"
    assert "notes/escape.md" not in [f["path"] for f in out["failed"]]
    assert store.db.get_note("notes/escape.md") is None
    _assert_not_indexed(store, vectors, emb, "notes/escape.md")
    # 库内真实文件照旧入索引（守卫不能收过头）
    assert store.db.get_note("notes/a.md") is not None
    assert store.db.get_note("notes/b.md") is not None


# ---- S6：日志不得被路径里的换行伪造 --------------------------------------


def test_newline_in_path_cannot_forge_a_log_line(store: Store, caplog):
    """`notes/bad\nERROR forged line.md` 这样的路径不得在日志里多出一行。"""
    forged = "ERROR forged line"
    name = f"notes/bad\n{forged} line.md"
    bad = store.root / name
    bad.write_text("# bad\n内容\n", encoding="utf-8")
    bad.chmod(0o000)
    try:
        bad.read_text(encoding="utf-8")
    except OSError:
        pass
    else:                                         # pragma: no cover
        pytest.skip("当前用户可读 000 文件（root？），无法触发读失败分支")
    try:
        with caplog.at_level(logging.WARNING, logger="yacmemo.store"):
            store.audit()
    finally:
        bad.chmod(0o644)

    text = caplog.text
    assert f"\n{forged}" not in text, "路径里的换行伪造出了一条日志行"
    # 诊断性不丢：原值仍以字面量形式留在日志里
    assert f"bad\\n{forged} line.md" in text, text


def test_log_safe_helper_escapes_control_chars():
    """其余日志点（向量索引失败、提案结案）共用同一个折行助手。"""
    from yacmemo.store import _log_safe

    assert _log_safe("a\nb") == "a\\nb"
    assert _log_safe("a\rb") == "a\\rb"
    assert _log_safe("a\\b") == "a\\\\b"
    assert _log_safe("正常路径.md") == "正常路径.md"


# ---- 回归：健康库与"可规范化的库内路径"都不该被误伤 ----------------------


def test_healthy_store_audit_is_unchanged(store: Store, vectors, emb):
    """健康库：隔离清单与非法卡清单恒为空，正常自愈照旧。"""
    res = store.audit()

    assert res["quarantined_index_rows"] == []
    assert res["invalid_topic_cards"] == []
    assert res["added"] == [] and res["resynced"] == []
    assert store.db.fts_body("notes/a.md")

    (store.root / "notes" / "新笔记.md").write_text("# 新笔记\n内容\n",
                                                   encoding="utf-8")
    res2 = store.audit()
    assert "notes/新笔记.md" in res2["added"]
    assert store.db.get_note("notes/新笔记.md") is not None

    (store.root / "notes" / "b.md").unlink()
    res3 = store.audit()
    assert "notes/b.md" in res3["missing"]
    assert store.db.get_note("notes/b.md") is None
    assert res3["quarantined_index_rows"] == []


@pytest.mark.parametrize("raw", ["notes/./a.md", "notes//a.md"])
def test_noncanonical_in_root_rows_are_not_false_positives(store: Store, raw,
                                                            vectors, emb):
    """'notes/./a.md' 指向库内真实文件——可规范化，不是越界，不得隔离。

    守卫收过头和收不住同样是回归：这类行照常自愈、照常进 FTS。
    """
    store.db.upsert_note(raw, "a", "0" * 64)

    res = store.audit()

    assert res["quarantined_index_rows"] == [], res["quarantined_index_rows"]
    assert raw in res["resynced"]
    assert store.db.fts_body(raw), "库内行照常可检索"
    assert SECRET not in (store.db.fts_body(raw) or "")


@pytest.mark.parametrize("probe,expect", [
    ("/topics/x.md", "topics/x.md"),        # 前导斜杠仍容忍（既有契约）
    ("notes//a.md", "notes/a.md"),
    ("notes/./a.md", "notes/a.md"),
])
def test_norm_rel_keeps_legitimate_forms(store: Store, probe, expect):
    """回归：收口不许变成新的摩擦源（合法写法照旧解析得出来）。"""
    assert store._norm_rel(probe) == expect


def test_norm_rel_strict_mode_rejects_absolute_paths(store: Store, outsider):
    """派生数据入口（索引行 / issue_id 提案文件）显式关掉前导斜杠容忍。

    绝对路径在那里不是笔误而是损坏的证据：容忍它等于把损坏洗成一个假的
    库内路径（指向另一个真实文件，或一个不存在的路径）。
    """
    with pytest.raises(StoreError):
        store._norm_rel("/topics/x.md", allow_leading_slash=False)
    with pytest.raises(StoreError):
        store._norm_rel(str(outsider["secret"]), what="索引行路径",
                        allow_leading_slash=False)
    # 默认（用户手输路径）仍容忍前导斜杠
    assert store._norm_rel("/topics/x.md") == "topics/x.md"
