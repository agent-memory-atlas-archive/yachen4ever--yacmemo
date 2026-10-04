"""Path-ingress guard: every externally supplied path must stay inside root.

2026-10-03 审查 F1：`Store.resolve()` 只 lstrip 前导斜杠、不拒 ".."，而
`save()` 明确拒 ".."——同一个库两套标准。传导面是读/删/列举/改全开，
且 `_require_agents_write` 只匹配 `agents/` 前缀，`../..` 直接放行。

修法是把六处各写各的路径规范化收进 `Store._norm_rel()`，本文件钉住
"越界必拒 + 合法写法不误伤"两侧——规范化收口最怕的就是收过头。

诱饵（canaries）放在 memory root 的**同级**目录、探针只穿越**一级**：这样
每条探针都真的落在真实文件上，断言可以建立在"文件还在不在、内容变没变"
上。放在根目录之外更远的位置时，`../../` 反而越过了一级，探针一条也没落
到诱饵上——"用真实文件击败『未找到』兜底"就成了空话。
"""

from __future__ import annotations

import pytest

from yacmemo.store import Store, StoreError

# 越界探针：`..` 穿越（正/反斜杠）、盘符绝对路径、深层夹带。
# 前四个只穿一级，正好落在 canaries 上（root = tmp_path/memory）。
TRAVERSAL = [
    "../canary/config.toml",
    "../canary/secret.md",
    "..\\canary\\secret.md",
    "notes/../../canary/secret.md",
    # 下面这些不指向诱饵，只验"形态必拒"
    "../../config.toml",
    "../../other/memory/topics/secret.md",
    "..\\..\\config.toml",
    "/../../config.toml",
    "C:/Windows/win.ini",
    "C:evil.md",
    "..",
    "a/..",
]


@pytest.fixture
def canaries(store: Store, tmp_path):
    """root 之外、只差一层的真实诱饵文件——越界成功就会读到/删到它们。

    位置是成败关键：`store.root` 是 `tmp_path/memory`（见 conftest），诱饵
    因此放在它的同级 `tmp_path/canary/`，`../canary/x.md` 从 root 出发正好
    落在上面（第一版把诱饵放在 `tmp_path/` 自己身上，`../../` 越过一级，
    67 条探针没有一条真落到诱饵上）。

    必须真实存在：修前 resolve() 对不存在的目标会落到"未找到笔记"分支，
    那也是一个 StoreError，只断言 raises 会让这批用例在有洞的代码上照样
    全绿（第一版就是这么写的，67 条里 57 条打旧代码是绿的）。
    """
    d = tmp_path / "canary"
    (d / "topics").mkdir(parents=True)
    config = d / "config.toml"
    config.write_text("[secret]\ntoken = abc\n", encoding="utf-8")
    secret = d / "secret.md"
    secret.write_text("# 别人的秘密\n不该被读到。\n", encoding="utf-8")
    nested = d / "topics" / "deep.md"
    nested.write_text("# 深一层\n也不该被读到。\n", encoding="utf-8")
    return {"dir": d, "config": config, "secret": secret, "nested": nested,
            "topics_dir": d / "topics",
            "config_body": "[secret]\ntoken = abc\n"}


def _assert_canaries_intact(canaries) -> None:
    """诱饵既没被删，也没被改——这是"越界没发生"的直接证据。"""
    for key in ("config", "secret", "nested"):
        assert canaries[key].is_file(), f"越界操作把 root 外的 {key} 删掉了"
    assert canaries["config"].read_text(encoding="utf-8") == canaries["config_body"]
    assert canaries["secret"].read_text(encoding="utf-8").startswith("# 别人的秘密")


# 越界拒绝的报错必须来自守卫本身，而不是"没找到"这条兜底分支。
_GUARD_REASONS = ("路径穿越", "盘符", "越出记忆库根目录", "空的")


def _assert_blocked(fn, *args, **kwargs):
    """调用必须因越界守卫而失败——顺带钉住"拒的理由"。"""
    with pytest.raises(StoreError) as e:
        fn(*args, **kwargs)
    assert any(r in str(e.value) for r in _GUARD_REASONS), (
        f"拒绝理由不是越界守卫，而是别的兜底分支: {e.value}")
    return e


# ---- 越界必拒 ----------------------------------------------------------

@pytest.mark.parametrize("probe", TRAVERSAL)
def test_resolve_rejects_traversal(store: Store, probe: str):
    """resolve 只返回路径、不碰文件系统，所以这里不挂 canaries 断言。"""
    _assert_blocked(store.resolve, probe)


@pytest.mark.parametrize("probe", TRAVERSAL)
def test_read_rejects_traversal(store: Store, canaries, probe: str):
    """读面：F1 修前 resolve('../../config.toml') 直接返回该相对路径。"""
    _assert_blocked(store.read, probe)
    _assert_canaries_intact(canaries)


@pytest.mark.parametrize("probe", TRAVERSAL)
def test_delete_rejects_traversal(store: Store, canaries, probe: str):
    """删面：修前 abs_path.unlink() 会删掉 root 之外的任意文件。"""
    _assert_blocked(store.delete_note, probe)
    _assert_canaries_intact(canaries)


@pytest.mark.parametrize("probe", ["..", "../..", "../canary", "../canary/topics",
                                    "notes/../../canary"])
def test_list_rejects_traversal(store: Store, canaries, probe: str):
    """列举面：修前 base 可以被指到 root 之外的目录树，诱饵会被直接列出来。"""
    _assert_blocked(store.list_notes, probe)
    # 诱饵就在 ../canary —— 修前这里会把它的真实路径原样返回
    assert not any("secret.md" in p for p in store.list_notes()), \
        "root 外的诱饵出现在列举结果里"
    _assert_canaries_intact(canaries)


@pytest.mark.parametrize("probe", TRAVERSAL)
def test_save_rejects_traversal(store: Store, canaries, probe: str):
    _assert_blocked(store.save, probe, "# 注入\n")
    _assert_canaries_intact(canaries)


@pytest.mark.parametrize("probe", TRAVERSAL)
def test_move_target_rejects_traversal(store: Store, canaries, probe: str):
    store.write("notes/可移动", "# 可移动\n内容\n")
    _assert_blocked(store.move, "notes/可移动.md", probe)
    _assert_canaries_intact(canaries)
    assert (store.root / "notes" / "可移动.md").is_file(), "源文件不该被移动走"


@pytest.mark.parametrize("probe", ["../evil", "notes/../../evil", "C:/evil"])
def test_write_title_rejects_traversal(store: Store, probe: str):
    """标题入口同样收口——否则 memory_write 就是一个绕过 save 的写门。"""
    _assert_blocked(store.write, probe, "# 注入\n")


@pytest.mark.parametrize("probe", ["../canary/secret.md", "../canary/config.toml",
                                    "../../config.toml", "C:/x.md"])
def test_topic_card_path_rejects_traversal(store: Store, canaries, probe: str):
    """修前 card_path 只 lstrip 前导斜杠：指向真实存在的诱饵时 is_file() 直接
    通过，注册表就会多出一张指向 root 之外的卡（卡被索引成第二个路径）。"""
    _assert_blocked(store.topic_register, "穿越主题", description="x",
                    card_path=probe)
    _assert_canaries_intact(canaries)
    assert "穿越主题" not in (store.root / "TOPICS.md").read_text(encoding="utf-8"), \
        "越界主题卡被写进了注册表"
    assert all(".." not in t["card"] for t in store.load_topics()), store.load_topics()


def test_audit_disposition_rejects_traversal(store: Store, canaries):
    """审计处置/提案入口也吃外部路径，同样不得越界。"""
    af = store.audit()["audit_file"]
    _assert_blocked(store.record_audit_action, "../canary/config.toml", "D4",
                    "x.md", "已忽略")
    _assert_blocked(store.record_proposal_action,
                    "../canary/secret.md", 1, "ignored")
    assert af  # 审计本身仍然可用
    _assert_canaries_intact(canaries)


# ---- 合法写法不误伤 ----------------------------------------------------

# ---- 前导斜杠：可以放行，但绝不允许越界 -------------------------------

def test_leading_slash_inputs_never_escape_root(store: Store, canaries):
    """`/topics/x.md` 这类前导斜杠必须继续能用（既有容忍度），规范化后
    只能是 root 内的相对路径。

    UNC 形态（`//server/share/x.md`）同理：剥掉前导斜杠后它退化成
    root 内的相对路径，交不到 pathlib 手里当绝对路径解析——真正会把 root
    整个丢弃的是盘符，那一条已显式拒。

    旧版这里是「抛异常就 continue」，于是一个把所有输入一律拒掉的
    `_norm_rel` 也能全绿——收过头和收不住同样是回归，所以先钉住合法写法
    确实解析得出来。
    """
    assert store._norm_rel("/topics/x.md") == "topics/x.md", "合法写法被误伤"
    assert store._norm_rel("topics/x.md") == "topics/x.md", "合法写法被误伤"

    unc = "/" * 2 + "server" + "/" + "share" + "/x.md"
    for probe in ["/topics/x.md", "topics/x.md", unc, "/etc/passwd",
                  "/../../etc/passwd"]:
        try:
            rel = store._norm_rel(probe)
        except StoreError:
            continue                      # 拒掉也算守住不变量
        assert not rel.startswith("/") and ".." not in rel.split("/"), (
            f"越界了: {probe!r} -> {rel!r}")
        assert (store.root / rel).resolve().is_relative_to(store.root.resolve())

    # 绝对路径形态绝不能越界：要么被拒，要么（只可能是 UNC 退化形态）落在
    # root 内。诱饵就在 ../canary——这条探针必须被拒。
    for probe in ["/etc/passwd", "/../../etc/passwd", "../canary/secret.md"]:
        try:
            rel = store._norm_rel(probe)
        except StoreError:
            continue
        assert (store.root / rel).resolve().is_relative_to(store.root.resolve()), \
            f"越界了: {probe!r} -> {rel!r}"
    _assert_canaries_intact(canaries)


@pytest.mark.parametrize("raw,expect", [
    ("topics/x.md", "topics/x.md"),
    ("/topics/x.md", "topics/x.md"),
    ("topics\\x.md", "topics/x.md"),
    ("topics//x.md", "topics/x.md"),
    ("topics/./x.md", "topics/x.md"),
])
def test_norm_rel_keeps_legitimate_forms(store: Store, raw: str, expect: str):
    """Windows 反斜杠、前导斜杠都要继续能用——守卫不能变成新的摩擦源。"""
    assert store._norm_rel(raw) == expect


def test_norm_rel_allows_legit_path_under_symlinked_root(store: Store, tmp_path):
    """root 自身是符号链接时，合法路径不得被包含性断言误判为越界。

    macOS 的 /var → /private/var、Linux 的 /tmp → /private/tmp 都是这种形状；
    断言两侧必须都比 resolve 后的 root。
    """
    real = tmp_path / "real-root"
    real.mkdir()
    link = tmp_path / "link-root"
    link.symlink_to(real)
    s = Store.__new__(Store)          # 绕开 __init__：只测纯路径判定
    s.root = link                     # 故意不给 resolve 过的 root
    assert s._norm_rel("notes/x.md") == "notes/x.md"


# ---- §7 重复索引：同一物理文件被索引成两个路径 --------------------------

def test_title_with_duplicate_separators_indexes_one_path(store: Store):
    """'notes//dup' 与 'notes/./dup' 指向同一文件，规范化后必须只剩一个路径。

    修前两者各自成 path → D1 报相似度 1.0 的「标题重复」，而 memory_list
    只给一个，agent 拿到一个无法处置的假问题。
    """
    assert store.title_to_path("notes//dup") == "notes/dup.md"
    assert store.title_to_path("notes/./dup") == "notes/dup.md"
    r = store.write("notes//dup", "# dup\n内容\n")
    assert r["path"] == "notes/dup.md"
    assert store.list_notes().count("notes/dup.md") == 1
    assert store.db.get_note("notes//dup.md") is None
