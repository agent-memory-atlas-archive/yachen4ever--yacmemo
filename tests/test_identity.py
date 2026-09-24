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
    # 平铺文件（agent 层）对同 agent 设备可见
    assert visible("agents/teleagent/必读.md", ident)
    # 本机子树可见
    assert visible("agents/teleagent/r9000x/环境.md", ident)
    # 兄弟设备子树不可见
    assert not visible("agents/teleagent/m5air/必读.md", ident)
    # 其他 agent 全树不可见
    assert not visible("agents/hermes/必读.md", ident)
    assert not visible("agents/hermes/r9000x/必读.md", ident)


def test_visibility_none_is_admin():
    assert visible("agents/hermes/r9000x/必读.md", None)
    assert visible("topics/x.md", None)


def test_writability_matrix(ident):
    assert not writable("topics/x.md", ident)          # user 层不经 writable 守卫，
    # 但 writable 的语义是"agents/ 区内可写"——user 层写守卫在 _require_covered
    assert writable("agents/teleagent/必读.md", ident)
    assert writable("agents/teleagent/r9000x/环境.md", ident)
    assert not writable("agents/teleagent/m5air/必读.md", ident)
    assert not writable("agents/hermes/必读.md", ident)
    assert not writable("agents/teleagent/新建目录/x.md", ident)  # 子目录=设备目录
    assert not writable("agents/teleagent/必读.md", None)          # 无身份不可写


# ---------------------------------------------------------------- store 守卫

def _write_must_read(store: Store, ident: Identity):
    r = store.write(f"{ident.agent_prefix}必读", "# 必读\n- 只写指针与纪律\n",
                    identity=ident)
    assert r["path"] == f"{ident.agent_prefix}必读.md"


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


def test_identity_write_agent_flat_file_exempt_from_registry(store: Store):
    ident = Identity("teleagent", "r9000x")
    _write_must_read(store, ident)
    # 同 agent 两台设备写同名必读——agent 层共享文件名冲突是合法场景，
    # 免注册 + 免重名守卫（D1 审计侧同步排除）
    r = store.write("agents/teleagent/必读", "# 必读\n- 更新内容\n",
                    identity=Identity("teleagent", "m5air"))
    assert r["path"] == "agents/teleagent/必读.md"


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


def test_human_write_agents_allowed(store: Store):
    r = store.write("agents/hermes/必读", "# hermes 必读\n")
    assert r["path"] == "agents/hermes/必读.md"


def test_read_guard_blocks_other_identity(store: Store):
    hermes = Identity("hermes", "r9000x")
    store.write("agents/hermes/必读", "# 必读\n", identity=hermes)
    tele = Identity("teleagent", "r9000x")
    with pytest.raises(StoreError) as e:
        store.read("agents/hermes/必读.md", identity=tele)
    assert "不可见" in str(e.value)
    # 本人可读；ANONYMOUS 也不可读 agents/ 区
    store.read("agents/hermes/必读.md", identity=hermes)
    with pytest.raises(StoreError):
        store.read("agents/hermes/必读.md", identity=ANONYMOUS)
    # 人类无身份全库可读
    store.read("agents/hermes/必读.md")


def test_read_by_title_still_guarded(store: Store):
    hermes = Identity("hermes", "r9000x")
    store.write("agents/hermes/必读", "# 必读\n- 守卫生效于标题解析之后\n",
                identity=hermes)
    with pytest.raises(StoreError):
        store.read("必读", identity=Identity("teleagent", "r9000x"))


def test_edit_and_delete_guarded(store: Store):
    hermes = Identity("hermes", "r9000x")
    store.write("agents/hermes/必读", "# 必读\n- 原文\n", identity=hermes)
    tele = Identity("teleagent", "r9000x")
    with pytest.raises(StoreError):
        store.edit("agents/hermes/必读.md", "- 原文", "- 改", identity=tele)
    with pytest.raises(StoreError):
        store.edit_section("agents/hermes/必读.md", "任意", "内容", identity=tele)
    with pytest.raises(StoreError):
        store.delete_note("agents/hermes/必读.md", identity=tele)
    # 本人可改可删
    store.edit("agents/hermes/必读.md", "- 原文", "- 改", identity=hermes)
    store.delete_note("agents/hermes/必读.md", identity=hermes)


def test_move_guarded_both_ends(store: Store):
    tele = Identity("teleagent", "r9000x")
    store.write("agents/teleagent/旧笔记", "# 旧笔记\n", identity=tele)
    # 移入其他 agent 的树：拒
    with pytest.raises(StoreError):
        store.move("agents/teleagent/旧笔记.md", "agents/hermes/x.md", identity=tele)
    # 移入自己设备子树：可
    r = store.move("agents/teleagent/旧笔记.md",
                   f"{tele.device_prefix}旧笔记.md", identity=tele)
    assert r["new_path"] == f"{tele.device_prefix}旧笔记.md"


def test_list_notes_filters_other_identity_zones(store: Store):
    store.write("agents/teleagent/必读", "# 必读\n", identity=Identity("teleagent", "r9000x"))
    store.write("agents/hermes/必读", "# 必读\n", identity=Identity("hermes", "r9000x"))
    store.write("notes/a", "# a\n补充内容\n")
    rows = store.list_notes(identity=Identity("teleagent", "r9000x"))
    assert "agents/teleagent/必读.md" in rows
    assert "agents/hermes/必读.md" not in rows
    assert "notes/a.md" in rows
    # 人类视角全量
    assert "agents/hermes/必读.md" in store.list_notes()


# ---------------------------------------------------------- memory_context

def test_memory_context_injects_identity_must_read(store: Store):
    store.write("agents/teleagent/必读", "# agent 层必读\n- 纪律A\n",
                identity=Identity("teleagent", "r9000x"))
    store.write("agents/teleagent/r9000x/必读", "# 本机必读\n- 纪律B\n",
                identity=Identity("teleagent", "r9000x"))
    ctx = store.memory_context(identity=Identity("teleagent", "r9000x"))
    assert "agents/teleagent/必读.md" in ctx
    assert "纪律A" in ctx
    assert "agents/teleagent/r9000x/必读.md" in ctx
    assert "纪律B" in ctx
    # hermes 的必读不出现
    store.write("agents/hermes/必读", "# hermes\n- hermes纪律\n",
                identity=Identity("hermes", "r9000x"))
    ctx = store.memory_context(identity=Identity("teleagent", "r9000x"))
    assert "hermes纪律" not in ctx


def test_memory_context_hint_when_no_must_read(store: Store):
    ctx = store.memory_context(identity=Identity("teleagent", "r9000x"))
    assert "专属必读（尚未创建）" in ctx
    assert "agents/teleagent/必读" in ctx
    assert "agents/teleagent/r9000x/必读" in ctx


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
    store.write("agents/hermes/必读", "# 必读\n- [纪律] hermes 专属端口配置 9722\n",
                identity=Identity("hermes", "r9000x"))

    tele = Identity("teleagent", "r9000x")
    hits = [r["path"] for r in searcher.search("端口", kind="fts", identity=tele)]
    assert "notes/a.md" in hits
    assert "agents/teleagent/r9000x/环境.md" in hits
    assert "agents/hermes/必读.md" not in hits

    hermes = Identity("hermes", "r9000x")
    hits = [r["path"] for r in searcher.search("端口", kind="fts", identity=hermes)]
    assert "agents/hermes/必读.md" in hits
    assert "agents/teleagent/r9000x/环境.md" not in hits

    # 人类视角全量
    hits = [r["path"] for r in searcher.search("端口", kind="fts")]
    assert "agents/hermes/必读.md" in hits
    assert "agents/teleagent/r9000x/环境.md" in hits


# ---------------------------------------------------------------- audit/D1

def test_d1_ignores_agent_zone(store: Store):
    # 两个 agent 各自的"必读"标题同构——D1 不得对 agents/ 报标题重复
    store.write("agents/teleagent/必读", "# 必读\n", identity=Identity("teleagent", "r9000x"))
    store.write("agents/hermes/必读", "# 必读\n", identity=Identity("hermes", "r9000x"))
    r = store.audit()
    d1_paths = {c["a_path"] for c in r["title_duplicates"]} | \
               {c["b_path"] for c in r["title_duplicates"]}
    assert not any(p.startswith("agents/") for p in d1_paths)


def test_stray_detection_exempts_agents(store: Store):
    store.write("agents/teleagent/必读", "# 必读\n", identity=Identity("teleagent", "r9000x"))
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
