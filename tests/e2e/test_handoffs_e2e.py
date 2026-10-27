"""The task system is the record, not the hub (the owner's decision D17): work passes directly
between the machines that do it, and the scheduler records it, takes back what a silent machine
holds, offers waiting work and runs the gate. The real scheduler, store, git and gate, with
machines that speak the protocol (tests/fake_machine.py); the hand-offs are the ones Orchestra's
Machines page and event map compute from the bus (conftest.handoffs)."""

import time

import pytest
from conftest import claim, end, git, handoffs, on_bus, run_scheduler
from fake_machine import FakeMachine

from tasking import scheduler as scheduler_mod
from tasking.assignments import (
    ACCEPTED,
    APPROVED,
    ASSIGNED,
    CHANGES_REQUESTED,
    RELEASED,
    REVIEW_REQUESTED,
)
from tasking.events import Events
from tasking.model import State
from tasking.scheduler import Scheduler
from tasking.store import LocalStore


def setup(project, work: str, **tasks):
    repo = project.repo_root
    toml = repo / "orchestra.toml"
    text = toml.read_text().replace('work = "tasks"', f'work = "{work}"')
    for k, v in tasks.items():
        text = text.replace("[tasks]\n", f"[tasks]\n{k} = {v}\n")
    toml.write_text(text)
    project.reload_config()
    git("add", "-A", cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", f"{work} mode", cwd=repo)
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    t = store.create_ticket(src="planner", title="a ticket", milestone_id=store.create_milestone(src="planner", name="m").id, description="x", acceptance="y")
    return store, events, t


def until(sched, check, timeout_s=20.0):
    end_at = time.time() + timeout_s
    while time.time() < end_at:
        sched.tick()
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("did not happen in time")


def test_rejected_work_returns_to_the_same_worker_and_the_scheduler_is_no_hub(project):
    """Worked, rejected once, reworked by the same worker instance, approved and landed. On the
    bus the hand-offs go planner → worker → reviewer → worker → reviewer → merge_gate; the
    scheduler appears nowhere: what it publishes goes out as the machine it acts for (the first
    offer as the worker's queue), and never restates another machine's act."""
    store, events, t = setup(project, "code")
    with FakeMachine(events, roles=("worker",), instance="2"):            # another worker, free all along
        m = run_scheduler(project, store, events, reject_first=True)
    t = store.get_ticket(t.id)
    assert t.state == State.DONE and t.worker_turns == 2 and t.reviewer_rejections == 1
    work = [e for e in on_bus(events, ACCEPTED, ticket=t.id) if e["data"]["role"] == "worker"]
    assert len(work) >= 2 and work[-1]["source"] == work[0]["source"] == t.worker      # the second turn: the same instance
    rejection = on_bus(events, CHANGES_REQUESTED, ticket=t.id)
    assert len(rejection) == 1 and rejection[0]["data"]["to"] == t.worker and rejection[0]["source"] == "reviewer/1"
    assert [o["type"] for o in m.offers if o["role"] == "worker"].count(ASSIGNED) == 1      # only the first turn came from the queue
    assert handoffs(events, t.id) == [("planner", "ticket.created", "worker"), ("worker", REVIEW_REQUESTED, "reviewer"),
                                      ("reviewer", CHANGES_REQUESTED, "worker"), ("worker", REVIEW_REQUESTED, "reviewer"),
                                      ("reviewer", APPROVED, "merge_gate")]
    evs = events.bus.read(0, limit=100000)[0]
    assert not [e for e in evs if e["source"] == "task_scheduler"]                       # no event as the scheduler, not even an announcement
    assert not [e for e in evs if e["type"] in ("ticket.transitioned", "ticket.note_added")]   # no echo of another machine's act
    queue = [e["type"] for e in evs if e.get("subject") == t.id and e["source"] == "worker"]
    assert queue == [ASSIGNED]                                                            # the worker's queue: the first offer, nothing else
    assert {e["source"] for e in evs if e["type"].startswith("gate.")} == {"merge_gate"}
    started = next(e for e in evs if e["type"] == "gate.started")
    approved = on_bus(events, APPROVED, ticket=t.id)[-1]
    assert started["causationid"] == approved["id"]                                      # the gate acted on the approval
    assert "WORK.md" in git("ls-tree", "--name-only", "main", cwd=project.repo_root)


def test_a_worker_gone_at_rejection_loses_the_work_to_the_queue_after_the_timeout(project):
    """The worker that did the work is gone when the rejection comes back: after the return
    timeout the scheduler takes the work back, says why on the ticket, and offers it to any
    worker; another takes it, and it lands."""
    store, events, t = setup(project, "tasks", return_timeout='"1s"')
    sched = Scheduler(project, store, events)
    sched.recover()
    with FakeMachine(events, roles=("worker",), instance="1", leave_after=True), FakeMachine(events, roles=("reviewer",), reject_first=True):
        until(sched, lambda: on_bus(events, CHANGES_REQUESTED, ticket=t.id))
        assert on_bus(events, CHANGES_REQUESTED, ticket=t.id)[0]["data"]["to"] == "worker/1"
        with FakeMachine(events, roles=("worker",), instance="2") as two:
            until(sched, lambda: store.get_ticket(t.id).state == State.DONE)
    released = [e for e in on_bus(events, RELEASED, ticket=t.id) if "did not take" in e["data"]["reason"]]
    assert len(released) == 1
    assert released[0]["data"]["reason"] == "worker/1 did not take the rejected work back within 1 s (gone, or busy); offered to any worker"
    assert any(n["author"] == "task_scheduler" and n["text"].startswith("worker/1 did not take the rejected work back") for n in store.notes(t.id))
    again = [e for e in on_bus(events, ASSIGNED, ticket=t.id) if e["data"]["role"] == "worker"][-1]
    assert "to" not in again["data"] and two.started[0][0] == again["data"]["assignment"]
    t = store.get_ticket(t.id)
    assert t.worker == "worker/2" and t.worker_turns == 2 and t.state == State.DONE


def test_two_reviewers_race_for_one_review_and_the_loser_starts_nothing(project):
    store, events, t = setup(project, "tasks")
    with FakeMachine(events, roles=("reviewer",), instance="1") as one, FakeMachine(events, roles=("reviewer",), instance="2") as two:
        run_scheduler(project, store, events, roles=("worker",))
    assert store.get_ticket(t.id).state == State.DONE
    review = on_bus(events, REVIEW_REQUESTED, ticket=t.id)[0]["data"]["assignment"]
    claims = [e["source"] for e in on_bus(events, ACCEPTED, assignment=review)]
    assert sorted(claims) == ["reviewer/1", "reviewer/2"]                                 # both claimed it
    winner, loser = (one, two) if claims[0] == "reviewer/1" else (two, one)
    assert winner.started == [(review, claims[0])] and loser.started == [] and loser.lost == [review]
    assert on_bus(events, RELEASED, ticket=t.id) == []                                    # nobody had to be told
    assert [e["source"] for e in on_bus(events, APPROVED, ticket=t.id)] == [claims[0]]


@pytest.mark.parametrize("work", ["tasks", "code"])
def test_a_worker_silent_past_its_lease_after_a_direct_hand_off_is_taken_back(project, monkeypatch, work):
    """No zombies: the worker takes its rejected work back directly from the reviewer, then says
    nothing; the scheduler, which saw every event, takes it back when the lease runs out."""
    store, events, t = setup(project, work)
    sched = Scheduler(project, store, events)
    sched.recover()
    sched.tick()
    offer = on_bus(events, ASSIGNED)[-1]["data"] | {"id": on_bus(events, ASSIGNED)[-1]["id"]}
    claim(events, offer)
    sched.tick()
    commit = None
    if work == "code":
        wt = offer["workspace"]
        git("commit", "--allow-empty", "-qm", "work", cwd=wt)
        commit = git("rev-parse", "HEAD", cwd=wt).strip()
    end(events, offer, REVIEW_REQUESTED, commit=commit)
    with FakeMachine(events, roles=("reviewer",), reject_first=True):
        until(sched, lambda: on_bus(events, CHANGES_REQUESTED, ticket=t.id))
    back = on_bus(events, CHANGES_REQUESTED, ticket=t.id)[0]["data"]
    claim(events, back, source="worker/1")                                                # it takes the work back, then goes silent
    sched.tick()
    t = store.get_ticket(t.id)
    assert t.state == State.IN_PROGRESS and t.owner == "worker/1" and store.assignments()[back["assignment"]]["instance"] == "worker/1"
    monkeypatch.setattr(scheduler_mod, "LEASE_S", 0.0)
    store.update_assignment(back["assignment"], lease_until=0)
    sched.tick()
    released = on_bus(events, RELEASED, assignment=back["assignment"])
    assert released and released[0]["data"]["reason"].startswith("no word from worker/1")
    t = store.get_ticket(t.id)
    assert t.state == State.OPEN and t.owner is None and store.notes(t.id)[-1]["kind"] == "crash"
    assert back["assignment"] not in store.assignments()
