"""Planners' changes are requests to the task system over the bus (tasking/requests.py)."""

import subprocess

import pytest

from tasking import api
from tasking.config import init_project
from tasking.events import Events
from tasking.requests import Desk
from tasking.store import LocalStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    p = init_project(repo, prefix="T")
    store = LocalStore(p, Events(p.events_file, source="task_scheduler"))
    desk = Desk(p, store, Events(p.events_file, source="task_scheduler"), log=lambda m: None)
    desk.start()
    yield p, store
    desk.stop.set()


def test_an_agents_change_is_a_request_the_scheduler_applies_and_answers(env, monkeypatch):
    p, store = env
    monkeypatch.setenv("TASKING_REQUESTS", "bus")                     # as in an agent's session
    monkeypatch.setenv("ORCHESTRA_SOURCE", "supervisor")
    agent_store = LocalStore(p, Events(p.events_file))                 # the tool's own, in the agent's process
    m = api.handle(p, agent_store, "create_milestone", {"name": "M"}, role="supervisor")
    assert m["ok"] and m["milestone"]["id"] == "M1"
    t = api.handle(p, agent_store, "create_ticket", {"title": "a task", "milestone_id": "M1", "description": "x"}, role="supervisor")
    assert t["ok"] and store.get_ticket(t["ticket"]["id"]).title == "a task"
    bad = api.handle(p, agent_store, "create_ticket", {"title": "lost", "milestone_id": "M9"}, role="supervisor")
    assert bad["ok"] is False and "M9" in bad["error"]
    evs = Events(p.events_file).bus.read(0, limit=1000)[0]
    asked = [(e["type"], e["source"]) for e in evs if e["type"].endswith("requested")]
    answered = [(e["type"], e["source"]) for e in evs if e["type"].startswith("request.")]
    assert asked == [("milestone.requested", "supervisor"), ("task.requested", "supervisor"), ("task.requested", "supervisor")]
    assert answered == [("request.done", "task_scheduler"), ("request.done", "task_scheduler"), ("request.refused", "task_scheduler")]
    created = [e for e in evs if e["type"] == "ticket_created"]
    assert created and all(e["source"] == "task_scheduler" for e in created)     # the task system made it, on request


def test_reads_are_not_requests(env, monkeypatch):
    p, store = env
    monkeypatch.setenv("TASKING_REQUESTS", "bus")
    assert api.handle(p, store, "list_tickets", {}, role="supervisor")["ok"]
    assert not [e for e in Events(p.events_file).bus.read(0, limit=100)[0] if e["type"].endswith("requested")]
