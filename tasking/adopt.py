"""Adopted worktrees (``[tasks] worktree_paths``): the worktree pool's slots are worktrees the owner
already has, instead of checkouts the pool makes under ``<state>/tasks/pool/``. For a repository
whose checkout takes minutes and whose disk holds only a few.

A listed path is a worktree of the project's own repository (the same ``git rev-parse
--git-common-dir``), not the project root (the worktree Orchestra runs in), listed once. The pool
resets a slot before every turn, which on a worktree the owner also used would destroy their work;
so a path is *adopted* only when nothing would be lost: no uncommitted change, no untracked
(non-ignored) file, no merge, rebase, cherry-pick, revert or ``am`` in progress. Commits are never
at risk: the pool checks a ticket's branch out and leaves the owner's branch where it is. An
adopted worktree carries a marker in its own git directory (``git rev-parse --git-dir``, never the
working tree), ``orchestra-pool``, holding the project's key; from then on it is the pool's and is
reset like any slot. Without the marker (removed by the owner, or a new path), it is inspected
again before it is used. SPEC.md section 5."""

from __future__ import annotations

import subprocess
from pathlib import Path

MARKER = "orchestra-pool"
IN_PROGRESS = {"MERGE_HEAD": "a merge", "rebase-merge": "a rebase", "rebase-apply": "a rebase or am",
               "CHERRY_PICK_HEAD": "a cherry-pick", "REVERT_HEAD": "a revert"}
SHOWN = 5                                   # the changes named in a refusal; the rest are counted


def resolve(paths: list[str] | tuple[str, ...], root: Path) -> list[Path]:
    """Each listed path as an absolute path: ``~`` expanded, a relative one from the project root."""
    out = []
    for p in paths:
        q = Path(str(p)).expanduser()
        out.append((q if q.is_absolute() else Path(root) / q).resolve())
    return out


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=False, timeout=300)


def _abs(path: Path, what: str) -> Path | None:
    p = _git(path, "rev-parse", "--path-format=absolute", what)
    return Path(p.stdout.strip()).resolve() if p.returncode == 0 and p.stdout.strip() else None


def git_dir(path: Path) -> Path | None:
    """The worktree's own git directory (``<common>/worktrees/<name>`` for a linked one)."""
    return _abs(path, "--git-dir")


def owner(path: Path) -> str | None:
    """The key of the project that adopted ``path`` (its marker), or None."""
    d = git_dir(path)
    if d is None:
        return None
    try:
        return (d / MARKER).read_text().strip() or None
    except OSError:
        return None


def mark(path: Path, key: str) -> None:
    d = git_dir(path)
    if d is None:
        raise OSError(f"{path} has no git directory")
    (d / MARKER).write_text(key + "\n")


def unmark(path: Path, key: str) -> None:
    """The marker removed, when it is ``key``'s: the worktree is the owner's again."""
    d = git_dir(path)
    if d is not None and owner(path) == key:
        (d / MARKER).unlink(missing_ok=True)


def invalid(path: Path, root: Path) -> str | None:
    """Why ``path`` cannot be a slot at all, or None: it must exist, be a worktree's top (not a
    folder inside one, not a bare repository) of the project's own repository, and not be the
    project root."""
    root = Path(root).resolve()
    if not path.is_dir():
        return f"{path} does not exist"
    if path == root:
        return f"{path} is the project root, the worktree Orchestra runs in"
    top = _abs(path, "--show-toplevel") if _git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true" else None
    if top != path:
        return f"{path} is not a git worktree" + (f" (it is inside {top})" if top else "")
    if _abs(path, "--git-common-dir") != _abs(root, "--git-common-dir"):
        return f"{path} is a worktree of another repository (git rev-parse --git-common-dir differs from the project root's)"
    return None


def unsaved(path: Path) -> str | None:
    """What a reset would destroy in ``path``, or None when nothing: an operation in progress,
    uncommitted changes, untracked files that are not ignored."""
    d = git_dir(path)
    ops = [what for name, what in IN_PROGRESS.items() if d is not None and (d / name).exists()]
    st = _git(path, "status", "--porcelain", "--untracked-files=normal")
    if st.returncode != 0:
        return f"git status failed: {(st.stderr or st.stdout).strip()[:200]}"
    lines = [ln for ln in st.stdout.splitlines() if ln.strip()]
    if not ops and not lines:
        return None
    parts = [f"{' and '.join(ops)} in progress"] if ops else []
    changed = [ln[3:] for ln in lines if not ln.startswith("??")]
    untracked = [ln[3:] for ln in lines if ln.startswith("??")]
    for n, kind in ((changed, "uncommitted change"), (untracked, "untracked file")):
        if n:
            more = f" and {len(n) - SHOWN} more" if len(n) > SHOWN else ""
            parts.append(f"{len(n)} {kind}{'s' if len(n) != 1 else ''} ({', '.join(n[:SHOWN])}{more})")
    return "; ".join(parts)


def refusal(path: Path, root: Path, key: str | None) -> str | None:
    """Why the pool may not use ``path`` now, or None. An unmarked path is refused while a reset
    would lose anything in it; one marked by another project is refused outright; one marked by
    ``key`` (None: any marker) is the pool's, whatever it holds."""
    if bad := invalid(path, root):
        return bad
    mine = owner(path)
    if mine is not None and key is not None and mine != key:
        return f"{path} is adopted by another Orchestra project ({mine}; its marker {MARKER} in {git_dir(path)})"
    if mine is not None:
        return None
    if what := unsaved(path):
        return (f"{path} is not adopted: it has {what}. Orchestra resets a worktree before every turn and would "
                f"destroy them; commit, stash or remove them (or take it out of [tasks] worktree_paths)")
    return None


def problems(paths: list[str] | tuple[str, ...], root: Path) -> list[str]:
    """What ``[tasks] worktree_paths`` says wrong for the project at ``root`` (check.py): each path
    invalid, listed twice, or (not adopted yet) with work a reset would destroy."""
    out, seen = [], set()
    for raw, p in zip(paths, resolve(paths, root), strict=True):
        if p in seen:
            out.append(f"[tasks] worktree_paths: {raw} is listed twice")
            continue
        seen.add(p)
        if why := refusal(p, root, None):
            out.append(f"[tasks] worktree_paths: {why}")
    return out
