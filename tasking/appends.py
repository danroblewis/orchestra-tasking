"""A merge conflict the gate resolves itself: two tickets that each appended whole tables to
``orchestra.toml`` (a ``[[collect]]``, ``[[scrape]]`` or ``[[machines]]`` entry, say) at the same
place, the end of the file. Git cannot order two additions at one spot and calls it a conflict;
keeping both, the base branch's first, is what both tickets meant.

Only that case, and nothing that merely looks like it: the conflicted files are exactly
``orchestra.toml``; in every conflicted region the common ancestor has nothing (both sides only
added lines there); each side's addition is whole tables (its first line that is not blank or a
comment is a table header, so no key lands in the other side's table); and the result parses as
TOML. Anything else stays a conflict, sent back to the ticket's worker as before. The gate's
stages then run on the merged result, as on any merge."""

from __future__ import annotations

import tomllib
from pathlib import Path

from .git import Git

CONFIG = "orchestra.toml"


def _whole_tables(lines: list[str]) -> bool:
    first = next((ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")), None)
    return first is None or first.startswith("[")


def union_of_appends(merged: str) -> str | None:
    """The file with each conflicted region replaced by both sides (ours first), when every
    region is an addition of whole tables on both sides over nothing; None otherwise. ``merged``
    is the file as ``git merge`` left it with ``merge.conflictStyle=diff3`` (the ancestor's part
    between ``|||||||`` and ``=======``)."""
    out: list[str] = []
    part: str | None = None                          # None outside a region, else "ours" | "base" | "theirs"
    sides: dict[str, list[str]] = {}
    for line in merged.splitlines(keepends=True):
        mark = line[:7]
        if part is None and mark == "<<<<<<<":
            part, sides = "ours", {"ours": [], "base": [], "theirs": []}
        elif part == "ours" and mark == "|||||||":
            part = "base"
        elif part in ("ours", "base") and mark == "=======":
            if part == "ours":
                return None                          # no ancestor part: not diff3 output, cannot tell
            part = "theirs"
        elif part == "theirs" and mark == ">>>>>>>":
            if any(ln.strip() for ln in sides["base"]) or not (_whole_tables(sides["ours"]) and _whole_tables(sides["theirs"])):
                return None
            ours = sides["ours"]
            if ours and not ours[-1].endswith("\n"):
                ours = [*ours[:-1], ours[-1] + "\n"]
            out += ours + sides["theirs"]
            part = None
        elif part is not None:
            sides[part].append(line)
        else:
            out.append(line)
    if part is not None:
        return None                                  # an unterminated region
    text = "".join(out)
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    return text


def resolve_appends(git: Git, cwd: Path) -> bool:
    """In a merge that stopped on conflicts in ``cwd``: when the only conflicted file is
    orchestra.toml and its conflicts are appended tables on both sides, write both, and commit
    the merge with its message. True when the merge is now committed; False leaves it as it was
    (the caller aborts it)."""
    conflicted = [p for p in git.run("diff", "--name-only", "--diff-filter=U", "-z", cwd=cwd).stdout.split("\0") if p]
    if conflicted != [CONFIG]:
        return False
    # the conflicted file again, with the ancestor's part, which the default style leaves out
    git.run("checkout", "--conflict=diff3", "--", CONFIG, cwd=cwd)
    text = union_of_appends((cwd / CONFIG).read_text(encoding="utf-8"))
    if text is None:
        return False
    (cwd / CONFIG).write_text(text, encoding="utf-8")
    git.run("add", "--", CONFIG, cwd=cwd)
    return git.run("commit", "--no-edit", cwd=cwd, check=False).returncode == 0
