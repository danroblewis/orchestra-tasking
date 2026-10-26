"""Planners' changes are requests to the task system over the bus (tasking/requests.py)."""

import pytest
from conftest import asked

from tasking.events import Events
from tasking.model import State
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


def refusals(p) -> list[str]:
    """The errors the desk answered with, in order, as they are on the bus."""
    return [e["data"]["error"] for e in Events(p.events_file, source="test").bus.read(0, limit=10000)[0] if e["type"] == "request.refused"]


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
    sent = [(e["type"], e["source"]) for e in evs if e["type"].endswith("requested")]
    answered = [(e["type"], e["source"]) for e in evs if e["type"].startswith("request.")]
    assert sent[:3] == [("milestone.requested", "supervisor"), ("task.requested", "supervisor"), ("task.requested", "supervisor")]
    assert answered[:3] == [("request.done", "task_scheduler"), ("request.done", "task_scheduler"), ("request.refused", "task_scheduler")]
    created = [e for e in evs if e["type"] == "ticket.created"]
    assert created and all(e["source"] == "task_scheduler" for e in created)     # the task system made it, on request
    events = Events(p.events_file, source="test")
    assert asked(events, "coordinator") == [] and asked(events, "supervisor") == []   # a creation is routine: no attention


def test_every_operation_is_answered(env):
    """The rest of the operations, each sent as a planner and answered done or refused, with the
    refusal's error on the bus."""
    p, store = env
    planner = Events(p.events_file, source="planner")
    do = lambda op, args: ask(planner, op, args, "planner")
    m = do("create_milestone", {"name": "M", "description": "d"})["milestone"]
    t = do("create_ticket", {"title": "a", "milestone_id": m["id"]})["ticket"]["id"]
    assert do("add_note", {"id": t, "text": "hi"})["ok"] and store.notes(t)[0] == {**store.notes(t)[0], "author": "planner", "text": "hi"}
    assert do("update_ticket", {"id": t, "priority": 2, "objective": "o"})["ticket"]["priority"] == 2

    x = store.get_ticket(t)
    store.transition(t, x.version, State.IN_PROGRESS)
    assert do("cancel_ticket", {"id": t, "reason": "no"})["ok"] is False and f"{t} is IN_PROGRESS" in refusals(p)[-1]
    x = store.get_ticket(t)
    store.transition(t, x.version, State.OPEN)
    assert do("cancel_ticket", {"id": t, "reason": "no"})["ticket"]["state"] == "CANCELLED"

    u = do("create_ticket", {"title": "b", "milestone_id": m["id"]})["ticket"]["id"]
    assert do("unblock_ticket", {"id": u, "note": "n"})["ok"] is False and f"{u} is OPEN, not BLOCKED" in refusals(p)[-1]
    x = store.get_ticket(u)
    x = store.transition(u, x.version, State.IN_PROGRESS)
    store.transition(u, x.version, State.BLOCKED, blocked_reason="r")
    assert do("unblock_ticket", {"id": u, "note": "n"})["ticket"]["state"] == "OPEN"

    assert do("update_milestone", {"id": m["id"], "name": "N"})["milestone"]["name"] == "N"
    assert do("close_milestone", {"id": m["id"]})["ok"] is False and f"unfinished tickets {u}" in refusals(p)[-1]
    do("cancel_ticket", {"id": u})
    assert do("close_milestone", {"id": m["id"]})["milestone"]["status"] == "closed"

    store.set_pipeline(True, "x")
    assert do("resume_pipeline", {})["ok"] and store.pipeline()["paused"] is False

    assert {x["id"] for x in do("list_tickets", {})["tickets"]} == {t, u}
    assert [x["id"] for x in do("list_tickets", {"state": "CANCELLED"})["tickets"]] == [t, u]
    assert do("get_ticket", {"id": t})["ticket"]["notes"][0]["text"] == "hi"
    assert set(do("project_status", {})) == {"ok", "counts", "pipeline", "milestones", "assigned", "blocked"}
    assert {"tickets", "milestones", "counts", "capacity", "assignments", "gate_runs", "pipeline"} <= set(do("snapshot", {})["snapshot"])
    assert set(do("get_capacity", {})["capacity"]) == {"workers", "reviewers", "max_workers", "max_reviewers", "note"}
    assert do("list_gate_runs", {})["runs"] == []
    assert do("get_gate_run", {"id": "nope"})["ok"] is False and "no gate run nope" in refusals(p)[-1]
    assert set(do("get_staging_status", {})) == {"ok", "staging", "log_tail"}


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
