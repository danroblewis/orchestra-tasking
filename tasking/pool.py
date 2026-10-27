"""Reused worktrees (``[tasks] reuse_worktrees = true``, code mode): instead of a worktree made
for each ticket and removed when it lands, the task system keeps at most ``worktree_pool`` of
them under ``<state>/pool/<n>/`` and resets one for each turn: a worker's, a reviewer's, a gate
run's. A repository whose checkout is large, with ignored build caches that take long to make, is
the reason: a reset keeps the ignored files unless ``worktree_clean_ignored`` says otherwise.

Which slot is held by whom is the store's record (``Runtime.claim_slot``); this module makes the
worktree on disk match what a holder needs. SPEC.md section 5."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from .config import Project
from .git import Git, GitError
from .store import LocalStore


class Pool:
    def __init__(self, project: Project, store: LocalStore, git: Git, log: Callable[[str], None] = print):
        self.project, self.store, self.git, self.log = project, store, git, log

    @property
    def cfg(self):
        return self.project.config

    def claim(self, holder: str, ticket: str, prefer: str | None = None) -> Path | None:
        """A worktree for ``holder`` (made if the slot is new or its worktree is gone), or None
        when the pool is full: the caller waits. Its contents are as the last holder left them
        until ``reset``."""
        got = self.store.claim_slot(holder, ticket, self.cfg.worktree_pool, prefer)
        if got is None:
            return None
        slot, new = got
        path = Path(slot["path"])
        try:
            if new or not self._is_worktree(path):
                self._make(path)
        except GitError:
            if new:
                self.store.forget_slot(slot["name"])
            else:
                self.store.release_slot(holder)
            raise
        return path

    def claim_for(self, holder: str, ticket: str, branch: str) -> Path | None:
        """``claim``, then ``reset`` to the ticket's branch; the slot it last used comes first."""
        path = self.claim(holder, ticket, prefer=ticket)
        if path is not None:
            try:
                self.reset(path, branch)
            except GitError:
                self.store.release_slot(holder)
                raise
        return path

    def reset(self, path: Path, branch: str) -> None:
        """The worktree on ``branch`` (made from the base when it does not exist), with nothing
        left of the last holder but its ignored files (unless ``worktree_clean_ignored``): an
        unfinished merge or rebase aborted, tracked changes discarded, untracked files removed.
        A worktree a crash left dirty is put right here, whatever the crash left."""
        g, clean = self.git, ("clean", "-ffdx" if self.cfg.worktree_clean_ignored else "-ffd")
        for op in ("merge", "rebase", "cherry-pick", "revert", "am"):
            g.run(op, "--abort", cwd=path, check=False)
        g.run("reset", "-q", "--hard", cwd=path)
        g.run(*clean, cwd=path)
        self.release_branch(branch, keep=path)
        if g.branch_exists(branch):
            g.run("checkout", "-q", "-f", branch, cwd=path)
        else:
            g.run("checkout", "-q", "-f", "-b", branch, self.cfg.base_branch, cwd=path)
        g.run(*clean, cwd=path)
        if left := g.run("status", "--porcelain", cwd=path).stdout.strip():
            raise GitError(f"the worktree {path} is not clean after a reset: {left[:300]}")

    def detach(self, path: Path) -> None:
        """Off its branch (so the branch can be deleted or checked out elsewhere), files kept."""
        self.git.run("checkout", "-q", "--detach", cwd=path, check=False)

    def release_branch(self, branch: str, keep: Path | None = None) -> None:
        """``branch`` checked out nowhere but ``keep``: a slot or a ticket's worktree that has it
        and that no assignment works in is detached. Git checks a branch out in one worktree at a
        time, so a ticket that comes back to a different slot, or a branch the gate deletes,
        needs this. A worktree in use, or one outside the task system's state, is not touched:
        that is an error."""
        live = {str(Path(a["workspace"]).resolve()) for a in self.store.assignments().values() if a.get("workspace")}
        ours = (self.project.pool_dir.resolve(), self.project.worktrees_dir.resolve())
        for wt, b in self._checked_out():
            if b != branch or (keep is not None and wt == keep.resolve()):
                continue
            if str(wt) in live or not any(wt.is_relative_to(d) for d in ours):
                raise GitError(f"{branch} is checked out in {wt}, which is in use")
            self.detach(wt)

    def _checked_out(self) -> list[tuple[Path, str]]:
        """(worktree, branch) for each worktree of the repository that has a branch checked out."""
        out, path = [], None
        for line in self.git.run("worktree", "list", "--porcelain").stdout.splitlines():
            if line.startswith("worktree "):
                path = Path(line[len("worktree "):]).resolve()
            elif line.startswith("branch refs/heads/") and path is not None:
                out.append((path, line[len("branch refs/heads/"):]))
        return out

    def _is_worktree(self, path: Path) -> bool:
        if not path.is_dir():
            return False
        top = self.git.run("rev-parse", "--show-toplevel", cwd=path, check=False)
        return top.returncode == 0 and Path(top.stdout.strip()).resolve() == path.resolve()

    def _make(self, path: Path) -> None:
        self.log(f"worktree pool: making {path}")
        if path.exists():                       # a directory that is no longer a worktree
            shutil.rmtree(path)
        self.git.run("worktree", "prune", check=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git.run("worktree", "add", "--detach", str(path), self.cfg.base_branch)
