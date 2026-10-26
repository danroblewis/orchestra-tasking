"""End-to-end: the real scheduler, store, git, and gate, with a machine that fills the worker and
reviewer roles over the bus (tests/fake_machine.py). The code path: a branch per ticket, the
reviewer gets the commit and the unit tests' output in its assignment, the gate lands it."""

import subprocess

import pytest
from fake_machine import FakeMachine

from tasking.config import init_project
from tasking.events import Events
from tasking.loop import Loop
from tasking.model import State
from tasking.store import LocalStore


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "t@t", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / "README.md").write_text("# e2e\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "init", cwd=repo)
    project = init_project(repo, prefix="E2E", unit_command="test -f README.md")
    toml = repo / "orchestra.toml"
    toml.write_text(toml.read_text().replace('work = "tasks"', 'work = "code"'))   # the code path: a branch per ticket, the gate
    project.reload_config()
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "orchestra config", cwd=repo)
    monkeypatch.setenv("ORCHESTRA_REPO", str(repo))
    events = Events(project.events_file)
    store = LocalStore(project, events)
    ms = store.create_milestone(name="m")
    return project, store, events, ms, repo


def run_loop(project, store, events, **behaviour):
    with FakeMachine(events, **behaviour) as m:
        Loop(project, store, events).run(once=True)
    return m


def test_ticket_goes_open_to_done(env):
    project, store, events, ms, repo = env
    t = store.create_ticket(title="Do the thing", milestone_id=ms.id, description="x", acceptance="y")
    m = run_loop(project, store, events)
    t = store.get_ticket(t.id)
    assert t.state == State.DONE
    assert t.worker_turns == 1 and t.reviewer_rejections == 0 and t.gate_rejections == 0
    assert "WORK.md" in git("ls-tree", "--name-only", "main", cwd=repo)
    assert "Merge E2E-1" in git("log", "-1", "--format=%s", "main", cwd=repo)
    assert not (project.worktrees_dir / t.id).exists()
    assert not git("branch", "--list", "orchestra/*", cwd=repo).strip()
    assert store.gate_runs()[-1]["status"] == "landed"
    work, review = m.offers
    assert work["unit_command"] == "test -f README.md" and work["brief"]["id"] == t.id and work["brief"]["title"] == "Do the thing"
    r = review["review"]                                     # what the reviewer is handed: the scheduler verified it
    assert r["summary"] == "added WORK.md line" and len(r["commit"]) == 40 and "[passed]" in r["test_output"]


def test_rejection_loops_back_to_worker(env):
    project, store, events, ms, _repo = env
    t = store.create_ticket(title="Needs two tries", milestone_id=ms.id)
    m = run_loop(project, store, events, reject_first=True)
    t = store.get_ticket(t.id)
    assert t.state == State.DONE and t.review_rounds == 1
    assert t.worker_turns == 2 and t.reviewer_rejections == 1 and t.gate_rejections == 0   # re-engaged once, by the reviewer
    assert [n["kind"] for n in store.notes(t.id)].count("review") == 2
    second = [o for o in m.offers if o["role"] == "worker"][1]["brief"]
    assert second["feedback"] == "1. add more" and second["rejected_by"] == "reviewer" and second["worker_turns"] == 2


def test_blocked_worker(env):
    project, store, events, ms, _repo = env
    t = store.create_ticket(title="Blocked one", milestone_id=ms.id)
    run_loop(project, store, events, block=True)
    t = store.get_ticket(t.id)
    assert t.state == State.BLOCKED and t.blocked_reason == "need a decision"
    assert (project.worktrees_dir / t.id).exists()  # kept for resumption
    asked = [e for e in events.read(0, limit=1000)[0] if e["type"] == "task.attention_needed"]
    assert asked and asked[-1]["to"] == "coordinator" and t.id in asked[-1]["reason"]


def test_dependency_order(env):
    project, store, events, ms, repo = env
    a = store.create_ticket(title="A", milestone_id=ms.id, priority=5)
    b = store.create_ticket(title="B", milestone_id=ms.id, priority=1, depends_on=[a.id])
    run_loop(project, store, events)
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
    m = run_loop(project, store, events, break_first=True)
    t = store.get_ticket(t.id)
    assert t.state == State.DONE
    assert t.review_rounds == 1                       # the gate rejection counted as one round
    assert t.worker_turns == 2 and t.gate_rejections == 1 and t.reviewer_rejections == 0   # re-engaged once, by the gate
    runs = store.gate_runs()
    assert [r["status"] for r in runs] == ["rejected", "landed"]
    assert runs[0]["rejected"] == [t.id] and runs[1]["merged"] == [t.id]
    kinds = [(n["author"], n["kind"]) for n in store.notes(t.id)]
    assert kinds.count(("merge_gate", "gate")) == 2   # the rejection and the landing
    assert kinds.count(("reviewer", "review")) == 2   # reviewed before each gate run
    evs, _ = events.read(0, limit=10000)
    trail = [(e["from"], e["to"]) for e in evs if e["type"] == "ticket_transition" and e["ticket"] == t.id]
    assert trail == [("OPEN", "IN_PROGRESS"), ("IN_PROGRESS", "READY_FOR_REVIEW"), ("READY_FOR_REVIEW", "IN_REVIEW"),
                     ("IN_REVIEW", "APPROVED"), ("APPROVED", "MERGING"), ("MERGING", "REJECTED"),
                     ("REJECTED", "IN_PROGRESS"), ("IN_PROGRESS", "READY_FOR_REVIEW"), ("READY_FOR_REVIEW", "IN_REVIEW"),
                     ("IN_REVIEW", "APPROVED"), ("APPROVED", "MERGING"), ("MERGING", "DONE")]
    assert "README.md" in git("ls-tree", "--name-only", "main", cwd=repo)
    assert "WORK.md" in git("ls-tree", "--name-only", "main", cwd=repo)
    first_review = next(o for o in m.offers if o["role"] == "reviewer")["review"]
    assert "[failed]" in first_review["test_output"]   # the reviewer was told the tests fail on the branch
