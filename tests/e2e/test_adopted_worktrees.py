"""Adopted worktrees (``[tasks] worktree_paths``), end to end, in the owner's layout: a bare clone
``Code/bare/.git`` and worktrees ``Code/worktree_1..4`` beside it, Orchestra in worktree_1. The
real scheduler, store, git and gate with the fake machine: every turn and every gate run happens in
the listed worktrees, nothing is made under ``<state>/tasks/pool/``, none is removed, and the one
the owner left work in is refused, said to the supervisor, and never touched."""

import pytest
from conftest import asked, git, run_scheduler

from tasking import adopt
from tasking.model import State
from tasking.scheduler import Scheduler
from tasking.status import collect, render


@pytest.fixture
def owner(owner_layout):
    return owner_layout('worktree_paths = ["../worktree_2", "~/Code/worktree_3", "../worktree_4"]')


def test_tickets_run_in_the_owners_worktrees_and_a_dirty_one_is_never_touched(owner):
    project, store, events, code = owner
    wt2, wt3, wt4 = ((code / f"worktree_{i}").resolve() for i in (2, 3, 4))
    (wt4 / "README.md").write_text("the owner's unfinished edit\n")       # worktree_4 has the owner's work in it
    (wt4 / "idea.txt").write_text("not committed\n")
    (wt2 / "cache").mkdir()
    (wt2 / "cache" / "warm").write_text("built\n")                           # ignored: no reason to refuse, and kept
    assert project.config.reuse_worktrees and project.config.worktree_pool == 3
    ms = store.create_milestone(src="planner", name="m")
    ts = []
    for i in range(4):
        ts.append(store.create_ticket(src="planner", title=f"ticket {i}", milestone_id=ms.id, depends_on=[ts[-1].id] if ts else []))
    seen = []
    run_scheduler(project, store, events, look=lambda a, p: seen.append((a["role"], a["workspace"], git("status", "--porcelain", cwd=p))))
    assert all(store.get_ticket(t.id).state == State.DONE for t in ts)
    assert {w for _, w, _ in seen} <= {str(wt2), str(wt3)} and all(s == "" for *_, s in seen)
    assert not project.pool_dir.exists() or not list(project.pool_dir.iterdir())   # nothing made in the state directory
    assert not list(project.gate_dir.glob("*"))
    assert all(r["status"] == "landed" for r in store.gate_runs())
    # the owner's worktrees are all still there, the dirty one exactly as it was left
    listed = git("worktree", "list", "--porcelain", cwd=code / "worktree_1")
    assert all(f"worktree {p}" in listed for p in (wt2, wt3, wt4))
    assert (wt4 / "README.md").read_text() == "the owner's unfinished edit\n" and (wt4 / "idea.txt").exists()
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=wt4).strip() == "mine-4" and adopt.owner(wt4) is None
    assert (wt2 / "cache" / "warm").exists()
    # the adopted ones carry the project's marker in their own git directory, and are left clean, off any ticket branch
    used = {w for _, w, _ in seen} | {s["path"] for s in store.pool().values()}
    assert str(wt2) in used
    for p in (wt2, wt3):
        assert adopt.owner(p) == project.key and git("status", "--porcelain", cwd=p) == ""
        head = git("rev-parse", "--abbrev-ref", "HEAD", cwd=p).strip()
        assert head == ("HEAD" if str(p) in used else f"mine-{p.name[-1]}")   # detached at the last commit it served, or never touched
    assert git("branch", "--list", "--format=%(refname:short)", "mine-2", "mine-3", cwd=wt2).split() == ["mine-2", "mine-3"]   # the owner's branches kept
    # refused once, said to the supervisor once, and shown
    why = store.pool_refused()[str(wt4)]
    assert "1 uncommitted change (README.md)" in why and "1 untracked file (idea.txt)" in why
    said = [a["reason"] for a in asked(events, "supervisor") if "worktree pool" in a["reason"]]
    assert len(said) == 1 and str(wt4) in said[0]
    log = (project.logs_dir / "scheduler.log").read_text()
    assert log.count(f"worktree pool: refused {wt4}") == 1 and f"worktree pool: adopted {wt2}" in log
    shown = render(collect(project, store, events))
    assert "worktree pool: 3 of your worktrees" in shown and f"{wt4}  REFUSED: {wt4} is not adopted" in shown
    assert f"{wt2}  free" in shown


def test_a_cleaned_worktree_is_adopted_and_one_taken_out_of_the_list_is_given_back(owner):
    project, store, events, code = owner
    wt2, wt4 = (code / "worktree_2").resolve(), (code / "worktree_4").resolve()
    (wt4 / "idea.txt").write_text("not committed\n")
    pool = Scheduler(project, store, events).pool
    assert set(pool.survey().values()) == {str(wt2), str((code / "worktree_3").resolve())}
    (wt4 / "idea.txt").unlink()                                             # the owner tidied up
    pool._looked.clear()                                                    # (the minute between looks has passed)
    assert str(wt4) in pool.survey().values() and adopt.owner(wt4) == project.key and not store.pool_refused()
    path = pool.claim("T-1.worker.x", "T-1")
    store.release_slot("T-1.worker.x")
    toml = project.repo_root / "orchestra.toml"
    toml.write_text(toml.read_text().replace('"../worktree_2", ', ""))       # worktree_2 taken out of the list
    project.reload_config()
    pool.survey()
    assert adopt.owner(wt2) is None and str(wt2) not in {s["path"] for s in store.pool().values()}
    assert path is not None


def test_the_ticket_branch_checked_out_in_the_project_root_is_never_taken_from_it(owner):
    """Git checks a branch out in one worktree: a ticket's branch the owner has in worktree_1 (not
    the pool's) makes the turn fail to start, as with the pool's own worktrees, and nothing moves."""
    project, store, events, _code = owner
    sched = Scheduler(project, store, events)
    git("branch", "orchestra/T-9-x", "main", cwd=project.repo_root)
    git("checkout", "-q", "orchestra/T-9-x", cwd=project.repo_root)
    with pytest.raises(Exception, match="is checked out in .*worktree_1, which is in use"):
        sched.pool.claim_for("T-9.worker.x", "T-9", "orchestra/T-9-x")
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=project.repo_root).strip() == "orchestra/T-9-x"
    assert all(s["free"] for s in store.pool().values())
