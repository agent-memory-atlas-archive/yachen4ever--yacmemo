"""Git snapshot layer: auto-init, per-mutation commits, degradation."""

from __future__ import annotations

import subprocess

import pytest

from yacmemo.config import MemoryConfig
from yacmemo.store import Store, TitleConflict


def _git(root, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, text=True, check=True)
    return r.stdout


def test_write_auto_inits_repo_and_commits(store: Store):
    """First write in a fresh root: repo auto-init + one commit, git-clean."""
    r = store.write("测试笔记", "# 测试笔记\n- [配置] 端口 9721\n")
    assert (store.root / ".git").is_dir()
    log = _git(store.root, "log", "--format=%s")
    assert f"write: {r['path']}" in log
    status = _git(store.root, "status", "--porcelain")
    assert status.strip() == ""  # git-clean invariant


def test_each_mutation_leaves_one_commit(store: Store):
    """write/edit/move each leave exactly one commit; delete too."""
    w = store.write("测试笔记", "# 测试笔记\nA\n")
    store.edit(w["path"], "A", "B")
    store.move(w["path"], "notes/测试笔记.md")
    store.delete_note("notes/测试笔记.md")

    subjects = _git(store.root, "log", "--format=%s").strip().splitlines()
    assert subjects == [
        "delete: notes/测试笔记.md",
        "move: 测试笔记.md -> notes/测试笔记.md",
        "edit: 测试笔记.md",
        f"write: {w['path']}",
    ]
    status = _git(store.root, "status", "--porcelain")
    assert status.strip() == ""


def test_refused_write_leaves_no_commit(store: Store):
    """Guard refusals must not snapshot (no file change happened)."""
    store.write("主题一", "# 主题一\n内容\n")
    before = _git(store.root, "rev-parse", "HEAD")
    with pytest.raises(TitleConflict):
        store.write("主题一2", "# 主题一2\n内容\n")  # near-duplicate title
    assert _git(store.root, "rev-parse", "HEAD") == before


def test_audit_commits_external_changes(store: Store):
    """Out-of-band edits ride along on the audit external commit."""
    store.write("测试笔记", "# 测试笔记\nA\n")
    p = store.root / "测试笔记.md"
    p.write_text("# 测试笔记\nB（外部改动）\n", encoding="utf-8")
    r = store.audit()
    assert r["resynced"] == ["测试笔记.md"]
    subjects = _git(store.root, "log", "--format=%s").strip().splitlines()
    # 审计自愈先提交 external，随后落盘审计快照（save 为最新一条）
    assert subjects[0].startswith("save: journal/audit/")
    assert any(s.startswith("external: self-healed 1 note(s)") for s in subjects)
    status = _git(store.root, "status", "--porcelain")
    assert status.strip() == ""


def test_disabled_via_config(tmp_path):
    """git_snapshots=false: writes work normally, no repo is created."""
    from yacmemo.config import Config
    from yacmemo.index_db import IndexDB

    root = tmp_path / "m"
    cfg = Config(memory=MemoryConfig(root=str(root), git_snapshots=False))
    db = IndexDB(cfg.sqlite_path)
    try:
        s = Store(cfg, db)
        assert not s.snapshots.active
        r = s.write("测试笔记", "# 测试笔记\n内容\n")
        assert (root / r["path"]).is_file()
        assert not (root / ".git").exists()
        assert "停用" in s.snapshots.status_line()
    finally:
        db.close()


def test_resolve_git_identity_priorities():
    """user override > [memory] override > id defaults."""
    from yacmemo.config import Config, MemoryConfig, UserEntry, resolve_git_identity

    cfg = Config(memory=MemoryConfig(root="/tmp/x"))
    u = UserEntry(id="yachen", root="/m")
    assert resolve_git_identity(u, cfg) == ("yachen", "yachen@yacmemo.com")
    assert resolve_git_identity(None, cfg) == ("local", "local@yacmemo.com")

    u2 = UserEntry(id="yachen", root="/m",
                   git_user_name="王旭晨", git_user_email="w@x.cn")
    assert resolve_git_identity(u2, cfg) == ("王旭晨", "w@x.cn")

    cfg2 = Config(memory=MemoryConfig(root="/tmp/x",
                                      git_user_name="全局", git_user_email="g@x.cn"))
    assert resolve_git_identity(u, cfg2) == ("全局", "g@x.cn")
    assert resolve_git_identity(u2, cfg2) == ("王旭晨", "w@x.cn")  # user wins


def test_configured_identity_lands_on_commits(store: Store):
    """Store(git_user=..., git_email=...) -> commits carry that identity."""
    s = Store(store.config, store.db, store.emb, store.vectors,
              root=store.root, git_user="王旭晨",
              git_email="wangxc4@chinatelecom.cn")
    r = s.write("身份测试", "# 身份测试\n内容\n")
    author = _git(store.root, "log", "--format=%an <%ae>",
                  "--", r["path"]).strip()
    assert author == "王旭晨 <wangxc4@chinatelecom.cn>"


def test_existing_identity_not_overwritten(store: Store):
    """A repo with pre-set identity keeps it — no silent overwrite."""
    _git(store.root, "init", "-q")
    _git(store.root, "config", "user.name", "预置用户")
    _git(store.root, "config", "user.email", "pre@set.dev")
    s = Store(store.config, store.db, store.emb, store.vectors,
              root=store.root, git_user="不应生效", git_email="no@pe.com")
    r = s.write("预置身份", "# 预置身份\n内容\n")
    author = _git(store.root, "log", "--format=%an", "--", r["path"]).strip()
    assert author == "预置用户"


def test_env_backfills_missing_home(monkeypatch):
    """systemd services run without HOME; git then misses the global config
    (safe.directory exemptions) and fails on dubious ownership. _env must
    backfill HOME from the password database."""
    import os
    import pwd

    from yacmemo.git_snapshots import GitSnapshots

    monkeypatch.delenv("HOME", raising=False)
    env = GitSnapshots._env()
    assert env["HOME"] == pwd.getpwuid(os.getuid()).pw_dir


def test_status_line_surfaces_runtime_failure(store: Store, monkeypatch):
    """A silently degraded snapshot layer must be visible in the audit line."""
    orig = store.snapshots._run

    class _Broken:
        returncode = 128
        stdout = ""
        stderr = "fatal: detected dubious ownership"

    monkeypatch.setattr(store.snapshots, "_run",
                        lambda *a, check=True, **kw: _Broken())
    store.write("失败可见性测试", "# 失败可见性测试\n内容\n")  # write still succeeds
    assert (store.root / "失败可见性测试.md").is_file()
    assert "失败" in store.snapshots.status_line()
    assert "dubious ownership" in store.snapshots.status_line()

    monkeypatch.setattr(store.snapshots, "_run", orig)
    store.delete_note("失败可见性测试")  # cleanup; snapshot path works again
    assert "启用" in store.snapshots.status_line()
