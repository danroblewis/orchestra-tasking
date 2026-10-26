"""work = "tasks": a ticket is a task, done in a scratch folder and reviewed, with nothing merged;
and each agent is its own machine on the bus (fake Claude Code)."""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tasking.config import init_project
from tasking.events import Events
from tasking.loop import Loop
from tasking.model import State
from tasking.store import LocalStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    fake = tmp_path / "claude"
    shutil.copy(Path(__file__).parent / "fake_claude.py", fake)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("CLAUDE_BINARY", str(fake))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    for v in ("FAKE_PI_WORKER_BLOCK", "FAKE_PI_REJECT_FIRST", "FAKE_PI_BREAK_FIRST"):
        monkeypatch.delenv(v, raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    project = init_project(repo, prefix="T")
    toml = repo / "orchestra.toml"
    toml.write_text(toml.read_text().replace('harness = "pi"', 'harness = "claude_code"'))
    project.reload_config()
    assert project.config.work == "tasks"
    monkeypatch.setenv("ORCHESTRA_REPO", str(repo))
    events = Events(project.events_file)
    return project, LocalStore(project, events), events, repo


def test_a_task_is_done_in_its_folder_and_reviewed_with_nothing_merged(env):
    project, store, events, repo = env
    ms = store.create_milestone(name="m")
    t = store.create_ticket(title="Find the critical temperature", milestone_id=ms.id, description="x", acceptance="y")
    Loop(project, store, events).run(once=True)
    t = store.get_ticket(t.id)
    assert t.state == State.DONE and t.branch is None
    assert (project.workspaces_dir / t.id / "RESULT.md").read_text().startswith(f"# {t.id}")
    assert not subprocess.run(["git", "branch", "--list"], cwd=repo, capture_output=True, text=True).stdout.strip("* main\n")
    assert store.gate_runs() == []


def test_every_agent_is_its_own_machine(env):
    project, store, events, repo = env
    ms = store.create_milestone(name="m")
    store.create_ticket(title="a task", milestone_id=ms.id, description="x", acceptance="y")
    Loop(project, store, events).run(once=True)
    evs, _ = events.bus.read(0, limit=10000)
    sources = {e.get("source") for e in evs}
    assert {"task_scheduler", "supervisor", "coordinator", "worker/1", "reviewer/1"} <= sources
    assert "tasks" not in sources
    started = {e["source"]: e["data"] for e in evs if e["type"] == "orchestra.machine.started"}
    assert started["task_scheduler"]["kind"] == "program"
    assert started["worker/1"] == {**started["worker/1"], "kind": "agent", "mode": "ad hoc", "harness": "claude_code", "role": "worker"}
    assert started["coordinator"]["mode"] == "scheduled" and "ticket_created" in started["coordinator"]["listens_for"]
    turns = [e for e in evs if e["type"] == "session_started" and e["data"]["kind"] == "worker"]
    assert turns and all(e["source"] == "worker/1" for e in turns)
    reports = [e for e in evs if e["type"] == "ticket_transition" and (e["data"] or {}).get("to") == "READY_FOR_REVIEW"]
    assert reports and reports[0]["source"] in ("worker/1", "task_scheduler")


def test_work_is_assigned_over_the_bus_and_the_machine_that_accepts_owns_it(env):
    """The scheduler offers the ticket to a role (task.assigned); the machine that serves it
    accepts (and so owns the ticket), and reports (task.reported); the scheduler applies it.
    docs/proposals/agents-split.md."""
    project, store, events, repo = env
    ms = store.create_milestone(name="m")
    t = store.create_ticket(title="a task", milestone_id=ms.id, description="x", acceptance="y")
    Loop(project, store, events).run(once=True)
    evs = [e for e in events.bus.read(0, limit=10000)[0] if e.get("subject") == t.id]
    proto = [(e["type"], e["source"], (e["data"] or {}).get("role")) for e in evs if e["type"].startswith("task.")]
    assert [p for p in proto if p[0] != "task.working"] == [
        ("task.assigned", "task_scheduler", "worker"), ("task.accepted", "worker/1", "worker"), ("task.reported", "worker/1", "worker"),
        ("task.assigned", "task_scheduler", "reviewer"), ("task.accepted", "reviewer/1", "reviewer"), ("task.reported", "reviewer/1", "reviewer")]
    owners = [(e["data"] or {}).get("owner") for e in evs if e["type"] == "ticket_updated" and "owner" in (e["data"] or {})]
    assert owners == ["worker/1", "reviewer/1"]
    transitions = [(e["data"]["to"], e["source"]) for e in evs if e["type"] == "ticket_transition"]
    assert all(src == "task_scheduler" for _, src in transitions)                        # only the scheduler moves tickets
    assert store.get_ticket(t.id).state.value == "DONE" and store.get_ticket(t.id).owner is None
    assert store.assignments() == {}


def test_an_assignment_nobody_answers_goes_back_when_its_lease_runs_out(env, monkeypatch):
    project, store, events, repo = env
    from tasking import loop as loop_mod
    monkeypatch.setattr(loop_mod, "LEASE_S", 0.0)                                         # no word in time: released at once
    ms = store.create_milestone(name="m")
    t = store.create_ticket(title="a task", milestone_id=ms.id, description="x", acceptance="y")
    lp = Loop(project, store, events)
    lp.runner.ROLES = ()                                                                  # nothing serves the roles
    lp.recover()
    lp.tick()                                                                             # assigned
    first = set(store.assignments())
    lp.tick()                                                                             # the lease ran out: released, and offered again
    released = [e for e in events.bus.read(0, limit=1000)[0] if e["type"] == "task.released"]
    assert released and released[0]["data"]["assignment"] in first and "no word" in released[0]["data"]["reason"]
    assert not first & set(store.assignments())                                           # the old offer is gone, a new one stands
    assert any(n["kind"] == "crash" for n in store.notes(t.id))                           # counted as a crash: no zombie
