"""The scheduler's own guards: one process per project, and one bad event on the bus never
loses the rest of its batch. Driven by hand: the test plays the machine."""

import os
import time

import pytest
from conftest import on_bus

from tasking.assignments import ACCEPTED, ASSIGNED, REPORTED, WORKING
from tasking.events import Events
from tasking.model import State
from tasking.scheduler import Scheduler
from tasking.store import LocalStore


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
    assert on_bus(events, "task.released") == [] and on_bus(events, "orchestra.machine.started") == []


def test_with_wait_a_second_scheduler_takes_over_when_the_first_stops(project):
    """How orchestra run starts it: a scheduler already running (one started by hand) keeps the
    project; the launched one waits, offering nothing, and takes over when that one is gone."""
    import subprocess
    import threading

    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    other = subprocess.Popen(["sleep", "60"])
    store.set_scheduler(other.pid, time.time())
    s = Scheduler(project, store, events)
    th = threading.Thread(target=s.run, kwargs={"wait": True}, daemon=True)
    th.start()
    time.sleep(1.0)
    assert th.is_alive() and store.scheduler()["pid"] == other.pid and on_bus(events, "orchestra.machine.started") == []
    other.kill()
    other.wait()
    deadline = time.time() + 10
    while (store.scheduler() or {}).get("pid") != os.getpid() and time.time() < deadline:
        time.sleep(0.1)
    assert store.scheduler()["pid"] == os.getpid()
    s.stop.set()
    th.join(timeout=10)
    assert not th.is_alive()


def test_an_error_on_one_event_does_not_lose_the_rest_of_the_batch(project, monkeypatch):
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    sched = Scheduler(project, store, events)
    sched.recover()
    ms = store.create_milestone(name="m")
    t = store.create_ticket(title="a task", milestone_id=ms.id, description="x", acceptance="y")
    sched.tick()                                                     # offered
    a = [e for e in events.after(0)[0] if e["type"] == ASSIGNED][-1]
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


def test_a_report_for_an_assignment_a_restart_dropped_is_ignored_with_a_line_in_the_log(project, monkeypatch):
    """On start the scheduler releases what the previous one left and offers it afresh (SPEC
    section 2: no zombies); a report that then arrives for the old assignment changes nothing,
    and the log says so."""
    from tasking import scheduler

    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    first = Scheduler(project, store, events)
    first.recover()
    ms = store.create_milestone(name="m")
    t = store.create_ticket(title="a task", milestone_id=ms.id, description="x", acceptance="y")
    first.tick()
    old = [e for e in events.after(0)[0] if e["type"] == ASSIGNED][-1]
    say = lambda kind, **d: events.publish(kind, {"assignment": old["assignment"], "ticket": t.id, "role": "worker", **d},
                                           source="worker/1", subject=t.id)
    say(ACCEPTED)
    first.tick()
    assert store.get_ticket(t.id).state == State.IN_PROGRESS
    lines = []
    monkeypatch.setattr(scheduler, "log", lines.append)
    second = Scheduler(project, store, events)                     # the scheduler restarts
    second.recover()
    assert store.get_ticket(t.id).state == State.OPEN and old["assignment"] not in store.assignments()
    assert [e["data"]["assignment"] for e in on_bus(events, "task.released")] == [old["assignment"]]   # worker/1 was told to stop
    say(REPORTED, report={"state": "READY_FOR_REVIEW", "summary": "done"}, failed=False, text="")   # it reported anyway
    second.tick()
    x = store.get_ticket(t.id)
    assert x.state == State.OPEN and x.worker_turns == 1                 # not applied: offered afresh, nobody took it yet
    offers = [e["data"]["assignment"] for e in on_bus(events, ASSIGNED)]
    assert len(offers) == 2 and offers[0] == old["assignment"] and offers[1] in store.assignments()
    assert any("task.reported from worker/1" in ln and old["assignment"] in ln and "ignored" in ln for ln in lines)


def test_a_rewritten_ticket_does_not_inherit_the_old_backoff(project, monkeypatch):
    """Offers nobody took pause longer each time; a planner's rewrite of the ticket starts the
    count (and the age of the first offer) over."""
    from tasking import scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "LEASE_S", 0.0)                # nobody answers in time
    monkeypatch.setattr(scheduler_mod, "AGAIN_FIRST_S", 0.0)          # and the pause is no wait, so each tick offers again
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    t = store.create_ticket(title="a task", milestone_id=store.create_milestone(name="m").id)
    sched = Scheduler(project, store, events)
    sched.recover()
    for _ in range(4):                                                # each tick: the offer missed, and made again
        sched.tick()
    rec = store.untaken()[f"{t.id}/worker"]
    assert rec["misses"] >= 2
    first_since = rec["since"]
    time.sleep(0.05)
    store.update_ticket(t.id, description="rewritten so a machine can take it")
    sched.tick()                                     # the offer made before the rewrite is missed, and a new one made
    rec = store.untaken()[f"{t.id}/worker"]
    assert rec["misses"] == 1 and rec["since"] > first_since and rec["version"] == store.get_ticket(t.id).version
