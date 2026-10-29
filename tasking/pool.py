"""Reused worktrees (``[tasks] reuse_worktrees = true``, code mode): instead of a worktree made
for each ticket and removed when it lands, the task system keeps at most ``worktree_pool`` of
them under ``<state>/pool/<n>/`` and resets one for each turn: a worker's, a reviewer's, a gate
run's. A repository whose checkout is large, with ignored build caches that take long to make, is
the reason: a reset keeps the ignored files unless ``worktree_clean_ignored`` says otherwise.

With ``[tasks] worktree_paths`` the slots are worktrees the owner already has, adopted
(tasking/adopt.py): nothing is made under ``<state>/pool/``, a listed worktree is used only once a
reset would lose nothing in it, and none is ever removed or made.

Which slot is held by whom is the store's record (``Runtime.claim_slot``); this module makes the
worktree on disk match what a holder needs. SPEC.md section 5."""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from pathlib import Path

from . import adopt
from .config import Project
from .git import Git, GitError
from .setup import Setup, SetupError
from .store import LocalStore

RECHECK_S = 60.0          # a refused worktree is looked at again at most this often


class Pool:
    def __init__(self, project: Project, store: LocalStore, git: Git, log: Callable[[str], None] = print,
                 attention: Callable[[str], None] | None = None, setup: Setup | None = None):
        self.project, self.store, self.git, self.log = project, store, git, log
        self.attention = attention                   # the scheduler's: a refused worktree is the supervisor's to raise
        self.setup = setup                           # [tasks] worktree_setup, run in a slot before its first use
        self._looked: dict[str, float] = {}          # refused path -> when it was last looked at

    @property
    def cfg(self):
        return self.project.config

    def claim(self, holder: str, ticket: str, prefer: str | None = None) -> Path | None:
        """A worktree for ``holder`` (made if the slot is new or its worktree is gone), or None
        when the pool is full: the caller waits. Its contents are as the last holder left them
        until ``reset``. With adopted worktrees, only those ``survey`` finds usable are given, and
        none is ever made. A slot is first prepared by ``[tasks] worktree_setup``; one whose setup
        fails is not given (another is, or None: the turn waits)."""
        usable = self.survey() if self.cfg.worktree_paths else self._give_back()
        unready: set[str] = set()                    # slots whose setup failed: never used unprepared
        while True:
            got = self.store.claim_slot(holder, ticket, self.cfg.worktree_pool, prefer, paths=usable, skip=unready)
            if got is None:
                return None
            slot, new = got
            path = Path(slot["path"])
            try:
                if usable is not None:
                    if bad := adopt.invalid(path, self.project.repo_root):
                        raise GitError(bad)
                elif new or not self._is_worktree(path):
                    self._make(path)
                if self.setup is not None:
                    self.setup.ensure(path)
            except SetupError:
                self.store.release_slot(holder)      # kept, unprepared: tried again after its back-off
                unready.add(str(path))
                continue
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

    def survey(self) -> dict[str, str]:
        """The adopted worktrees the pool may use now, by slot name (their place in the list,
        from 1). A listed worktree marked as this project's is the pool's. One not marked yet is
        adopted (marked) when a reset would lose nothing in it, else refused: never reset, said
        once in the log and to the supervisor, and looked at again at most every ``RECHECK_S``
        until it is clean. A free slot whose path is no longer listed is forgotten, and its
        marker removed: the worktree is the owner's again, as the pool last left it."""
        key, root = self.project.key, self.project.repo_root
        listed = {str(i): str(p) for i, p in enumerate(self.project.adopted_paths, 1)}
        slots = self.store.pool()
        for name, s in slots.items():
            if s["free"] and listed.get(name) != s["path"]:
                self.store.forget_slot(name)
                if s.get("adopted") and s["path"] not in listed.values():
                    adopt.unmark(Path(s["path"]), key)
                    self.log(f"worktree pool: {s['path']} is no longer listed; it is yours again")
        held = {s["path"] for s in slots.values() if not s["free"]}
        for gone in set(self.store.pool_refused()) - set(listed.values()):
            self.store.set_pool_refused(gone, None)
        usable: dict[str, str] = {}
        for name, path in listed.items():
            if path in held:
                usable[name] = path
                continue
            if path in self._looked and time.monotonic() - self._looked[path] < RECHECK_S:
                continue
            why = adopt.refusal(Path(path), root, key)
            if why is None and adopt.owner(Path(path)) is None:
                try:
                    adopt.mark(Path(path), key)
                    self.log(f"worktree pool: adopted {path}; it is the pool's while Orchestra runs")
                except OSError as e:
                    why = f"{path} could not be marked as the pool's: {e}"
            if why is None:
                self._looked.pop(path, None)
                self.store.set_pool_refused(path, None)
                usable[name] = path
                continue
            self._looked[path] = time.monotonic()
            if self.store.set_pool_refused(path, why):
                self.log(f"worktree pool: refused {why}")
                if self.attention:
                    self.attention(f"worktree pool: {why}")
        return usable

    def _give_back(self) -> None:
        """No worktrees listed (any more): the free slots that were adopted are forgotten and
        their markers removed, so the pool makes its own again. Always None (no listed paths)."""
        for name, s in self.store.pool().items():
            if s.get("adopted") and s["free"]:
                self.store.forget_slot(name)
                adopt.unmark(Path(s["path"]), self.project.key)
                self.log(f"worktree pool: {s['path']} is no longer listed; it is yours again")

    def adopted(self) -> set[Path]:
        """The listed worktrees marked as this project's: the pool's to detach and reset."""
        return {p for p in self.project.adopted_paths if adopt.owner(p) == self.project.key}

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
        that is an error. An adopted worktree (``[tasks] worktree_paths``) is the pool's as any
        other; one of the owner's that is not adopted is not touched."""
        live = {str(Path(a["workspace"]).resolve()) for a in self.store.assignments().values() if a.get("workspace")}
        ours = (self.project.pool_dir.resolve(), self.project.worktrees_dir.resolve())
        adopted = self.adopted() if self.cfg.worktree_paths else set()
        for wt, b in self._checked_out():
            if b != branch or (keep is not None and wt == keep.resolve()):
                continue
            if str(wt) in live or not (wt in adopted or any(wt.is_relative_to(d) for d in ours)):
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
        if not path.resolve().is_relative_to(self.project.pool_dir.resolve()):
            raise GitError(f"the pool makes worktrees only under {self.project.pool_dir}, not {path}")
        self.log(f"worktree pool: making {path}")
        if path.exists():                       # a directory that is no longer a worktree
            shutil.rmtree(path)
        self.git.run("worktree", "prune", check=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git.run("worktree", "add", "--detach", str(path), self.cfg.base_branch)
