"""Identity 机制测试：token 解析、可见性/可写性矩阵、store 守卫、
memory_context 注入、scoped search、usage 归属。"""

from __future__ import annotations

import pytest

from yacmemo.identity import (
    ANONYMOUS,
    Identity,
    IdentityError,
    make_identity,
    parse_token,
    visible,
    writable,
)
from yacmemo.store import Store, StoreError

# ---------------------------------------------------------------- token

def test_parse_token_valid():
    i = parse_token("r9000x_teleagent")
    assert (i.agent, i.device) == ("teleagent", "r9000x")
    assert i.token == "r9000x_teleagent"
    assert i.agent_prefix == "agents/teleagent/"
    assert i.device_prefix == "agents/teleagent/r9000x/"


def test_parse_token_normalizes_case():
    i = parse_token("R9000X_TeleAgent")
    assert (i.agent, i.device) == ("teleagent", "r9000x")


def test_parse_token_splits_on_first_underscore():
    # 第一个下划线切分：device="dev"，agent="ice_agent" 含下划线 → 非法
    with pytest.raises(IdentityError):
        parse_token("dev_ice_agent")


def test_parse_token_rejects():
    for bad in ("teleagent", "", "dev_", "_agent", "dev_a_b_c"):
        with pytest.raises(IdentityError):
            parse_token(bad)


def test_parse_token_rejects_bad_slug_chars():
    with pytest.raises(IdentityError):
        parse_token("r9000x.Teleagent")
    with pytest.raises(IdentityError):
        parse_token("r9000x_记忆")


def test_make_identity_validates_each_slug():
    assert make_identity("teleagent", "r9000x").token == "r9000x_teleagent"
    with pytest.raises(IdentityError):
        make_identity("tele_agent", "r9000x")
    with pytest.raises(IdentityError):
        make_identity("teleagent", "r9000x!")


def test_device_name_shared_reserved():
    with pytest.raises(IdentityError):
        make_identity("teleagent", "shared")
    with pytest.raises(IdentityError):
        parse_token("shared_teleagent")


def test_anonymous_has_empty_token():
    assert ANONYMOUS.token == ""
    assert ANONYMOUS.agent == ""


# ------------------------------------------------- 可见性/可写性矩阵

@pytest.fixture
def ident():
    return Identity(agent="teleagent", device="r9000x")


def test_visibility_user_layer_shared(ident):
    assert visible("topics/x/笔记.md", ident)
    assert visible("journal/20260924.md", ident)
    assert visible("PROFILE.md", ident)


def test_visibility_agent_tree(ident):
    # 历史平铺文件只读兼容：仍可见
    assert visible("agents/teleagent/必读.md", ident)
    # shared 子树（agent 层共享）对本 agent 所有设备可见
    assert visible("agents/teleagent/shared/必读.md", ident)
    assert visible("agents/teleagent/shared/工作流/清理.md", ident)
    # 本机子树可见
    assert visible("agents/teleagent/r9000x/环境.md", ident)
    # 兄弟设备子树不可见
    assert not visible("agents/teleagent/m5air/必读.md", ident)
    # 其他 agent 全树不可见
    assert not visible("agents/hermes/shared/必读.md", ident)
    assert not visible("agents/hermes/r9000x/必读.md", ident)


def test_visibility_none_is_admin():
    assert visible("agents/hermes/r9000x/必读.md", None)
    assert visible("topics/x.md", None)


def test_writability_matrix(ident):
    assert not writable("topics/x.md", ident)          # user 层不经 writable 守卫，
    # 但 writable 的语义是"agents/ 区内可写"——user 层写守卫在 _require_covered
    assert writable("agents/teleagent/shared/必读.md", ident)
    assert writable("agents/teleagent/shared/工作流/清理.md", ident)
    assert writable("agents/teleagent/r9000x/环境.md", ident)
    # 平铺层只读兼容：写入收敛到 shared/ 与设备子树
    assert not writable("agents/teleagent/必读.md", ident)
    assert not writable("agents/teleagent/m5air/必读.md", ident)
    assert not writable("agents/hermes/shared/必读.md", ident)
    assert not writable("agents/teleagent/必读.md", None)          # 无身份不可写


# ---------------------------------------------------------------- store 守卫

def _write_must_read(store: Store, ident: Identity):
    r = store.write(f"{ident.shared_prefix}必读", "# 必读\n- 只写指针与纪律\n",
                    identity=ident)
    assert r["path"] == f"{ident.shared_prefix}必读.md"


def r_path(store: Store, rel: str) -> bool:
    return (store.root / rel).is_file()


def test_write_anonymous_to_agents_refused(store: Store):
    # MCP 无 token → tools 层传 ANONYMOUS → 拒绝（消息给出配置方式）
    with pytest.raises(StoreError) as e:
        store.write("agents/teleagent/必读", "# 必读\n", identity=ANONYMOUS)
    assert "identity" in str(e.value)
    # identity=None 是人类入口（WebUI/内部）——agents/ 写入放行
    assert store.write("agents/teleagent/必读", "# 必读\n")["path"] == \
        "agents/teleagent/必读.md"


def test_write_bad_token_error(store: Store):
    # tools 层把 IdentityError 转 StoreError；这里测 store 对 ANONYMOUS 的表现
    with pytest.raises(StoreError):
        store.write("agents/teleagent/必读", "# 必读\n", identity=ANONYMOUS)


def test_identity_write_agent_shared_exempt_from_registry(store: Store):
    ident = Identity("teleagent", "r9000x")
    _write_must_read(store, ident)
    # agent 层共享文件名跨设备同构是合法场景：免注册 + 免重名守卫
    # （D1 审计侧同步排除）；但覆盖被显式拒绝——m5air 设备须走 memory_edit
    with pytest.raises(StoreError):
        store.write("agents/teleagent/shared/必读", "# 必读\n- 更新内容\n",
                    identity=Identity("teleagent", "m5air"))
    store.edit("agents/teleagent/shared/必读.md", "- 只写指针与纪律",
               "- 更新内容", identity=Identity("teleagent", "m5air"))
    assert r_path(store, "agents/teleagent/shared/必读.md")


def test_flat_level_write_refused(store: Store):
    """agents/<agent>/ 第一层平铺文件只读兼容：写入/编辑一律拦截并指向 shared/。"""
    ident = Identity("teleagent", "r9000x")
    with pytest.raises(StoreError) as e:
        store.write("agents/teleagent/新笔记", "# 新笔记\n", identity=ident)
    assert "shared" in str(e.value)
    # 人类（WebUI）可写平铺层（管理员），但 agent 不可编辑
    store.write("agents/teleagent/旧笔记", "# 旧笔记\n")
    with pytest.raises(StoreError):
        store.edit("agents/teleagent/旧笔记.md", "# 旧笔记", "# 改",
                   identity=ident)


def test_identity_write_device_subtree(store: Store):
    ident = Identity("teleagent", "r9000x")
    r = store.write(f"{ident.device_prefix}环境", "# 环境\n- [设备] 4090\n",
                    identity=ident)
    assert r["path"] == f"{ident.device_prefix}环境.md"


def test_identity_write_other_agent_refused(store: Store):
    with pytest.raises(StoreError) as e:
        store.write("agents/hermes/必读", "# 必读\n", identity=Identity("teleagent", "r9000x"))
    assert "专属范围" in str(e.value)


def test_identity_write_other_device_refused(store: Store):
    with pytest.raises(StoreError):
        store.write("agents/teleagent/m5air/必读", "# 必读\n",
                    identity=Identity("teleagent", "r9000x"))


def test_identity_write_shared_subdir_refused(store: Store):
    # agents/<agent>/ 第一层子目录一律视为设备目录——不能借道创建"共享子目录"
    with pytest.raises(StoreError):
        store.write("agents/teleagent/共享/笔记.md", "内容",
                    identity=Identity("teleagent", "r9000x"))


def test_write_refuses_overwrite_in_agents(store: Store):
    """agents/ 区 memory_write 只创建不覆盖：跳过了标题守卫，必须显式拒绝
    静默覆盖（更新一律 memory_edit；WebUI 编辑器走 save() 不受影响）。"""
    ident = Identity("teleagent", "r9000x")
    store.write("agents/teleagent/shared/必读", "# v1\n", identity=ident)
    with pytest.raises(StoreError) as e:
        store.write("agents/teleagent/shared/必读", "# v2\n", identity=ident)
    assert "只创建不覆盖" in str(e.value)
    # 人类入口同样受守卫（WebUI 新建表单语义与 MCP 一致；覆盖走编辑器）
    with pytest.raises(StoreError):
        store.write("agents/teleagent/shared/必读", "# v3\n")
    # 文件未被覆盖
    assert (store.root / "agents/teleagent/shared/必读.md"
            ).read_text(encoding="utf-8") == "# v1\n"
    # 就地编辑仍然放行
    store.edit("agents/teleagent/shared/必读.md", "# v1", "# v1-edited",
               identity=ident)
    # 其他路径不受影响
    store.write("agents/teleagent/shared/其他", "# 其他\n", identity=ident)


def test_human_write_agents_allowed(store: Store):
    r = store.write("agents/hermes/必读", "# hermes 必读\n")
    assert r["path"] == "agents/hermes/必读.md"


def test_read_guard_blocks_other_identity(store: Store):
    hermes = Identity("hermes", "r9000x")
    store.write("agents/hermes/shared/必读", "# 必读\n", identity=hermes)
    tele = Identity("teleagent", "r9000x")
    with pytest.raises(StoreError) as e:
        store.read("agents/hermes/shared/必读.md", identity=tele)
    assert "不可见" in str(e.value)
    # 本人可读；ANONYMOUS 也不可读 agents/ 区
    store.read("agents/hermes/shared/必读.md", identity=hermes)
    with pytest.raises(StoreError):
        store.read("agents/hermes/shared/必读.md", identity=ANONYMOUS)
    # 人类无身份全库可读
    store.read("agents/hermes/shared/必读.md")


def test_read_by_title_still_guarded(store: Store):
    hermes = Identity("hermes", "r9000x")
    store.write("agents/hermes/shared/必读", "# 必读\n- 守卫生效于标题解析之后\n",
                identity=hermes)
    with pytest.raises(StoreError):
        store.read("必读", identity=Identity("teleagent", "r9000x"))


def test_edit_and_delete_guarded(store: Store):
    hermes = Identity("hermes", "r9000x")
    store.write("agents/hermes/shared/必读", "# 必读\n- 原文\n", identity=hermes)
    tele = Identity("teleagent", "r9000x")
    with pytest.raises(StoreError):
        store.edit("agents/hermes/shared/必读.md", "- 原文", "- 改", identity=tele)
    with pytest.raises(StoreError):
        store.edit_section("agents/hermes/shared/必读.md", "任意", "内容",
                           identity=tele)
    with pytest.raises(StoreError):
        store.delete_note("agents/hermes/shared/必读.md", identity=tele)
    # 本人可改可删
    store.edit("agents/hermes/shared/必读.md", "- 原文", "- 改", identity=hermes)
    store.delete_note("agents/hermes/shared/必读.md", identity=hermes)


def test_move_guarded_both_ends(store: Store):
    tele = Identity("teleagent", "r9000x")
    store.write("agents/teleagent/shared/旧笔记", "# 旧笔记\n", identity=tele)
    # 移入其他 agent 的树：拒
    with pytest.raises(StoreError):
        store.move("agents/teleagent/shared/旧笔记.md", "agents/hermes/x.md",
                   identity=tele)
    # 移入自己设备子树：可
    r = store.move("agents/teleagent/shared/旧笔记.md",
                   f"{tele.device_prefix}旧笔记.md", identity=tele)
    assert r["new_path"] == f"{tele.device_prefix}旧笔记.md"


def test_list_notes_filters_other_identity_zones(store: Store):
    store.write("agents/teleagent/shared/必读", "# 必读\n",
                identity=Identity("teleagent", "r9000x"))
    store.write("agents/hermes/shared/必读", "# 必读\n",
                identity=Identity("hermes", "r9000x"))
    store.write("notes/a", "# a\n补充内容\n")
    rows = store.list_notes(identity=Identity("teleagent", "r9000x"))
    assert "agents/teleagent/shared/必读.md" in rows
    assert "agents/hermes/shared/必读.md" not in rows
    assert "notes/a.md" in rows
    # 人类视角全量
    assert "agents/hermes/shared/必读.md" in store.list_notes()


# ---------------------------------------------------------- memory_context

def test_memory_context_injects_identity_must_read(store: Store):
    store.write("agents/teleagent/shared/必读", "# agent 层必读\n- 纪律A\n",
                identity=Identity("teleagent", "r9000x"))
    store.write("agents/teleagent/r9000x/必读", "# 本机必读\n- 纪律B\n",
                identity=Identity("teleagent", "r9000x"))
    ctx = store.memory_context(identity=Identity("teleagent", "r9000x"))
    assert "agents/teleagent/shared/必读.md" in ctx
    assert "纪律A" in ctx
    assert "agents/teleagent/r9000x/必读.md" in ctx
    assert "纪律B" in ctx
    # hermes 的必读不出现
    store.write("agents/hermes/shared/必读", "# hermes\n- hermes纪律\n",
                identity=Identity("hermes", "r9000x"))
    ctx = store.memory_context(identity=Identity("teleagent", "r9000x"))
    assert "hermes纪律" not in ctx


def test_memory_context_hint_when_no_must_read(store: Store):
    ctx = store.memory_context(identity=Identity("teleagent", "r9000x"))
    assert "专属必读（尚未创建）" in ctx
    assert "agents/teleagent/shared/必读" in ctx
    assert "agents/teleagent/r9000x/必读" in ctx


def test_memory_context_flags_stub(store: Store):
    """WebUI 预创建的占位模板：注入即读，且持续提醒 agent 填写。"""
    ident = Identity("teleagent", "r9000x")
    store.write(f"{ident.shared_prefix}必读",
                "# teleagent 专属必读（跨设备共享）\n\n"
                "> 占位模板：请用 memory_edit 就地替换为你的专属纪律与指针。\n",
                identity=ident)
    ctx = store.memory_context(identity=ident)
    assert "占位模板" in ctx
    assert "请用 memory_edit 就地填写专属纪律" in ctx


def test_memory_context_anonymous_no_identity_section(store: Store):
    store.write("notes/a", "# a\n补充\n")
    ctx = store.memory_context(identity=ANONYMOUS)
    assert "专属必读" not in ctx


# ------------------------------------------------------------ scoped search

def test_search_scoped_by_identity(store: Store, searcher):
    store.write("notes/a", "# 共享笔记\n- [配置] 服务端口为 9721\n")
    store.write("agents/teleagent/r9000x/环境",
                "# 环境\n- [设备] r9000x 有独立 GPU 端口配置\n",
                identity=Identity("teleagent", "r9000x"))
    store.write("agents/hermes/shared/必读", "# 必读\n- [纪律] hermes 专属端口配置 9722\n",
                identity=Identity("hermes", "r9000x"))

    tele = Identity("teleagent", "r9000x")
    hits = [r["path"] for r in searcher.search("端口", kind="fts", identity=tele)]
    assert "notes/a.md" in hits
    assert "agents/teleagent/r9000x/环境.md" in hits
    assert "agents/hermes/必读.md" not in hits

    hermes = Identity("hermes", "r9000x")
    hits = [r["path"] for r in searcher.search("端口", kind="fts", identity=hermes)]
    assert "agents/hermes/shared/必读.md" in hits
    assert "agents/teleagent/r9000x/环境.md" not in hits

    # 人类视角全量
    hits = [r["path"] for r in searcher.search("端口", kind="fts")]
    assert "agents/hermes/shared/必读.md" in hits
    assert "agents/teleagent/r9000x/环境.md" in hits


# ---------------------------------------------------------------- audit/D1

def test_d1_ignores_agent_zone(store: Store):
    # 两个 agent 各自的"必读"标题同构——D1 不得对 agents/ 报标题重复
    store.write("agents/teleagent/shared/必读", "# 必读\n",
                identity=Identity("teleagent", "r9000x"))
    store.write("agents/hermes/shared/必读", "# 必读\n",
                identity=Identity("hermes", "r9000x"))
    r = store.audit()
    d1_paths = {c["a_path"] for c in r["title_duplicates"]} | \
               {c["b_path"] for c in r["title_duplicates"]}
    assert not any(p.startswith("agents/") for p in d1_paths)


def test_stray_detection_exempts_agents(store: Store):
    store.write("agents/teleagent/shared/必读", "# 必读\n",
                identity=Identity("teleagent", "r9000x"))
    r = store.audit()
    assert not any(p.startswith("agents/") for p in r["stray"])


# ---------------------------------------------------------------- usage 归属

def test_usage_logs_identity(tmp_path):
    from yacmemo.usage import UsageDB
    u = UsageDB(str(tmp_path / "usage.db"))
    u.log_call("yachen", "memory_search", "query=端口", 5,
               identity="r9000x_teleagent")
    row = u.recent(limit=1)[0]
    assert row["identity"] == "r9000x_teleagent"
    u.log_call("yachen", "memory_search", "query=端口", 5)
    row = u.recent(limit=1)[0]
    assert row["identity"] == ""
    u.close()
