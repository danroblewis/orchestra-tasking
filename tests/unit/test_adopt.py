"""Adopted worktrees (``[tasks] worktree_paths``, tasking/adopt.py): which listed paths may be the
pool's slots, and that one holding work a reset would destroy is refused, never touched."""

import subprocess

import pytest

from tasking import adopt
from tasking.check import check
from tasking.config import Config


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def code(tmp_path):
    """The owner's layout: one bare clone ``Code/bare/.git`` with worktrees ``Code/worktree_1..3``
    beside it (worktree_1 is where Orchestra runs), and an unrelated repository ``other``. The
    worktrees ignore ``cache/``."""
    src = tmp_path / "src"
    src.mkdir()
    git("init", "-q", "-b", "main", cwd=src)
    git("config", "user.email", "t@t", cwd=src)
    git("config", "user.name", "t", cwd=src)
    (src / "README.md").write_text("# monorepo\n")
    (src / ".gitignore").write_text("cache/\n")
    git("add", "-A", cwd=src)
    git("commit", "-qm", "init", cwd=src)
    root = tmp_path / "Code"
    root.mkdir()
    git("clone", "-q", "--bare", str(src), str(root / "bare" / ".git"), cwd=tmp_path)
    for i in (1, 2, 3):
        git("worktree", "add", "-q", "--detach", f"../../worktree_{i}", "main", cwd=root / "bare" / ".git")
    other = tmp_path / "other"
    other.mkdir()
    git("init", "-q", "-b", "main", cwd=other)
    return root


def test_paths_resolve_from_home_and_the_project_root(code, monkeypatch):
    monkeypatch.setenv("HOME", str(code.parent))
    wt1 = code / "worktree_1"
    assert adopt.resolve(["~/Code/worktree_2", "../worktree_3"], wt1) == [(code / "worktree_2").resolve(), (code / "worktree_3").resolve()]


def test_a_listed_path_must_be_another_worktree_of_the_same_repository(code):
    wt1, wt2 = code / "worktree_1", code / "worktree_2"
    (wt2 / "sub").mkdir()
    assert adopt.invalid(wt2.resolve(), wt1) is None
    assert "does not exist" in adopt.invalid((code / "worktree_9").resolve(), wt1)
    assert "is the project root" in adopt.invalid(wt1.resolve(), wt1)
    assert "is not a git worktree (it is inside" in adopt.invalid((wt2 / "sub").resolve(), wt1)
    assert "another repository" in adopt.invalid((code.parent / "other").resolve(), wt1)
    assert "is not a git worktree" in adopt.invalid(code.parent.resolve(), wt1)


def test_a_worktree_with_work_in_it_is_refused_and_says_what_is_there(code):
    wt1, wt2 = code / "worktree_1", (code / "worktree_2").resolve()
    assert adopt.refusal(wt2, wt1, "k") is None                              # clean: may be adopted
    (wt2 / "cache").mkdir()
    (wt2 / "cache" / "built").write_text("warm\n")                         # ignored files are no reason to refuse
    assert adopt.unsaved(wt2) is None
    (wt2 / "README.md").write_text("the owner's edit\n")
    (wt2 / "notes.txt").write_text("the owner's notes\n")
    why = adopt.refusal(wt2, wt1, "k")
    assert "is not adopted" in why and "1 uncommitted change (README.md)" in why and "1 untracked file (notes.txt)" in why
    assert (wt2 / "README.md").read_text() == "the owner's edit\n" and (wt2 / "notes.txt").exists()   # untouched


def test_an_operation_in_progress_is_work_too(code):
    wt2 = (code / "worktree_2").resolve()
    (adopt.git_dir(wt2) / "MERGE_HEAD").write_text("0" * 40 + "\n")
    assert adopt.unsaved(wt2) == "a merge in progress"


def test_the_marker_is_in_the_worktrees_own_git_directory_and_makes_it_the_pools(code):
    wt1, wt2 = code / "worktree_1", (code / "worktree_2").resolve()
    adopt.mark(wt2, "proj-1")
    assert (adopt.git_dir(wt2) / adopt.MARKER).read_text() == "proj-1\n"
    assert adopt.git_dir(wt2).is_relative_to((code / "bare" / ".git" / "worktrees").resolve())
    assert not (wt2 / adopt.MARKER).exists()                                # nothing in the working tree
    (wt2 / "half-done.txt").write_text("a turn's leftovers\n")
    assert adopt.refusal(wt2, wt1, "proj-1") is None                         # the pool's: reset like any slot
    assert "adopted by another Orchestra project (proj-1" in adopt.refusal(wt2, wt1, "proj-2")
    adopt.unmark(wt2, "proj-2")                                             # not its marker: kept
    assert adopt.owner(wt2) == "proj-1"
    adopt.unmark(wt2, "proj-1")
    assert adopt.owner(wt2) is None and "is not adopted" in adopt.refusal(wt2, wt1, "proj-1")   # inspected again


def test_check_says_what_is_wrong_with_the_listed_worktrees(code):
    wt1 = code / "worktree_1"
    raw = {"tasks": {"worktree_paths": ["../worktree_2", str(code / "worktree_3")]}}
    assert check(raw, wt1) == []
    (code / "worktree_3" / "scratch.txt").write_text("x\n")
    said = check({"tasks": {"worktree_paths": ["../worktree_2", "../worktree_2", "../worktree_3", ".", "../nope"]}}, wt1)
    assert said[0] == "[tasks] worktree_paths: ../worktree_2 is listed twice"
    assert "1 untracked file (scratch.txt)" in said[1]
    assert "is the project root" in said[2] and "does not exist" in said[3] and len(said) == 4
    assert check({"tasks": {"worktree_paths": ["../worktree_2"], "reuse_worktrees": False}}, wt1) == [
        "[tasks] reuse_worktrees = false with worktree_paths: listing worktrees turns the pool on; remove one of the two"]
    assert "is more than the 1 worktree_paths" in check({"tasks": {"worktree_paths": ["../worktree_2"], "worktree_pool": 2}}, wt1)[0]
    assert check({"tasks": {"worktree_paths": "../worktree_2"}}) == ['[tasks] worktree_paths: a list of paths, such as ["~/Code/worktree_2"]']
    assert check({"tasks": {"worktree_paths": ["../worktree_2", "/nowhere"]}}) == []     # no root given: shapes only


def test_listing_worktrees_turns_the_pool_on_and_sizes_it():
    c = Config.from_toml('[tasks]\nworkers = 3\nworktree_paths = ["~/a", "~/b"]\n')
    assert (c.reuse_worktrees, c.worktree_pool, c.worktree_paths) == (True, 2, ("~/a", "~/b"))
    c = Config.from_toml('[tasks]\nworktree_paths = ["~/a", "~/b", "~/c"]\nworktree_pool = 2\n')
    assert c.worktree_pool == 2                                             # fewer: the first ones are used
    assert Config.from_toml('[tasks]\nworktree_paths = ["~/a"]\nworktree_pool = 5\n').worktree_pool == 1
