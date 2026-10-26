"""work = "code", end to end: the real scheduler, store, git and gate, with a machine that fills the
worker and reviewer roles over the bus (tests/fake_machine.py). A branch per ticket, the reviewer
gets the commit and the unit tests' output in its assignment, the gate lands it."""

import threading
import time

import pytest
from conftest import git
from fake_machine import FakeMachine

from tasking import scheduler as scheduler_mod
from tasking.events import Events
from tasking.model import State
from tasking.scheduler import Scheduler
from tasking.store import LocalStore


@pytest.fixture
def env(project):
    repo = project.repo_root
    toml = repo / "orchestra.toml"
    toml.write_text(toml.read_text().replace('work = "tasks"', 'work = "code"'))   # the code path: a branch per ticket, the gate
    project.reload_config()
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "code mode", cwd=repo)
    events = Events(project.events_file, source="task_scheduler")
    store = LocalStore(project, events)
    ms = store.create_milestone(name="m")
    return project, store, events, ms, repo


def run_scheduler(project, store, events, **behaviour):
    with FakeMachine(events, **behaviour) as m:
        Scheduler(project, store, events).run(once=True)
    return m


def test_ticket_goes_open_to_done(env):
    project, store, events, ms, repo = env
    t = store.create_ticket(title="Do the thing", milestone_id=ms.id, description="x", acceptance="y")
    m = run_scheduler(project, store, events)
    t = store.get_ticket(t.id)
    assert t.state == State.DONE
    assert t.worker_turns == 1 and t.reviewer_rejections == 0 and t.gate_rejections == 0
    assert "WORK.md" in git("ls-tree", "--name-only", "main", cwd=repo)
    assert f"Merge {t.id}" in git("log", "-1", "--format=%s", "main", cwd=repo)
    assert not (project.worktrees_dir / t.id).exists()
    assert not git("branch", "--list", "orchestra/*", cwd=repo).strip()
    assert store.gate_runs()[-1]["status"] == "landed"
    work, review = m.offers
    assert work["unit_command"].startswith("test -f README.md") and work["brief"]["id"] == t.id and work["brief"]["title"] == "Do the thing"
    r = review["review"]                                     # what the reviewer is handed: the scheduler verified it
    assert r["summary"] == "added WORK.md line" and len(r["commit"]) == 40 and "[passed]" in r["test_output"]


def test_rejection_loops_back_to_worker(env):
    project, store, events, ms, _repo = env
    t = store.create_ticket(title="Needs two tries", milestone_id=ms.id)
    m = run_scheduler(project, store, events, reject_first=True)
    t = store.get_ticket(t.id)
    assert t.state == State.DONE and t.review_rounds == 1
    assert t.worker_turns == 2 and t.reviewer_rejections == 1 and t.gate_rejections == 0   # re-engaged once, by the reviewer
    assert [n["kind"] for n in store.notes(t.id)].count("review") == 2
    second = [o for o in m.offers if o["role"] == "worker"][1]["brief"]
    assert second["feedback"] == "1. add more" and second["rejected_by"] == "reviewer" and second["worker_turns"] == 2


def test_blocked_worker(env):
    project, store, events, ms, _repo = env
    t = store.create_ticket(title="Blocked one", milestone_id=ms.id)
    run_scheduler(project, store, events, block=True)
    t = store.get_ticket(t.id)
    assert t.state == State.BLOCKED and t.blocked_reason == "need a decision"
    assert (project.worktrees_dir / t.id).exists()  # kept for resumption
    asked = [e for e in events.read(0, limit=1000)[0] if e["type"] == "task.attention_needed"]
    assert asked and asked[-1]["to"] == "coordinator" and t.id in asked[-1]["reason"]


def test_dependency_order(env):
    project, store, events, ms, repo = env
    a = store.create_ticket(title="A", milestone_id=ms.id, priority=5)
    b = store.create_ticket(title="B", milestone_id=ms.id, priority=1, depends_on=[a.id])
    run_scheduler(project, store, events)
    assert store.get_ticket(a.id).state == State.DONE
    assert store.get_ticket(b.id).state == State.DONE
    log = git("log", "--format=%s", "main", cwd=repo).splitlines()
    assert log.index(f"Merge {b.id}: B") < log.index(f"Merge {a.id}: A")  # B landed after A


def test_gate_rejection_reengages_worker_then_lands(env):
    """The whole failed-merge flow: the worker's first attempt passes review but fails the
    gate's tests; the gate sends it back with feedback; the worker is offered it again in the
    same worktree, fixes it, is re-reviewed, and lands."""
    project, store, events, ms, repo = env
    t = store.create_ticket(title="Breaks the build first", milestone_id=ms.id)
    m = run_scheduler(project, store, events, break_first=True)
    t = store.get_ticket(t.id)
    assert t.state == State.DONE
    assert t.review_rounds == 1                       # the gate rejection counted as one round
    assert t.worker_turns == 2 and t.gate_rejections == 1 and t.reviewer_rejections == 0   # re-engaged once, by the gate
    runs = store.gate_runs()
    assert [(r["status"], r["ticket"], r["conflict"]) for r in runs] == [("rejected", t.id, False), ("landed", t.id, False)]
    kinds = [(n["author"], n["kind"]) for n in store.notes(t.id)]
    assert kinds.count(("merge_gate", "gate")) == 2   # the rejection and the landing
    assert kinds.count(("reviewer", "review")) == 2   # reviewed before each gate run
    evs, _ = events.read(0, limit=10000)
    trail = [(e["from"], e["to"]) for e in evs if e["type"] == "ticket.transitioned" and e["ticket"] == t.id]
    assert trail == [("OPEN", "IN_PROGRESS"), ("IN_PROGRESS", "READY_FOR_REVIEW"), ("READY_FOR_REVIEW", "IN_REVIEW"),
                     ("IN_REVIEW", "APPROVED"), ("APPROVED", "MERGING"), ("MERGING", "REJECTED"),
                     ("REJECTED", "IN_PROGRESS"), ("IN_PROGRESS", "READY_FOR_REVIEW"), ("READY_FOR_REVIEW", "IN_REVIEW"),
                     ("IN_REVIEW", "APPROVED"), ("APPROVED", "MERGING"), ("MERGING", "DONE")]
    assert "README.md" in git("ls-tree", "--name-only", "main", cwd=repo)
    assert "WORK.md" in git("ls-tree", "--name-only", "main", cwd=repo)
    first_review = next(o for o in m.offers if o["role"] == "reviewer")["review"]
    assert "[failed]" in first_review["test_output"]   # the reviewer was told the tests fail on the branch


def test_a_shutdown_lets_the_gate_finish_its_run(env):
    """The scheduler stopped while the gate is in its stages: the run lands before anything is
    released, so the ticket is DONE once, not sent back and landed again by the next scheduler."""
    project, store, events, ms, repo = env
    toml = repo / "orchestra.toml"
    toml.write_text(toml.read_text().replace('command = "test -f README.md', 'command = "sleep 3 && test -f README.md'))
    project.reload_config()
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "a slow unit stage", cwd=repo)
    t = store.create_ticket(title="Slow to land", milestone_id=ms.id)
    sched = Scheduler(project, store, events)

    def stop_in_the_gate():
        end = time.time() + 60
        while time.time() < end and not any(r["status"] == "running" for r in store.gate_runs()):
            time.sleep(0.1)
        sched.stop.set()
    threading.Thread(target=stop_in_the_gate, daemon=True).start()
    with FakeMachine(events):
        sched.run(once=True)
    assert store.get_ticket(t.id).state == State.DONE and [r["status"] for r in store.gate_runs()] == ["landed"]
    again = Scheduler(project, store, events)
    again.recover()
    again.tick()
    assert [r["status"] for r in store.gate_runs()] == ["landed"] and store.get_ticket(t.id).state == State.DONE
    assert len([e for e in events.read(0, limit=10000)[0] if e["type"] == "gate.landed"]) == 1
    assert sum("landed on" in n["text"] for n in store.notes(t.id)) == 1
    assert any("waiting for the gate run to finish" in line for line in scheduler_mod.LOG.read_text().splitlines())
