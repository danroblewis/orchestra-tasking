"""``[tasks] worktree_setup`` (tasking/setup.py), end to end: run once in each worktree the task
system makes or adopts, before its first use, never again on a reset, again when the command
changes; a failure keeps the worktree unused and the ticket waiting, and is said once.

The owner's case is the example: a policy file under ``.claude/`` they edited in the project
checkout only, symlinked into every worktree and hidden from git with ``skip-worktree``. It must
survive the pool's resets, never be committed by a worker, and reach the gate's checks."""

from pathlib import Path

import pytest
from conftest import UNIT, asked, git, run_scheduler

from tasking.assignments import ASSIGNED
from tasking.model import State
from tasking.scheduler import Scheduler

POLICY = ".claude/policy.yaml"
OWNERS = "ln -sf $ORCHESTRA_PROJECT/.claude/policy.yaml .claude/policy.yaml && git update-index --skip-worktree .claude/policy.yaml"
CHECKED = UNIT + f" && grep -q edited {POLICY}"          # the gate's unit stage fails on the unedited policy


@pytest.mark.parametrize("tasks", ['worktree_paths = ["../worktree_2", "../worktree_3"]',      # adopted slots
                                   "reuse_worktrees = true\nworktree_pool = 2",               # slots the pool makes
                                   ""],                                                       # a worktree per ticket, the gate's own
                         ids=["adopted", "pool", "per-ticket"])
def test_the_owners_policy_file_reaches_every_turn_and_every_gate_run_and_is_never_committed(owner_layout, tasks):
    project, store, events, _code = owner_layout(f"{tasks}\nworktree_setup = \"{OWNERS}\"", files={POLICY: "policy: upstream\n"}, unit=CHECKED)
    (project.repo_root / POLICY).write_text("policy: edited\n")            # edited in the project checkout only,
    git("update-index", "--skip-worktree", POLICY, cwd=project.repo_root)    # and hidden there too (it is on main)
    ms = store.create_milestone(src="planner", name="m")
    ts = []
    for i in range(3):
        ts.append(store.create_ticket(src="planner", title=f"ticket {i}", milestone_id=ms.id, depends_on=[ts[-1].id] if ts else []))
    seen = []
    run_scheduler(project, store, events, look=lambda a, p: seen.append(
        ((p / POLICY).read_text(), git("status", "--porcelain", cwd=p), a["workspace"])))
    assert all(store.get_ticket(t.id).state == State.DONE for t in ts), store.gate_runs()
    assert seen and all(text == "policy: edited\n" and status == "" for text, status, _ in seen), seen
    assert all(r["status"] == "landed" for r in store.gate_runs())          # the gate's stage saw the edited policy
    assert git("show", f"main:{POLICY}", cwd=project.repo_root) == "policy: upstream\n"   # never committed by a worker
    log = (project.logs_dir / "scheduler.log").read_text()
    ran = log.count("worktree setup: running in")
    if tasks:                                                                # once per slot, never on a reset
        assert ran == len({w for *_, w in seen} | {s["path"] for s in store.pool().values()}) <= 2, log
    else:                                                                    # each ticket's worktree (each gate run's too:
        assert ran == len(ts), log                                           # its passing stage says so)


def test_a_failing_setup_keeps_the_ticket_waiting_says_so_once_and_runs_again_only_when_needed(owner_layout):
    project, store, events, _code = owner_layout('reuse_worktrees = true\nworktree_pool = 1\nworktree_setup = "echo boom >&2; exit 3"')
    ms = store.create_milestone(src="planner", name="m")
    t = store.create_ticket(src="planner", title="waits", milestone_id=ms.id)
    sched = Scheduler(project, store, events)
    sched.recover()
    sched.tick()
    sched.tick()
    log = lambda: (project.logs_dir / "scheduler.log").read_text()
    assert not [e for e in events.after(0)[0] if e["type"] == ASSIGNED] and store.get_ticket(t.id).state == State.OPEN
    assert log().count("worktree setup: running in") == 1 and "(exit 3)" in log() and "boom" in log()
    said = [a["reason"] for a in asked(events, "supervisor") if "worktree_setup failed" in a["reason"]]
    assert len(said) == 1 and "boom" in said[0]
    assert all(s["free"] for s in store.pool().values())                     # the slot is not held unprepared
    # the supervisor fixes the command: the next try (after the back-off) prepares the slot, and the ticket goes out
    toml = project.repo_root / "orchestra.toml"
    toml.write_text(toml.read_text().replace("echo boom >&2; exit 3", "mkdir -p cache && echo ready > cache/prepared"))
    project.reload_config()
    sched.setup._failed.clear()                                              # (the back-off has run out)
    sched.tick()
    offer = [e for e in events.after(0)[0] if e["type"] == ASSIGNED][-1]
    assert (Path(offer["workspace"]) / "cache" / "prepared").read_text() == "ready\n"
    assert log().count("worktree setup: running in") == 2
    store.drop_assignment(offer["assignment"])
    sched.pool.claim_for("again", t.id, offer["branch"])                    # claimed and reset again: not run again
    assert log().count("worktree setup: running in") == 2
    store.release_slot("again")
    toml.write_text(toml.read_text().replace("echo ready", "echo changed"))  # the command changed: it runs again
    project.reload_config()
    sched.pool.claim("once-more", t.id)
    assert log().count("worktree setup: running in") == 3
    assert (Path(offer["workspace"]) / "cache" / "prepared").read_text() == "changed\n"


def test_a_failing_setup_of_a_tickets_own_worktree_keeps_it_waiting(owner_layout):
    project, store, events, _code = owner_layout('worktree_setup = "exit 1"')
    ms = store.create_milestone(src="planner", name="m")
    t = store.create_ticket(src="planner", title="waits", milestone_id=ms.id)
    sched = Scheduler(project, store, events)
    sched.recover()
    sched.tick()
    sched.tick()
    assert not [e for e in events.after(0)[0] if e["type"] == ASSIGNED] and store.get_ticket(t.id).state == State.OPEN
    assert (project.logs_dir / "scheduler.log").read_text().count("worktree setup: running in") == 1
