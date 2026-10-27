"""The scheduler's own guards: one process per project, and one bad event on the bus never
loses the rest of its batch. Driven by hand: the test plays the machine."""

import os
import time

import pytest
from conftest import end, on_bus

from tasking.assignments import ACCEPTED, ASSIGNED, REVIEW_REQUESTED, WORKING
from tasking.events import Events
from tasking.model import State
from tasking.scheduler import Scheduler
from tasking.store import LocalStore


def test_a_second_scheduler_on_a_project_a_live_one_holds_does_not_start(project):
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    store.set_scheduler(os.getppid(), time.time())                   # a live pid that is not this process
    ms = store.create_milestone(src="planner", name="m")
    t = store.create_ticket(src="planner", title="a task", milestone_id=ms.id)
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
    ms = store.create_milestone(src="planner", name="m")
    t = store.create_ticket(src="planner", title="a task", milestone_id=ms.id, description="x", acceptance="y")
    sched.tick()                                                     # offered
    a = [e for e in events.after(0)[0] if e["type"] == ASSIGNED][-1]
    say = lambda kind, **d: events.publish(kind, {"assignment": a["assignment"], "ticket": t.id, "role": "worker", **d},
                                           source="worker/1", subject=t.id)
    say(ACCEPTED)
    sched.tick()                                                     # held by worker/1
    assert store.get_ticket(t.id).state == State.IN_PROGRESS
    say(WORKING)
    end(events, a, REVIEW_REQUESTED)
    update, calls = store.update_assignment, []

    def flaky(aid, **fields):
        calls.append(aid)
        if len(calls) == 1:                                          # the heartbeat's lease extension fails
            raise OSError("disk full")
        return update(aid, **fields)
    monkeypatch.setattr(store, "update_assignment", flaky)
    sched.tick()
    assert store.get_ticket(t.id).state == State.READY_FOR_REVIEW   # the review request after the bad event was recorded
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
    ms = store.create_milestone(src="planner", name="m")
    t = store.create_ticket(src="planner", title="a task", milestone_id=ms.id, description="x", acceptance="y")
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
    end(events, old, REVIEW_REQUESTED)                            # it asked for a review anyway
    second.tick()
    x = store.get_ticket(t.id)
    assert x.state == State.OPEN and x.worker_turns == 1                 # not applied: offered afresh, nobody took it yet
    offers = [e["data"]["assignment"] for e in on_bus(events, ASSIGNED)]
    assert len(offers) == 2 and offers[0] == old["assignment"] and offers[1] in store.assignments()
    assert any("review.requested from worker/1" in ln and old["assignment"] in ln and "ignored" in ln for ln in lines)
    assert all(e["data"]["role"] == "worker" for e in on_bus(events, ASSIGNED))       # and nobody was offered a review of it


def test_a_rewritten_ticket_does_not_inherit_the_old_backoff(project, monkeypatch):
    """Offers nobody took pause longer each time; a planner's rewrite of the ticket starts the
    count (and the age of the first offer) over."""
    from tasking import scheduler as scheduler_mod

    monkeypatch.setattr(scheduler_mod, "LEASE_S", 0.0)                # nobody answers in time
    monkeypatch.setattr(scheduler_mod, "AGAIN_FIRST_S", 0.0)          # and the pause is no wait, so each tick offers again
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    t = store.create_ticket(src="planner", title="a task", milestone_id=store.create_milestone(src="planner", name="m").id)
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


def test_an_acceptance_names_its_assignment_and_a_late_one_is_told_released(project):
    """The claim that holds the offer is recorded (the ticket held by its source, caused by the
    claim; nothing published for it); a claim of an assignment no longer held is answered by
    ``task.released`` naming the instance, so it never starts work on a ticket it does not hold."""
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    first = Scheduler(project, store, events)
    first.recover()
    ms = store.create_milestone(src="planner", name="m")
    t = store.create_ticket(src="planner", title="a task", milestone_id=ms.id, description="x", acceptance="y")
    first.tick()
    old = on_bus(events, ASSIGNED)[-1]["data"]["assignment"]
    accept = lambda aid: events.publish(ACCEPTED, {"assignment": aid, "ticket": t.id, "role": "worker"},
                                        source="worker/1", subject=t.id)
    accept(old)
    first.tick()
    claim = [e for e in on_bus(events, ACCEPTED) if e["data"]["assignment"] == old][-1]
    held = store.get_ticket(t.id)
    assert held.state == State.IN_PROGRESS and held.owner == "worker/1" and held.caused_by == claim["id"]
    assert on_bus(events, "ticket.transitioned") == []
    second = Scheduler(project, store, events)                     # a restart: the old assignment is no longer held
    second.recover()
    before = len(on_bus(events, "task.released", assignment=old))
    accept(old)                                                    # worker/1 answers late
    second.tick()
    late = on_bus(events, "task.released", assignment=old)[before:]
    assert len(late) == 1 and late[0]["data"]["instance"] == "worker/1"
    assert store.get_ticket(t.id).state == State.OPEN               # it changed nothing


def test_an_acceptance_claiming_another_instance_is_refused_to_its_source(project):
    """``task.accepted`` from worker/1 claiming worker/2 is refused, and the refusal names worker/1,
    the machine that sent it: a machine that filters releases by its own name hears it. The offer
    stays open."""
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    sched = Scheduler(project, store, events)
    sched.recover()
    ms = store.create_milestone(src="planner", name="m")
    t = store.create_ticket(src="planner", title="a task", milestone_id=ms.id, description="x", acceptance="y")
    sched.tick()
    aid = on_bus(events, ASSIGNED)[-1]["data"]["assignment"]
    events.publish(ACCEPTED, {"assignment": aid, "ticket": t.id, "role": "worker", "instance": "worker/2"},
                   source="worker/1", subject=t.id)
    sched.tick()
    refused = on_bus(events, "task.released", assignment=aid)
    assert len(refused) == 1 and refused[0]["data"]["instance"] == "worker/1"
    assert "worker/2" in refused[0]["data"]["reason"]
    assert store.get_ticket(t.id).state == State.OPEN and store.assignments()[aid].get("instance") is None
