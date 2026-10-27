"""Thin git wrappers: worktrees, branches, merging, landing. No Python git binding."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class Git:
    def __init__(self, repo_root: Path):
        self.root = Path(repo_root)

    def run(self, *args: str, cwd: Path | None = None, check: bool = True,
            timeout: float = 300) -> subprocess.CompletedProcess:
        p = subprocess.run(["git", "-C", str(cwd or self.root), *args], capture_output=True,
                           text=True, timeout=timeout, check=False)
        if check and p.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {p.stderr.strip() or p.stdout.strip()}")
        return p

    def rev_parse(self, ref: str, cwd: Path | None = None) -> str:
        return self.run("rev-parse", ref, cwd=cwd).stdout.strip()

    def branch_exists(self, name: str) -> bool:
        return self.run("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).returncode == 0

    def current_branch(self) -> str:
        return self.run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def is_clean(self) -> bool:
        return self.run("status", "--porcelain", "--untracked-files=no").stdout.strip() == ""

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.run("merge-base", "--is-ancestor", a, b, check=False).returncode == 0

    def worktree_add(self, branch: str, path: Path, base: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.branch_exists(branch):
            self.run("worktree", "add", str(path), branch)
        else:
            self.run("worktree", "add", "-b", branch, str(path), base)

    def worktree_remove(self, path: Path) -> None:
        if path.exists():
            self.run("worktree", "remove", "--force", str(path), check=False)
        self.run("worktree", "prune", check=False)

    def worktree_branch(self, path: Path) -> str | None:
        p = self.run("rev-parse", "--abbrev-ref", "HEAD", cwd=path, check=False)
        return p.stdout.strip() if p.returncode == 0 else None

    def branch_delete(self, name: str) -> None:
        self.run("branch", "-D", name, check=False)

    def merge_no_ff(self, branch_or_commit: str, cwd: Path, message: str) -> subprocess.CompletedProcess:
        return self.run("merge", "--no-ff", "-m", message, branch_or_commit, cwd=cwd, check=False)

    def abort_merge(self, cwd: Path) -> None:
        self.run("merge", "--abort", cwd=cwd, check=False)

    def changed(self, old: str, new: str) -> list[str]:
        """The paths that differ between two commits (both sides of a rename)."""
        out = self.run("diff", "--name-only", "--no-renames", "-z", old, new).stdout
        return [p for p in out.split("\0") if p]

    def untracked(self) -> set[str]:
        """The repository root's untracked files that are not ignored."""
        return {p for p in self.run("ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0") if p}

    def land(self, base: str, commit: str, expected_old: str) -> bool:
        """Fast-forward `base` to `commit`. Refuses if base moved since expected_old. True when the
        root's working tree followed (it has `base` checked out, clean: a fast-forward there);
        False when only the ref moved (it has another branch checked out), so the root's files,
        orchestra.toml among them, are as they were."""
        current = self.rev_parse(base)
        if current != expected_old:
            raise GitError(f"{base} moved from {expected_old[:10]} to {current[:10]} during the gate")
        if not self.is_ancestor(base, commit):
            raise GitError(f"{commit[:10]} is not a descendant of {base}")
        if self.current_branch() == base:
            if not self.is_clean():
                raise GitError(f"{base} is checked out with uncommitted changes; cannot fast-forward")
            if blocked := sorted(set(self.changed(base, commit)) & self.untracked()):
                raise GitError(f"untracked files in the repository root would be overwritten by landing {commit[:10]} on "
                               f"{base}: {', '.join(blocked)}; move them out of the way, then resume the pipeline")
            self.run("merge", "--ff-only", commit)
            return True
        self.run("update-ref", "-m", "tasking: land", f"refs/heads/{base}", commit, expected_old)
        return False
