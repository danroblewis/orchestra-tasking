"""The scheduler's own guards: one process per project, and one bad event on the bus never
loses the rest of its batch. Driven by hand: the test plays the machine."""

import os
import time

import pytest

from tasking.assignments import ACCEPTED, ASSIGNED, REPORTED, WORKING, Follower
from tasking.events import Events
from tasking.model import State
from tasking.scheduler import Scheduler
from tasking.store import LocalStore


def on_bus(events, type_):
    return [e for e in events.bus.read(0, limit=10000)[0] if e["type"] == type_]


def test_a_second_scheduler_on_a_project_a_live_one_holds_does_not_start(project):
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    store.set_scheduler(os.getppid(), time.time())                   # a live pid that is not this process
    ms = store.create_milestone(name="m")
    t = store.create_ticket(title="a task", milestone_id=ms.id)
    with pytest.raises(SystemExit):
        Scheduler(project, store, events).run(once=True)
    assert store.scheduler()["pid"] == os.getppid()                  # the record is the other scheduler's still
    assert store.assignments() == {} and store.get_ticket(t.id).state == State.OPEN   # nothing offered, nothing released
    assert on_bus(events, "task.released") == [] and on_bus(events, "scheduler.started") == []


def test_an_error_on_one_event_does_not_lose_the_rest_of_the_batch(project, monkeypatch):
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    sched = Scheduler(project, store, events)
    sched.recover()
    follow = Follower(events, start=0)
    ms = store.create_milestone(name="m")
    t = store.create_ticket(title="a task", milestone_id=ms.id, description="x", acceptance="y")
    sched.tick()                                                     # offered
    a = [e for e in follow.new() if e["type"] == ASSIGNED][-1]
    say = lambda kind, **d: events.publish(kind, {"assignment": a["assignment"], "ticket": t.id, "role": "worker", **d},
                                           source="worker/1", subject=t.id)
    say(ACCEPTED)
    sched.tick()                                                     # held by worker/1
    assert store.get_ticket(t.id).state == State.IN_PROGRESS
    say(WORKING)
    say(REPORTED, report={"state": "READY_FOR_REVIEW", "summary": "done"}, failed=False, text="")
    update, calls = store.update_assignment, []

    def flaky(aid, **fields):
        calls.append(aid)
        if len(calls) == 1:                                          # the heartbeat's lease extension fails
            raise OSError("disk full")
        return update(aid, **fields)
    monkeypatch.setattr(store, "update_assignment", flaky)
    sched.tick()
    assert store.get_ticket(t.id).state == State.READY_FOR_REVIEW   # the report after the bad event was applied
    assert a["assignment"] not in store.assignments()
