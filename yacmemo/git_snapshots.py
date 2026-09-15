"""Git snapshot layer: every store mutation leaves one commit.

Invariant (2026-09-16, user-approved design): after any store mutation the
memory repo is git-clean — "memory is always git-clean". Agents never need
filesystem access beyond the MCP tools:

- write/edit/edit_section/move/save/delete -> commit "{tool}: {path}"
- topic_register/unregister -> commit "topic: ..."
- audit self-heal of out-of-band changes -> one commit "external: ..."

Best effort by design: git missing, a non-repo root, or any git failure must
never block the memory write itself (markdown is the source of truth; git is
only the history). Degradation reasons are kept for audit reporting.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_GITIGNORE = ".index/\n"


class GitSnapshots:
    """Best-effort per-mutation git commits for one memory root."""

    def __init__(self, root: Path, enabled: bool = True,
                 user_name: str = "", user_email: str = ""):
        self.root = root
        self._lock = threading.Lock()
        self._user_name = user_name
        self._user_email = user_email
        self._disabled_reason = ""
        self._git = shutil.which("git") if enabled else None
        if not enabled:
            self._disabled_reason = "config: git_snapshots=false"
        elif self._git is None:
            self._disabled_reason = "git 可执行文件未找到"

    @property
    def active(self) -> bool:
        return self._git is not None

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run([self._git, "-C", str(self.root), *args],
                              capture_output=True, text=True, check=check)

    def _ensure_repo(self) -> bool:
        """Auto-init a fresh memory root on first snapshot, and make sure a
        commit identity exists (configured value, else yacmemo local defaults).
        Repo-local config is only written when neither local nor global
        identity is set — an existing identity is never overwritten."""
        r = self._run("rev-parse", "--is-inside-work-tree", check=False)
        if r.returncode != 0 or r.stdout.strip() != "true":
            init = self._run("init", "-q", check=False)
            if init.returncode != 0:
                self._disabled_reason = init.stderr.strip() or "git init 失败"
                return False
            ignore = self.root / ".gitignore"
            if not ignore.exists():
                ignore.write_text(_GITIGNORE, encoding="utf-8")
        for key, fallback in (("user.name", self._user_name or "yacmemo"),
                              ("user.email", self._user_email or "yacmemo@local")):
            cur = self._run("config", key, check=False)
            if cur.returncode != 0 or not cur.stdout.strip():
                self._run("config", key, fallback, check=False)
        return True

    def commit(self, message: str) -> str | None:
        """Stage all pending changes and commit. Returns short hash or None.

        Staging everything (add -A) is deliberate: out-of-band edits made
        directly on disk ride along, which keeps the git-clean invariant
        instead of leaving them pending until the next audit.
        """
        if not self.active:
            return None
        with self._lock:
            try:
                if not self._ensure_repo():
                    return None
                st = self._run("status", "--porcelain", check=False)
                if st.returncode != 0:
                    self._disabled_reason = st.stderr.strip()
                    return None
                if not st.stdout.strip():
                    return None  # nothing changed, nothing to snapshot
                self._run("add", "-A")
                c = self._run("commit", "-q", "-m", message, check=False)
                if c.returncode != 0:
                    if "nothing to commit" not in (c.stdout + c.stderr):
                        logger.warning("git commit failed: %s", c.stderr.strip())
                        self._disabled_reason = c.stderr.strip()
                    return None
                h = self._run("rev-parse", "--short", "HEAD", check=False)
                return h.stdout.strip() if h.returncode == 0 else "?"
            except Exception as e:  # never block the write path
                logger.warning("git snapshot failed (non-fatal): %s", e)
                self._disabled_reason = str(e)
                return None

    def status_line(self) -> str:
        if self.active:
            return "启用（每次变更自动 commit，仓库保持 git-clean）"
        return f"停用（{self._disabled_reason}）"
