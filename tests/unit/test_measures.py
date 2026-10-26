"""The task system's metrics: levels and the counters that move them, from the same change."""

import subprocess

import pytest
from orchestra.metrics import for_project as metrics_for

from tasking.config import init_project
from tasking.events import Events
from tasking.model import State
from tasking.store import LocalStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ORCHESTRA_METRICS_URL", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    p = init_project(repo, prefix="T")
    return LocalStore(p, Events(p.events_file, source="worker/2"))


def test_ticket_levels_and_flows_balance(store):
    m = metrics_for(store.project.orchestra)
    key = store.project.key
    ms = store.create_milestone(name="m")
    a = store.create_ticket(title="a", milestone_id=ms.id)
    store.create_ticket(title="b", milestone_id=ms.id)
    a = store.transition(a.id, a.version, State.IN_PROGRESS)
    store.transition(a.id, a.version, State.READY_FOR_REVIEW)
    level = lambda s: m.latest("tasks_tickets", {"project": key, "state": s, "machine": "task_scheduler"})[1]
    assert (level("OPEN"), level("IN_PROGRESS"), level("READY_FOR_REVIEW"), level("DONE")) == (1, 0, 1, 0)
    assert m.latest("tasks_tickets_created_total", {"project": key, "machine": "worker"})[1] == 2       # worker/2 is the machine worker
    moved = lambda f, t: m.latest("tasks_ticket_transitions_total", {"project": key, "from": f, "to": t, "machine": "worker"})[1]
    assert moved("OPEN", "IN_PROGRESS") == 1 and moved("IN_PROGRESS", "READY_FOR_REVIEW") == 1
    # the counters live with the tickets, so another process (or a restart) carries on from them
    again = LocalStore(store.project, Events(store.project.events_file, source="worker/1"))
    again.create_ticket(title="c", milestone_id=ms.id)
    assert m.latest("tasks_tickets_created_total", {"project": key, "machine": "worker"})[1] == 3


def test_agent_turns(store):
    m = metrics_for(store.project.orchestra)
    store.set_session("worker-T-1", kind="worker", status="running")
    store.measure_turn("worker", "worker/1", started=True)
    assert m.latest("tasks_agents_working", {"project": store.project.key, "role": "worker", "machine": "worker"})[1] == 1
    store.set_session("worker-T-1", status="idle")
    store.measure_turn("worker", "worker/1", started=False)
    assert m.latest("tasks_agents_working", {"project": store.project.key, "role": "worker", "machine": "worker"})[1] == 0
    assert m.latest("tasks_agent_turns_finished_total", {"project": store.project.key, "machine": "worker"})[1] == 1
