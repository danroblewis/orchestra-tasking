"""Planners' changes are requests to the task system over the bus (tasking/requests.py)."""

import pytest
from conftest import asked, wait_for

from tasking.events import Events
from tasking.model import State
from tasking.requests import Desk, ask
from tasking.store import LocalStore


@pytest.fixture
def env(project):
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    desk = Desk(project, store, events, log=lambda m: None)
    desk.start()
    yield project, store
    desk.stop.set()


def answered(p) -> dict[str, dict]:
    """request id -> its answer (the request.done or request.refused event), in the order the desk
    answered, as they are on the bus."""
    return {e["data"]["request"]: e for e in Events.for_project(p, source="test").bus.read(0, limit=10000)[0]
            if e["type"] in ("request.done", "request.refused")}


def kinds(p) -> dict[str, str]:
    return {r: e["type"] for r, e in answered(p).items()}


def test_a_planners_change_is_a_request_the_scheduler_applies_and_answers(env):
    p, store = env
    planner = Events.for_project(p, source="supervisor")                # the planner's own process
    m = ask(planner, "create_milestone", {"name": "M"}, "supervisor")
    assert m["ok"] and m["milestone"]["id"] == "M1"
    t = ask(planner, "create_ticket", {"title": "a task", "milestone_id": "M1", "description": "x"}, "supervisor")
    assert t["ok"] and store.get_ticket(t["ticket"]["id"]).title == "a task" and store.get_ticket(t["ticket"]["id"]).created_by == "supervisor"
    bad = ask(planner, "create_ticket", {"title": "lost", "milestone_id": "M9"}, "supervisor")
    assert bad["ok"] is False and "M9" in bad["error"]
    assert ask(planner, "no_such_op", {}, "supervisor")["ok"] is False
    anonymous = ask(Events.for_project(p, source="someone"), "add_note", {"id": "T-1", "text": "hi"}, "")
    assert anonymous["ok"] is False and "by" in anonymous["error"]       # a change names who asks
    evs = Events.for_project(p, source="test").bus.read(0, limit=1000)[0]
    sent = [(e["type"], e["source"]) for e in evs if e["type"].endswith("requested")]
    answered = [(e["type"], e["source"]) for e in evs if e["type"].startswith("request.")]
    assert sent[:3] == [("milestone.requested", "supervisor"), ("task.requested", "supervisor"), ("task.requested", "supervisor")]
    # the desk stands in for the asker's own request handling: its answers, and what a request made,
    # go out as the asker, naming the request as their cause; never as the scheduler
    assert answered[:3] == [("request.done", "supervisor"), ("request.done", "supervisor"), ("request.refused", "supervisor")]
    requests = {e["id"]: e for e in evs if e["type"].endswith("requested")}
    created = [e for e in evs if e["type"] in ("ticket.created", "milestone.created")]
    assert len(created) == 2 and all(e["source"] == "supervisor" and requests[e["causationid"]]["source"] == "supervisor" for e in created)
    assert all(e["causationid"] in requests for e in evs if e["type"].startswith("request."))
    assert not [e for e in evs if e["source"] == "task_scheduler"]
    events = Events.for_project(p, source="test")
    assert asked(events, "coordinator") == [] and asked(events, "supervisor") == []   # a creation is routine: no attention


def test_every_operation_is_answered(env):
    """The rest of the operations, each sent as a planner and answered done or refused, with the
    refusal's error on the bus."""
    p, store = env
    planner = Events.for_project(p, source="planner")
    do = lambda op, args: ask(planner, op, args, "planner")
    refusal = lambda: list(answered(p).values())[-1]["data"]["error"]   # the last answer's error, on the bus
    m = do("create_milestone", {"name": "M", "description": "d"})["milestone"]
    t = do("create_ticket", {"title": "a", "milestone_id": m["id"]})["ticket"]["id"]
    assert do("add_note", {"id": t, "text": "hi"})["ok"] and store.notes(t)[0] == {**store.notes(t)[0], "author": "planner", "text": "hi"}
    assert do("update_ticket", {"id": t, "priority": 2, "objective": "o"})["ticket"]["priority"] == 2

    x = store.get_ticket(t)
    store.transition(t, x.version, State.IN_PROGRESS, by="worker")
    assert do("cancel_ticket", {"id": t, "reason": "no"})["ok"] is False and f"{t} is IN_PROGRESS" in refusal()
    x = store.get_ticket(t)
    store.transition(t, x.version, State.OPEN, by="worker")
    assert do("cancel_ticket", {"id": t, "reason": "no"})["ticket"]["state"] == "CANCELLED"

    u = do("create_ticket", {"title": "b", "milestone_id": m["id"]})["ticket"]["id"]
    assert do("unblock_ticket", {"id": u, "note": "n"})["ok"] is False and f"{u} is OPEN, not BLOCKED" in refusal()
    x = store.get_ticket(u)
    x = store.transition(u, x.version, State.IN_PROGRESS, by="worker")
    store.transition(u, x.version, State.BLOCKED, blocked_reason="r", by="worker")
    assert do("unblock_ticket", {"id": u, "note": "n"})["ticket"]["state"] == "OPEN"

    assert do("update_milestone", {"id": m["id"], "name": "N"})["milestone"]["name"] == "N"
    assert do("close_milestone", {"id": m["id"]})["ok"] is False and f"unfinished tickets {u}" in refusal()
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
    assert do("get_gate_run", {"id": "nope"})["ok"] is False and "no gate run nope" in refusal()
    assert set(do("get_staging_status", {})) == {"ok", "staging", "log_tail"}


def test_any_planner_may_change_any_field(env):
    """Which planner may change what is the planners' organisation, not the task system's."""
    p, store = env
    coordinator = Events.for_project(p, source="coordinator")
    ask(coordinator, "create_milestone", {"name": "M"}, "coordinator")
    t = ask(coordinator, "create_ticket", {"title": "a task", "milestone_id": "M1", "objective": "old"}, "coordinator")["ticket"]
    assert ask(coordinator, "update_ticket", {"id": t["id"], "objective": "new"}, "coordinator")["ok"]
    assert store.get_ticket(t["id"]).objective == "new"
    assert ask(Events.for_project(p, source="planner"), "set_capacity", {"workers": 1}, "planner")["ok"]


def test_a_query_with_an_unknown_op_is_refused(env):
    """The contract is the bus: every request is answered, including one `ask` would not send."""
    p, _store = env
    planner = Events.for_project(p, source="planner")
    planner.publish("task.query_requested", {"request": "x1", "op": "list_ticket", "args": {}, "by": "planner"})
    wait_for(lambda: "x1" in answered(p))
    answer = answered(p)["x1"]
    assert answer["type"] == "request.refused" and answer["data"]["error"].startswith("no query op 'list_ticket'; queries: list_tickets, get_ticket")


def test_reads_are_queries_the_desk_answers(env):
    p, _store = env
    ask(Events.for_project(p, source="supervisor"), "create_milestone", {"name": "M"}, "supervisor")
    got = ask(Events.for_project(p, source="coordinator"), "list_milestones", {}, "coordinator")
    assert got["ok"] and [m["id"] for m in got["milestones"]] == ["M1"]
    queries = [e for e in Events.for_project(p, source="test").bus.read(0, limit=100)[0] if e["type"] == "task.query_requested"]
    assert [(e["data"]["op"], e["source"]) for e in queries] == [("list_milestones", "coordinator")]


def test_a_request_made_while_no_desk_ran_is_answered_when_one_starts(project):
    """The desk reads on from the last request it answered: a request published while the
    scheduler was down is applied when it comes back, if its asker may still be waiting; one older
    than that is refused with the reason, never applied late; and a restart answers nothing twice."""
    from datetime import UTC, datetime, timedelta

    from tasking.requests import RESUME_S

    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    new_desk = lambda: Desk(project, store, events, log=lambda m: None)
    first = new_desk()
    first.start()
    planner = Events.for_project(project, source="planner")
    assert ask(planner, "create_milestone", {"name": "M"}, "planner")["ok"]
    first.stop.set()
    first.thread.join()
    # no desk runs: a planner asks twice, one just now and one from before anyone would still wait
    planner.publish("task.requested", {"request": "fresh", "args": {"title": "fresh", "milestone_id": "M1"}, "by": "planner"})
    long_ago = (datetime.now(UTC) - timedelta(seconds=RESUME_S + 5)).isoformat(timespec="milliseconds")
    planner.publish("task.requested", {"request": "stale", "args": {"title": "stale", "milestone_id": "M1"}, "by": "planner"}, time=long_ago)
    assert [t.title for t in store.list_tickets()] == []
    second = new_desk()
    second.start()
    wait_for(lambda: {"fresh", "stale"} <= set(answered(project)))
    second.stop.set()
    second.thread.join()
    assert kinds(project)["fresh"] == "request.done" and kinds(project)["stale"] == "request.refused"
    stale = answered(project)["stale"]["data"]["error"]
    assert "no scheduler was running when this was asked" in stale and "ask again" in stale
    assert [t.title for t in store.list_tickets()] == ["fresh"]
    before = kinds(project)
    new_desk().serve()                                      # a third desk: everything before it is answered already
    assert kinds(project) == before


def test_a_desk_that_stops_halfway_through_a_batch_answers_nothing_twice(project, monkeypatch):
    from tasking import api

    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    planner = Events.for_project(project, source="planner")
    planner.publish("milestone.requested", {"request": "m", "args": {"name": "M"}, "by": "planner"})
    planner.publish("task.requested", {"request": "a", "args": {"title": "a", "milestone_id": "M1"}, "by": "planner"})
    planner.publish("task.requested", {"request": "b", "args": {"title": "b", "milestone_id": "M1"}, "by": "planner"})
    handle, calls = api.handle, []

    def crash_on_the_third(*a, **kw):
        calls.append(1)
        if len(calls) == 3:
            raise KeyboardInterrupt                         # the process dies between two answers of one batch
        return handle(*a, **kw)
    monkeypatch.setattr(api, "handle", crash_on_the_third)
    store.set_desk(0, [])                                   # a desk ran before: read from the start
    with pytest.raises(KeyboardInterrupt):
        Desk(project, store, events, log=lambda m: None).serve()
    assert kinds(project) == {"m": "request.done", "a": "request.done"}
    monkeypatch.setattr(api, "handle", handle)
    Desk(project, store, events, log=lambda m: None).serve()
    assert kinds(project) == {"m": "request.done", "a": "request.done", "b": "request.done"}
    assert sorted(t.title for t in store.list_tickets()) == ["a", "b"]   # a was not created twice
    assert store.desk()["answered"] == []
