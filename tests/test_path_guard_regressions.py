"""Path guard regressions found by the 2026-10-03 adversarial review.

`test_path_guard.py` 钉住"六处规范化收进 _norm_rel"本身；本文件钉住收口
**周围**的每一个洞——收口只在一个入口生效、却从别的入口绕过去时，测试全绿
而攻击照样成立。逐条对应：

- S1 `resolve()` 的索引行分支直接返回 `row["path"]`，不校验：只给标题就能
  让 read/edit/delete_note/move 落到 root 之外（实测删掉了 root 外的文件）。
- S2 保留前缀 `agents/` 用大小写敏感的 startswith 判定，而 macOS/Windows
  的文件系统不区分大小写：`Agents/bob/…` 逃出 identity 边界。
- S3 `issue_id` 里的 `P:<file>:<index>` 是第二条未收口的外部路径入口（读侧）。
- S4 `topic_register` 自动生成的主题卡路径绕过了收口。
- S5 收口引入的回归：`list_notes("/")` 曾经可用。
- S6 含 NUL 的路径让 `_norm_rel` 抛 ValueError 而非 StoreError。
"""

from __future__ import annotations

from contextlib import suppress

import pytest

from yacmemo.identity import ANONYMOUS, Identity, in_agents_zone, visible, writable
from yacmemo.store import Store, StoreError

# S1：修前 edit("../outside/victim.md") 能在索引里留下的越界行
POISON = "../outside/victim.md"


@pytest.fixture
def outsider(store: Store, tmp_path):
    """root 之外、只差一层的真实文件——断言建立在"文件还在不在"上。

    `store.root` 是 `tmp_path/memory`，所以诱饵放在 `tmp_path/outside/`：
    `../outside/x.md` 从 root 出发正好落在它上面。断言的是文件内容与存在性，
    报错文案只作旁证——文案会变，文件被读走/删掉才是不可辩的证据。
    """
    d = tmp_path / "outside"
    d.mkdir()
    secret = d / "victim.md"
    secret.write_text("# 机密\n只给 bob 看的内容。\n", encoding="utf-8")
    return {"dir": d, "secret": secret,
            "body": "# 机密\n只给 bob 看的内容。\n"}


def _plant_out_of_root_row(store: Store, tmp_path, title: str = "机密") -> str:
    """直接往索引里塞一行越界 path（模拟旧版本有洞的构建留下的残留）。"""
    store.db.upsert_note(POISON, title, "deadbeef")
    assert store.db.get_note(POISON) is not None
    return title


def _assert_untouched(outsider) -> None:
    assert outsider["secret"].is_file(), "root 外的文件被删除了"
    assert outsider["secret"].read_text(encoding="utf-8") == outsider["body"], \
        "root 外的文件被改写了"


# ---- S1：索引行是派生数据，不能当可信路径 -------------------------------

def test_resolve_refuses_out_of_root_index_row(store: Store, outsider, tmp_path):
    """只给标题（攻击者不需要知道任何路径）就能取回越界相对路径。"""
    _plant_out_of_root_row(store, tmp_path)
    with pytest.raises(StoreError) as e:
        store.resolve("机密")
    msg = str(e.value)
    assert POISON in msg, f"报错里没有坏行路径，运维无从下手: {msg}"
    assert "机密" in msg, f"报错里没有查的标题，无从复现: {msg}"
    _assert_untouched(outsider)


def test_read_refuses_out_of_root_index_row(store: Store, outsider, tmp_path):
    _plant_out_of_root_row(store, tmp_path)
    with pytest.raises(StoreError):
        store.read("机密")
    _assert_untouched(outsider)


def test_edit_refuses_out_of_root_index_row(store: Store, outsider, tmp_path):
    _plant_out_of_root_row(store, tmp_path)
    with pytest.raises(StoreError):
        store.edit("机密", "机密", "被改了")
    _assert_untouched(outsider)


def test_delete_refuses_out_of_root_index_row(store: Store, outsider, tmp_path):
    """这条是真删：修前 probe_s1.py 里 root 外的文件确实消失了。"""
    _plant_out_of_root_row(store, tmp_path)
    with pytest.raises(StoreError):
        store.delete_note("机密")
    _assert_untouched(outsider)


def test_move_refuses_out_of_root_index_row(store: Store, outsider, tmp_path):
    _plant_out_of_root_row(store, tmp_path)
    with pytest.raises(StoreError):
        store.move("机密", "notes/搬走了")
    _assert_untouched(outsider)
    assert not (store.root / "notes" / "搬走了.md").exists(), \
        "越界来源被搬进了 root（等于把 root 外的内容复制进来）"


def test_out_of_root_row_is_not_silently_dropped(store: Store, outsider, tmp_path):
    """守卫只拒绝、不改数据：删行还是修行是人的决定，代码不替他做。"""
    _plant_out_of_root_row(store, tmp_path)
    with pytest.raises(StoreError):
        store.read("机密")
    assert store.db.get_note(POISON) is not None, "坏行被静默删掉了"


def test_list_notes_never_surfaces_out_of_root_row(store: Store, outsider,
                                                   tmp_path):
    """列举面按定义只看得到 root 内的物理文件（越界行压根不在遍历范围里）。"""
    _plant_out_of_root_row(store, tmp_path)
    listed = store.list_notes()
    assert not any("outside" in p or ".." in p.split("/") for p in listed), listed
    _assert_untouched(outsider)


# ---- S2：保留前缀判定必须不区分大小写 -----------------------------------


@pytest.mark.parametrize("variant", [
    "Agents/bob/r9000x/必读.md",
    "AGENTS/bob/r9000x/必读.md",
    "aGeNtS/bob/r9000x/必读.md",
    "Agents\\bob\\r9000x\\必读.md",   # 反斜杠形态同样先归一
])
def test_agents_zone_membership_is_case_insensitive(variant: str):
    """纯字符串判定，不依赖文件系统——任何平台上都必须成立。"""
    assert in_agents_zone(variant), f"{variant!r} 被判成 user 层，保留区形同虚设"
    # 对照组：非保留区永远不算在区内（守卫不能收过头）
    assert not in_agents_zone("topics/notes/a.md")
    assert not in_agents_zone("agentsx/bob/x.md")


def test_anonymous_cannot_see_case_variant_agents_zone():
    """修前 startswith 大小写敏感，ANONYMOUS 直接把 Agents/ 当 user 层放行。"""
    bob = Identity(agent="bob", device="r9000x")
    for rel in ("agents/bob/r9000x/必读.md",
                "Agents/bob/r9000x/必读.md",
                "AGENTS/bob/r9000x/必读.md"):
        assert not visible(rel, ANONYMOUS), f"ANONYMOUS 看得见 {rel}"
        assert not writable(rel, ANONYMOUS), f"ANONYMOUS 写得了 {rel}"
        # 别人的 token 同样进不去（大小写变体不是后门）
        assert not visible(rel, Identity(agent="alice", device="laptop"))
        assert not writable(rel, Identity(agent="alice", device="laptop"))
        # 本人仍照常访问——守卫不能收过头
        assert visible(rel, bob) and writable(rel, bob), f"本人被误伤: {rel}"


def _fs_is_case_insensitive(probe_dir) -> bool:
    """探测宿主文件系统是否不区分大小写（不写死平台，Linux CI 上自动跳过）。

    探测本身必须**两个方向都无副作用**：它跑在 store.root 上，一个"只在 macOS
    上成立"的清理分支就足以让 Linux CI 从"跳过"退化成"报错"。

    - 清理按**两种拼写各试一次**，且一律 suppress：不区分大小写的盘上
      `caseprobe` 与 `CaseProbe` 是同一个目录（删一次即成，再删一次必然
      FileNotFoundError）；区分大小写的盘上真的建出来的只有 `CaseProbe`，
      只按小写拼写删同样 FileNotFoundError——两种拼法下都漏。
    - 清理的异常必须吞掉：finally 里抛出的异常会**整个吃掉 return 的布尔值**，
      函数从"返回 False"变成"抛异常"，调用点的 pytest.skip() 根本执行不到。
      探针的价值是那个布尔值，不是清理动作。
    """
    # exist_ok：上一次残留的同名目录不能让探测从"回答问题"退化成 FileExistsError
    (probe_dir / "CaseProbe").mkdir(exist_ok=True)
    try:
        return (probe_dir / "caseprobe").is_dir()
    finally:
        for spelling in ("CaseProbe", "caseprobe"):
            with suppress(OSError):
                (probe_dir / spelling).rmdir()


def test_case_variant_agents_path_cannot_touch_other_identity_file(
        store: Store, tmp_path):
    """端到端：大小写变体路径读/改/删都碰不到别人的专属文件。

    依赖"文件系统不区分大小写"才能让 `Agents/…` 指到真实的 `agents/…`：
    macOS/Windows 成立，Linux 上这条断言无意义（变体根本不是那个目录），
    故显式探测后跳过，而不是假装它在所有平台上都成立。
    """
    insensitive = _fs_is_case_insensitive(store.root)
    # 探测必须零残留（在跳过之前查，两条分支都受检）：漏下的 CaseProbe/caseprobe
    # 会留在 store.root 里，被后续的审计遍历/git 快照当成真实目录。
    residue = [n for n in ("CaseProbe", "caseprobe") if (store.root / n).exists()]
    assert not residue, f"大小写探测留下了残渣: {residue}"
    if not insensitive:
        pytest.skip("宿主文件系统区分大小写，Agents/ 与 agents/ 不是同一目录")
    bob = Identity(agent="bob", device="r9000x")
    store.save(f"{bob.device_prefix}必读.md", "# 必读\n只给 bob 看。\n")
    real = store.root / bob.device_prefix / "必读.md"
    before = real.read_text(encoding="utf-8")

    for rel in ("Agents/bob/r9000x/必读.md", "AGENTS/bob/r9000x/必读.md"):
        with pytest.raises(StoreError):
            store.read(rel, identity=ANONYMOUS)
        with pytest.raises(StoreError):
            store.edit(rel, "必读", "被改了", identity=ANONYMOUS)
        with pytest.raises(StoreError):
            store.delete_note(rel, identity=ANONYMOUS)
    assert real.is_file(), "别人的专属文件被大小写变体删掉了"
    assert real.read_text(encoding="utf-8") == before, "内容被改了"


# ---- S3：issue_id 里的 P:<file> 是第二路径入口 ---------------------------


@pytest.mark.parametrize("issue_id", [
    "P:../outside/victim.md:1",
    "P:/../../outside/victim.md:1",
    "P:..\\outside\\victim.md:1",
])
def test_audit_update_refuses_out_of_root_proposal_file(
        store: Store, outsider, issue_id: str):
    """读侧：修前会把 root 外的文件当提案读进来并解析条目。"""
    with pytest.raises(StoreError) as e:
        store.audit_exec_report(issue_id, "executed", note="汇报")
    assert "提案文件" in str(e.value), f"报错没点明是 issue_id 的文件段: {e}"
    _assert_untouched(outsider)


def test_audit_update_refuses_out_of_root_proposal_file_on_write(
        store: Store, outsider):
    """写侧：结案标记也走同一入口，两侧都拒。"""
    with pytest.raises(StoreError):
        store.audit_exec_report("P:../outside/victim.md:1", "executed")
    _assert_untouched(outsider)
    assert not any(a["id"].startswith("P:..")
                   for a in store.db.list_audit_actions()), \
        "越界 issue_id 仍被记进了事件表"


def test_audit_update_still_accepts_real_proposal(store: Store):
    """守门不能误伤：root 内的提案文件照常能汇报、能标结案。"""
    r = store.write("notes/a笔记", "# a笔记\n引用 [[ghost]]。\n")
    store.audit_exec_report(f"D3:{r['path']}|ghost", "executed", note="已修")
    assert store.db.exec_last_status()[f"D3:{r['path']}|ghost"]["event"] == "executed"


# ---- S4：自动生成的主题卡路径也要过收口 ---------------------------------


def test_topic_register_normalises_degenerate_card_path(store: Store):
    """退化标题（'..'、'/'）曾生成 `topics//abstract.md`——同一物理文件两个
    索引路径，正是 _norm_rel 存在的理由。收口后必须只剩一个规范路径。"""
    r = store.topic_register("..", description="退化标题")
    assert r["card"] == "topics/abstract.md", r["card"]
    assert "//" not in r["card"]
    rows = [row["path"] for row in store.db.all_titles()
            if row["path"].endswith("abstract.md")]
    assert rows == ["topics/abstract.md"], f"一个文件索引了两条路径: {rows}"
    # 另一个同样退化的标题归一到同一张卡 → 文件已存在即拒，不会索引出第二条
    with pytest.raises(StoreError):
        store.topic_register(" ... ", description="另一个退化标题")
    rows2 = [row["path"] for row in store.db.all_titles()
             if row["path"].endswith("abstract.md")]
    assert rows2 == ["topics/abstract.md"], f"退化标题又造了一条索引行: {rows2}"
    assert store.list_notes("topics") == ["topics/abstract.md"]


# ---- S5：list_notes("/") 这类写法的回归 ---------------------------------


@pytest.mark.parametrize("raw", ["/", "//", ".", "./", "///", "  /  "])
def test_list_notes_root_like_inputs_return_whole_root(store: Store, raw: str):
    """`memory_list(path="/")` 是合理调用；收口前它等价于 root。"""
    assert store.list_notes(raw) == store.list_notes("")
    assert "notes/a.md" in store.list_notes(raw)


def test_list_notes_still_rejects_real_traversal(store: Store, outsider):
    """恢复空路径语义不等于放开 ".."——真穿越仍然必拒。"""
    with pytest.raises(StoreError):
        store.list_notes("../outside")
    _assert_untouched(outsider)


# ---- S6：守卫的异常类型契约 ---------------------------------------------


@pytest.mark.parametrize("raw", [
    "notes/x.md\x00.txt",
    "notes/\x00",
    "agents/bob/\x00必读.md",
])
def test_null_byte_path_raises_store_error_not_valueerror(store: Store, raw: str):
    """含 NUL 的路径会让 Path.resolve() 抛 ValueError——调用方普遍只
    `except StoreError`，类型错了等于守卫被绕成崩溃。"""
    with pytest.raises(StoreError) as e:
        store._norm_rel(raw)
    assert "不是合法路径" in str(e.value), str(e.value)


def test_null_byte_never_reaches_the_filesystem(store: Store, outsider):
    """端到端：任何写入口遇到 NUL 都必须是 StoreError，且没有副作用。"""
    for call in (lambda: store.read("notes/x.md\x00"),
                 lambda: store.delete_note("notes/x.md\x00"),
                 lambda: store.save("notes/x.md\x00", "# 注入\n"),
                 lambda: store.write("notes/x\x00", "# 注入\n"),
                 lambda: store.move("notes/a.md", "notes/y\x00.md")):
        with pytest.raises(StoreError):
            call()
    _assert_untouched(outsider)
    assert sorted(p for p in store.list_notes() if p.startswith("notes/")) == \
        ["notes/a.md", "notes/b.md"]
