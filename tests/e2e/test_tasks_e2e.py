"""work = "tasks": a ticket is a task, done in a scratch folder and reviewed, with nothing merged;
the work is offered over the bus, and the machine that accepts it holds it (tests/fake_machine.py).
Also the contract's hard cases: leases, two machines answering one offer, the operator's stop, a
report from a machine that does not hold the work, the scheduler restarting and stopping."""

import subprocess
import time

import pytest
from fake_machine import FakeMachine
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tasking import scheduler as scheduler_mod
from tasking.assignments import ACCEPTED, RELEASED, REPORTED
from tasking.events import Events
from tasking.model import State
from tasking.scheduler import Scheduler
from tasking.store import LocalStore
from tasking.web import router


@pytest.fixture
def env(project):
    assert project.config.work == "tasks"
    events = Events.for_project(project, source="task_scheduler")
    return project, LocalStore(project, events), events, project.repo_root


def run_scheduler(project, store, events, **behaviour):
    with FakeMachine(events, **behaviour) as m:
        Scheduler(project, store, events).run(once=True)
    return m


def a_ticket(store):
    ms = store.create_milestone(name="m")
    return store.create_ticket(title="a task", milestone_id=ms.id, description="x", acceptance="y")


def on_bus(events, type_, **match):
    return [e for e in events.bus.read(0, limit=10000)[0] if e["type"] == type_ and all((e["data"] or {}).get(k) == v for k, v in match.items())]


def wait_for(check, timeout_s=5.0):
    end = time.time() + timeout_s
    while time.time() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("did not happen in time")


def test_a_task_is_done_in_its_folder_and_reviewed_with_nothing_merged(env):
    project, store, events, repo = env
    t = a_ticket(store)
    m = run_scheduler(project, store, events)
    t = store.get_ticket(t.id)
    assert t.state == State.DONE and t.branch is None
    assert (project.workspaces_dir / t.id / "RESULT.md").read_text().startswith(f"# {t.id}")
    assert not subprocess.run(["git", "branch", "--list"], cwd=repo, capture_output=True, text=True, check=False).stdout.strip("* main\n")
    assert store.gate_runs() == []
    review = next(o for o in m.offers if o["role"] == "reviewer")
    assert review["review"] == {"summary": "wrote RESULT.md", "commit": None, "test_output": None} and review["branch"] is None


def test_the_scheduler_is_a_program_and_starts_no_machine(env):
    project, store, events, _repo = env
    a_ticket(store)
    run_scheduler(project, store, events)
    evs, _ = events.bus.read(0, limit=10000)
    started = {e["source"]: e["data"] for e in evs if e["type"] == "orchestra.machine.started"}
    assert list(started) == ["task_scheduler"] and started["task_scheduler"]["kind"] == "program"
    assert {e["source"] for e in evs} <= {"task_scheduler", "worker/1", "reviewer/1"}


def test_work_is_assigned_over_the_bus_and_the_machine_that_accepts_holds_it(env):
    """The scheduler offers the ticket to a role (task.assigned); the machine that serves it
    accepts (and so holds the ticket), and reports (task.reported); the scheduler applies it."""
    project, store, events, _repo = env
    t = a_ticket(store)
    run_scheduler(project, store, events)
    evs = [e for e in events.bus.read(0, limit=10000)[0] if e.get("subject") == t.id]
    proto = [(e["type"], e["source"], (e["data"] or {}).get("role")) for e in evs if e["type"].startswith("task.") and e["type"] != "task.attention_needed"]
    assert [p for p in proto if p[0] != "task.working"] == [
        ("task.assigned", "task_scheduler", "worker"), ("task.accepted", "worker/1", "worker"), ("task.reported", "worker/1", "worker"),
        ("task.assigned", "task_scheduler", "reviewer"), ("task.accepted", "reviewer/1", "reviewer"), ("task.reported", "reviewer/1", "reviewer")]
    transitions = [(e["data"]["to"], e["data"]["owner"], e["source"]) for e in evs if e["type"] == "ticket.transitioned"]
    assert all(src == "task_scheduler" for _, _, src in transitions)                     # only the scheduler moves tickets
    assert [(to, owner) for to, owner, _ in transitions if to in ("IN_PROGRESS", "IN_REVIEW")] == [("IN_PROGRESS", "worker/1"), ("IN_REVIEW", "reviewer/1")]
    assert store.get_ticket(t.id).state.value == "DONE" and store.get_ticket(t.id).owner is None
    assert store.assignments() == {}


def test_an_offer_nobody_accepts_is_offered_again_and_counted_against_no_one(env, monkeypatch):
    project, store, events, _repo = env
    lease = scheduler_mod.LEASE_S
    monkeypatch.setattr(scheduler_mod, "LEASE_S", 0.0)                                    # nobody answers in time: released at once
    t = a_ticket(store)
    sched = Scheduler(project, store, events)
    sched.recover()
    sched.tick()                                                                          # offered
    first = set(store.assignments())
    assert store.get_ticket(t.id).state == State.OPEN and store.assignments()[next(iter(first))]["instance"] is None
    sched.tick()                                                                          # the lease ran out: released, offered again
    released = on_bus(events, RELEASED)
    assert released and released[0]["data"]["assignment"] in first
    assert released[0]["data"]["reason"] == "nobody filled the role worker within 0 s" and "instance" not in released[0]["data"]
    assert not first & set(store.assignments()) and len(store.assignments()) == 1          # the old offer is gone, a new one stands
    assert store.notes(t.id) == []                                                        # no crash: nobody was at fault
    assert on_bus(events, "ticket.transitioned") == []                                    # an offer is not a transition
    assert store.get_ticket(t.id).worker_turns == 0 and store.get_ticket(t.id).owner is None   # and not a turn
    assert on_bus(events, "task.attention_needed") == []
    assert sum("nobody filled the role" in line for line in scheduler_mod.LOG.read_text().splitlines()) == 1
    monkeypatch.setattr(scheduler_mod, "LEASE_S", lease)
    with FakeMachine(events, roles=("worker",), heartbeat=10.0):                          # now a machine accepts
        aid = next(iter(store.assignments()))
        wait_for(lambda: on_bus(events, ACCEPTED, assignment=aid))
        sched.tick()
        t = store.get_ticket(t.id)
        assert t.state == State.IN_PROGRESS and t.worker_turns == 1 and t.owner == "worker/1"   # the acceptance is the transition
        assert [(e["data"]["from"], e["data"]["to"]) for e in on_bus(events, "ticket.transitioned")] == [("OPEN", "IN_PROGRESS")]


def test_an_acceptance_names_its_source_as_the_instance(env):
    """``instance`` in task.accepted is the machine's own source; one that says otherwise is
    refused and the offer stays open for whoever answers correctly."""
    project, store, events, _repo = env
    t = a_ticket(store)
    sched = Scheduler(project, store, events)
    sched.recover()
    with FakeMachine(events, roles=("worker",), instance="1", claims="w1", heartbeat=0.05):   # source worker/1, instance "w1"
        sched.tick()
        aid = next(iter(store.assignments()))
        wait_for(lambda: on_bus(events, ACCEPTED, assignment=aid, instance="w1"))
        sched.tick()
        told = on_bus(events, RELEASED, assignment=aid)
        assert len(told) == 1 and told[0]["data"]["instance"] == "w1"
        assert told[0]["data"]["reason"] == "instance must be the event's source (worker/1)"
        assert store.assignments()[aid]["instance"] is None and store.get_ticket(t.id).state == State.OPEN   # the offer stands
        with FakeMachine(events, roles=("worker",), instance="2", heartbeat=0.05) as two:     # answers correctly
            wait_for(lambda: on_bus(events, ACCEPTED, assignment=aid, instance="worker/2"))
            sched.tick()
            assert store.assignments()[aid]["instance"] == "worker/2" and store.get_ticket(t.id).owner == "worker/2"
            two.finish.set()
            wait_for(lambda: on_bus(events, REPORTED, assignment=aid))
            sched.tick()
    assert store.get_ticket(t.id).state == State.READY_FOR_REVIEW and store.get_ticket(t.id).worker_turns == 1


def test_a_machine_that_accepts_and_goes_silent_is_a_crash(env, monkeypatch):
    project, store, events, _repo = env
    t = a_ticket(store)
    sched = Scheduler(project, store, events)
    sched.recover()
    with FakeMachine(events, heartbeat=10.0) as m:                                        # accepts, then nothing for a long while
        sched.tick()
        aid = next(iter(store.assignments()))
        wait_for(lambda: on_bus(events, ACCEPTED, assignment=aid))
        sched.tick()                                                                      # the acceptance is applied
        assert store.assignments()[aid]["instance"] == "worker/1" and store.get_ticket(t.id).owner == "worker/1"
        monkeypatch.setattr(scheduler_mod, "LEASE_S", 0.0)
        store.update_assignment(aid, lease_until=0)
        sched.tick()                                                                      # the lease ran out
        released = on_bus(events, RELEASED, assignment=aid)
        assert released and released[0]["data"]["reason"].startswith("no word from worker/1")
        assert [n["kind"] for n in store.notes(t.id)] == ["crash"]
        t = store.get_ticket(t.id)
        assert t.worker_turns == 1 and t.state == State.OPEN and len(store.assignments()) == 1   # back, and offered again
        m.finish.set()


def test_the_first_to_accept_holds_it_and_the_other_is_told(env):
    project, store, events, _repo = env
    t = a_ticket(store)
    sched = Scheduler(project, store, events)
    sched.recover()
    with FakeMachine(events, roles=("worker",), instance="1", heartbeat=0.05) as one:
        sched.tick()
        aid = next(iter(store.assignments()))
        wait_for(lambda: on_bus(events, ACCEPTED, assignment=aid, instance="worker/1"))
        with FakeMachine(events, roles=("worker",), instance="2", heartbeat=0.05) as two:  # sees the same offer, accepts second
            wait_for(lambda: on_bus(events, ACCEPTED, assignment=aid, instance="worker/2"))
            sched.tick()
            told = on_bus(events, RELEASED, assignment=aid)
            assert len(told) == 1 and told[0]["data"]["instance"] == "worker/2" and told[0]["data"]["reason"] == "already taken by worker/1"
            assert store.assignments()[aid]["instance"] == "worker/1"
            wait_for(lambda: aid in two._released)                                        # told, it stops without reporting
        lease = store.assignments()[aid]["lease_until"]
        time.sleep(0.2)
        sched.tick()                                                                      # heartbeats from worker/1 extend the lease
        assert store.assignments()[aid]["lease_until"] > lease
        one.finish.set()
        wait_for(lambda: on_bus(events, REPORTED, assignment=aid))
        sched.tick()
    t = store.get_ticket(t.id)
    assert t.state == State.READY_FOR_REVIEW and t.worker_turns == 1                    # reported, and offered to a reviewer
    assert [a["role"] for a in store.assignments().values()] == ["reviewer"]
    assert [e["source"] for e in on_bus(events, REPORTED, assignment=aid)] == ["worker/1"]


def test_the_operator_stops_whoever_holds_a_ticket(env):
    project, store, events, _repo = env
    t = a_ticket(store)
    sched = Scheduler(project, store, events)
    sched.recover()
    app = FastAPI()
    app.include_router(router(project))
    web = TestClient(app)
    with FakeMachine(events, heartbeat=0.05) as m:
        sched.tick()
        aid = next(iter(store.assignments()))
        wait_for(lambda: on_bus(events, ACCEPTED, assignment=aid))
        sched.tick()
        assert web.post("/control", json={"action": "kill", "ticket": t.id}).json() == {"ok": True, "ticket": t.id}
        sched.tick()
        released = on_bus(events, RELEASED, assignment=aid)
        assert released and released[0]["data"]["reason"] == "the operator stopped it"
        m.finish.set()
    t = store.get_ticket(t.id)
    assert t.state == State.OPEN and not any(n["kind"] == "crash" for n in store.notes(t.id))
    assert "stopped by the operator" in store.notes(t.id)[0]["text"] and store.notes(t.id)[0]["author"] == "task_scheduler"
    assert "stopped by the operator" in t.feedback
    assert aid not in store.assignments()
    assert on_bus(events, "kill.requested")[0]["source"] == "task_web"


def test_the_operator_withdraws_an_offer_nobody_took_yet(env):
    """A stop before any acceptance only withdraws the offer: no note, no transition, no turn."""
    project, store, events, _repo = env
    t = a_ticket(store)
    sched = Scheduler(project, store, events)
    sched.recover()
    app = FastAPI()
    app.include_router(router(project))
    web = TestClient(app)
    sched.tick()                                                                          # offered, nobody answers
    aid = next(iter(store.assignments()))
    assert web.post("/control", json={"action": "kill", "ticket": t.id}).json()["ok"]
    sched.tick()
    released = on_bus(events, RELEASED, assignment=aid)
    assert released and released[0]["data"]["reason"] == "the operator stopped it"
    assert aid not in store.assignments() and len(store.assignments()) == 1               # withdrawn, and offered again
    assert store.notes(t.id) == [] and on_bus(events, "ticket.transitioned") == []
    assert store.get_ticket(t.id).state == State.OPEN and store.get_ticket(t.id).worker_turns == 0
    done = store.transition(t.id, store.get_ticket(t.id).version, State.CANCELLED)        # nothing held: a kill has nothing to stop
    assert done.state == State.CANCELLED
    sched.tick()                                                                          # the standing offer is withdrawn: the ticket moved on
    assert store.assignments() == {} and on_bus(events, RELEASED)[-1]["data"]["reason"] == "the ticket is CANCELLED"
    assert web.post("/control", json={"action": "kill", "ticket": t.id}).status_code == 409


def test_a_report_from_a_machine_that_does_not_hold_the_work_is_ignored(env):
    project, store, events, _repo = env
    t = a_ticket(store)
    sched = Scheduler(project, store, events)
    sched.recover()
    with FakeMachine(events, heartbeat=0.05) as m:
        sched.tick()
        aid = next(iter(store.assignments()))
        wait_for(lambda: on_bus(events, ACCEPTED, assignment=aid))
        sched.tick()
        events.publish(REPORTED, {"assignment": aid, "ticket": t.id, "role": "worker", "report": {"state": "BLOCKED", "blocked_reason": "impostor"}},
                       source="worker/9", subject=t.id)
        sched.tick()
        assert store.get_ticket(t.id).state == State.IN_PROGRESS and aid in store.assignments()     # still worker/1's
        m.finish.set()
        wait_for(lambda: on_bus(events, REPORTED, assignment=aid, role="worker") and on_bus(events, REPORTED)[-1]["source"] == "worker/1")
        sched.tick()
    assert store.get_ticket(t.id).state == State.READY_FOR_REVIEW                        # worker/1's report applied, review offered
    assert [a["role"] for a in store.assignments().values()] == ["reviewer"]
    assert [n["author"] for n in store.notes(t.id)] == ["worker"] and "impostor" not in str(store.notes(t.id))


def test_a_restarted_scheduler_tells_the_holder_to_stop(env):
    project, store, events, _repo = env
    t = a_ticket(store)
    sched = Scheduler(project, store, events)
    sched.recover()
    with FakeMachine(events, heartbeat=0.05) as m:
        sched.tick()
        aid = next(iter(store.assignments()))
        wait_for(lambda: on_bus(events, ACCEPTED, assignment=aid))
        sched.tick()
        Scheduler(project, store, events).recover()                                       # a second scheduler starts in its place
        released = on_bus(events, RELEASED, assignment=aid)
        assert released and released[0]["data"]["reason"] == "the task system restarted"
        wait_for(lambda: aid in m._released)
        m.finish.set()
    assert store.assignments() == {}
    t = store.get_ticket(t.id)
    assert t.state == State.OPEN and "restarted" in store.notes(t.id)[0]["text"] and "restarted" in t.feedback


def test_a_stopped_scheduler_leaves_nothing_held(env):
    project, store, events, _repo = env
    t = a_ticket(store)
    sched = Scheduler(project, store, events)
    with FakeMachine(events, heartbeat=0.05) as m:
        def stop_once_held():
            wait_for(lambda: any(a.get("instance") for a in store.assignments().values()), timeout_s=10)
            sched.stop.set()
        import threading
        threading.Thread(target=stop_once_held, daemon=True).start()
        sched.run(once=True)
        m.finish.set()
    assert store.assignments() == {}
    assert store.get_ticket(t.id).state == State.OPEN
    assert on_bus(events, RELEASED)[-1]["data"]["reason"] == "the task system shut down"
    assert [e["type"] for e in events.bus.read(0, limit=10000)[0] if e["type"].startswith("scheduler.")] == ["scheduler.started", "scheduler.stopped"]


def test_a_worker_that_cannot_act_on_feedback_blocks_instead_of_arguing(env):
    """With a reviewer that always rejects, the only floor is the worker's: it reports BLOCKED
    when offered a rejected ticket it would only redo, so the ticket ends BLOCKED after one
    round with one attention event (SPEC section 6)."""
    project, store, events, _repo = env
    t = a_ticket(store)
    run_scheduler(project, store, events, always_reject=True, block_on_rejection=True)
    t = store.get_ticket(t.id)
    assert t.state == State.BLOCKED and t.review_rounds == 1 and t.worker_turns == 2 and t.reviewer_rejections == 1
    assert "cannot act on" in t.blocked_reason
    attention = on_bus(events, "task.attention_needed", ticket=t.id)
    assert len(attention) == 1 and "BLOCKED" in attention[0]["data"]["reason"]
