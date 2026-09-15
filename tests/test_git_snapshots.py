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
    assert subjects[0].startswith("external: self-healed 1 note(s)")
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
