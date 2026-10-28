"""A thin wrapper of git-spice (https://abhinav.github.io/git-spice/), the stacking tool behind
``[gate] stacking = "git-spice"`` (tasking/stacks.py). git-spice keeps the branch tree in the
repository itself (``refs/spice/data``): each tracked branch's base and the base's commit it was
last stacked on. The task system reads that tree and asks git-spice to track and restack; it never
asks it to submit, sync or merge (those reach the remote, and in external review they are the
owner's: ``git-spice stack submit`` opens the stack's pull requests with the bases right).

What git-spice is never asked here, and why (found by trying it, 0.33.0):

- a restack from the repository root: ``upstack restack`` checks every branch out in the current
  worktree and, when one is checked out in another worktree, skips it with a warning and exit 0, and
  may leave the root on another branch. Every restack here runs ``branch restack`` for one branch,
  in a worktree that has that branch (the ticket's own) or in the task system's stack worktree;
- ``repo sync``: it needs the forge's API and moves the local trunk. A squash merge is learnt from
  the pull-request bridge's ``github.pr.merged`` instead (tasking/external.py, tasking/stacks.py).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .git import GitError

COMMAND = "git-spice"


@dataclass
class Restacked:
    ok: bool
    conflict: list[str]          # the conflicted paths when it stopped on a conflict (aborted since)
    output: str


class Spice:
    def __init__(self, repo_root: Path, trunk: str, command: str = COMMAND):
        self.root, self.trunk, self.command = Path(repo_root), trunk, command

    def available(self) -> bool:
        return shutil.which(self.command) is not None

    def run(self, *args: str, cwd: Path | None = None, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess:
        p = subprocess.run([self.command, "--no-prompt", *args], cwd=str(cwd or self.root), capture_output=True, text=True,
                           timeout=timeout, check=False)
        if check and p.returncode != 0:
            raise GitError(f"{self.command} {' '.join(args)}: {(p.stderr or p.stdout).strip()[-1500:]}")
        return p

    def ensure(self) -> None:
        """The repository initialised for git-spice with the base branch as its trunk, once (an
        initialised repository is left as it is: its owner may use git-spice too)."""
        has = subprocess.run(["git", "-C", str(self.root), "show-ref", "--verify", "--quiet", "refs/spice/data"], check=False)
        if has.returncode != 0:
            self.run("repo", "init", "--trunk", self.trunk)

    def tree(self) -> dict[str, dict]:
        """Every tracked branch: {name: {"base": its base, "needs_restack": bool, "worktree": path or None}}.
        git-spice notices here, too, a tracked branch deleted out of band (a landed ticket's) and
        moves the branches on it onto that branch's base, keeping the commit they were stacked on."""
        out = {}
        for line in self.run("log", "short", "--all", "--json").stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            b = json.loads(line)
            down = b.get("down") or {}
            if b.get("name") and down.get("name"):
                out[b["name"]] = {"base": down["name"], "needs_restack": bool(down.get("needsRestack")), "worktree": b.get("worktree")}
        return out

    def track(self, branch: str, base: str) -> None:
        """``branch`` tracked with ``base`` as its base (tracked again when it was: the base's commit
        recorded as it stands now)."""
        self.run("branch", "track", branch, "--base", base)

    def restack(self, branch: str, cwd: Path) -> Restacked:
        """``branch`` rebased onto its base's head, in the worktree ``cwd`` (which git-spice leaves
        on ``branch``). A conflict is aborted at once and the conflicted paths returned: the
        branch is as it was."""
        p = self.run("branch", "restack", "--branch", branch, cwd=cwd, check=False)
        text = (p.stdout + p.stderr).strip()
        if p.returncode == 0:
            return Restacked(True, [], text)
        files = subprocess.run(["git", "-C", str(cwd), "diff", "--name-only", "--diff-filter=U"], capture_output=True, text=True,
                               check=False).stdout.split()
        self.run("rebase", "abort", cwd=cwd, check=False)
        subprocess.run(["git", "-C", str(cwd), "rebase", "--abort"], capture_output=True, check=False)
        if not files:                       # it failed for another reason: not a conflict for the worker to resolve
            raise GitError(f"{self.command} branch restack {branch}: {text[-1500:]}")
        return Restacked(False, files, text)
