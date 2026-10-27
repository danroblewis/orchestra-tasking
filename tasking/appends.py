"""A merge conflict the gate resolves itself: two tickets that each appended whole tables to
``orchestra.toml`` (a ``[[collect]]``, ``[[scrape]]`` or ``[[machines]]`` entry, say) at the same
place, the end of the file. Git cannot order two additions at one spot and calls it a conflict;
keeping both, the base branch's first, is what both tickets meant.

Only that case, and nothing that merely looks like it: the conflicted files are exactly
``orchestra.toml``; in every conflicted region the common ancestor has nothing (both sides only
added lines there); each side's addition is whole tables (its first line that is not blank or a
comment is a table header, so no key lands in the other side's table), or both sides began with
the same array-of-tables header (``[[collect]]``) just above the region, which git took as
context, in which case each side gets its own copy of that header; and the result parses as
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


def _ended(lines: list[str]) -> list[str]:
    """``lines`` with a newline at the end of the last one."""
    return [*lines[:-1], lines[-1] + "\n"] if lines and not lines[-1].endswith("\n") else lines


def _spaced(lines: list[str]) -> list[str]:
    """``lines``, ending with a newline and then a blank line, so a table header can follow."""
    lines = _ended(lines)
    return lines if not lines or not lines[-1].strip() else [*lines, "\n"]


def _last_text(lines: list[str]) -> str | None:
    return next((ln.strip() for ln in reversed(lines) if ln.strip()), None)


def _drop_last_text(lines: list[str]) -> list[str]:
    """``lines`` without their last line that is not blank, nor the blank lines after it."""
    i = max(i for i, ln in enumerate(lines) if ln.strip())
    return lines[:i]


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
            if any(ln.strip() for ln in sides["base"]):
                return None
            ours, theirs = _ended(sides["ours"]), sides["theirs"]
            if _whole_tables(ours) and _whole_tables(theirs):
                out += ours + theirs
            else:
                # both sides began their entries with the [[...]] header just above, and git took
                # that header as context: each side's first entry is keys under it
                header = out[-1] if out and out[-1].strip().startswith("[[") else None
                if header is None:
                    return None
                tail = [_last_text(s) == header.strip() for s in (ours, theirs)]
                if all(tail):                    # each side also ends with the header the context after it lacks
                    ours, theirs = _ended(_drop_last_text(ours)), _drop_last_text(theirs)
                    out += _spaced(ours) + [header] + _ended(theirs) + [header]
                elif not any(tail):              # the context after the region brings its own header
                    out += _spaced(ours) + [header] + theirs
                else:
                    return None
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
