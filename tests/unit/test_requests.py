"""Planners' changes are requests to the task system over the bus (tasking/requests.py)."""

import pytest

from tasking.events import Events
from tasking.requests import Desk, ask
from tasking.store import LocalStore


@pytest.fixture
def env(project):
    events = Events(project.events_file, source="task_scheduler")
    store = LocalStore(project, events)
    desk = Desk(project, store, events, log=lambda m: None)
    desk.start()
    yield project, store
    desk.stop.set()


def test_a_planners_change_is_a_request_the_scheduler_applies_and_answers(env):
    p, store = env
    planner = Events(p.events_file, source="supervisor")                # the planner's own process
    m = ask(planner, "create_milestone", {"name": "M"}, "supervisor")
    assert m["ok"] and m["milestone"]["id"] == "M1"
    t = ask(planner, "create_ticket", {"title": "a task", "milestone_id": "M1", "description": "x"}, "supervisor")
    assert t["ok"] and store.get_ticket(t["ticket"]["id"]).title == "a task" and store.get_ticket(t["ticket"]["id"]).created_by == "supervisor"
    bad = ask(planner, "create_ticket", {"title": "lost", "milestone_id": "M9"}, "supervisor")
    assert bad["ok"] is False and "M9" in bad["error"]
    assert ask(planner, "no_such_op", {}, "supervisor")["ok"] is False
    anonymous = ask(Events(p.events_file, source="someone"), "add_note", {"id": "T-1", "text": "hi"}, "")
    assert anonymous["ok"] is False and "by" in anonymous["error"]       # a change names who asks
    evs = Events(p.events_file, source="test").bus.read(0, limit=1000)[0]
    asked = [(e["type"], e["source"]) for e in evs if e["type"].endswith("requested")]
    answered = [(e["type"], e["source"]) for e in evs if e["type"].startswith("request.")]
    assert asked[:3] == [("milestone.requested", "supervisor"), ("task.requested", "supervisor"), ("task.requested", "supervisor")]
    assert answered[:3] == [("request.done", "task_scheduler"), ("request.done", "task_scheduler"), ("request.refused", "task_scheduler")]
    created = [e for e in evs if e["type"] == "ticket.created"]
    assert created and all(e["source"] == "task_scheduler" for e in created)     # the task system made it, on request


def test_any_planner_may_change_any_field(env):
    """Which planner may change what is the planners' organisation, not the task system's."""
    p, store = env
    coordinator = Events(p.events_file, source="coordinator")
    ask(coordinator, "create_milestone", {"name": "M"}, "coordinator")
    t = ask(coordinator, "create_ticket", {"title": "a task", "milestone_id": "M1", "objective": "old"}, "coordinator")["ticket"]
    assert ask(coordinator, "update_ticket", {"id": t["id"], "objective": "new"}, "coordinator")["ok"]
    assert store.get_ticket(t["id"]).objective == "new"
    assert ask(Events(p.events_file, source="planner"), "set_capacity", {"workers": 1}, "planner")["ok"]


def test_reads_are_queries_the_desk_answers(env):
    p, _store = env
    ask(Events(p.events_file, source="supervisor"), "create_milestone", {"name": "M"}, "supervisor")
    got = ask(Events(p.events_file, source="coordinator"), "list_milestones", {}, "coordinator")
    assert got["ok"] and [m["id"] for m in got["milestones"]] == ["M1"]
    queries = [e for e in Events(p.events_file, source="test").bus.read(0, limit=100)[0] if e["type"] == "task.query_requested"]
    assert [(e["data"]["op"], e["source"]) for e in queries] == [("list_milestones", "coordinator")]
