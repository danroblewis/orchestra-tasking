"""What the scheduler makes of what comes back on the bus, driven by hand: the test plays the
machine. A report the task system cannot use never leaves a ticket held by nobody."""

import pytest
from conftest import claim, end, standing

from tasking.assignments import ACCEPTED, ASSIGNED, REPORTED, REVIEW_REQUESTED, WORKING
from tasking.events import Events
from tasking.model import State
from tasking.reports import NO_VERDICT_BUDGET, unusable
from tasking.scheduler import Scheduler
from tasking.store import LocalStore


@pytest.fixture
def env(project):
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    sched = Scheduler(project, store, events)
    sched.recover()
    ms = store.create_milestone(src="planner", name="m")
    t = store.create_ticket(src="planner", title="a task", milestone_id=ms.id, description="x", acceptance="y")
    return store, events, sched, t


def offer(events) -> dict:
    """The latest task.assigned on the bus."""
    return [e for e in events.after(0)[0] if e["type"] == ASSIGNED][-1]


def answer(events, a: dict, report, **more) -> None:
    """The machine ``<role>/1`` accepts the offer and reports ``report``."""
    inst = f"{a['role']}/1"
    say = lambda kind, **d: events.publish(kind, {"assignment": a["assignment"], "ticket": a["ticket"], "role": a["role"], **d},
                                           source=inst, subject=a["ticket"])
    say(ACCEPTED, instance=inst)
    say(REPORTED, report=report, **more)


def test_unusable_reports():
    """A report is for work that passes to no machine: a worker's BLOCKED, or none at all.
    Finished work and verdicts pass on directly (review.requested, changes.requested,
    review.approved), so in a report they are unusable."""
    assert unusable("worker", None) is None and unusable("reviewer", None) is None
    assert unusable("worker", {"state": "BLOCKED", "blocked_reason": "x"}) is None
    assert unusable("worker", {"state": "READY_FOR_REVIEW"}) == "a worker passes finished work on with review.requested, not task.reported"
    assert unusable("worker", {}) == "the report had no state"
    assert "blocked_reason" in unusable("worker", {"state": "BLOCKED", "summary": "x"})
    assert "DONE" in unusable("worker", {"state": "DONE"})
    assert unusable("reviewer", {"verdict": "APPROVED"}) == "a reviewer's verdict is changes.requested or review.approved, not task.reported"
    assert unusable("reviewer", "yes") == "the report was not an object"


@pytest.mark.parametrize("report, why", [({}, "the report had no state"),
                                         ({"state": "BLOCKED", "summary": "x"}, "BLOCKED without a blocked_reason"),
                                         ({"state": "READY_FOR_REVIEW", "summary": "x"}, "with review.requested")])
def test_a_workers_unusable_report_sends_the_ticket_back_as_a_crash(env, report, why):
    store, events, sched, t = env
    sched.tick()
    a = offer(events)
    answer(events, a, report)
    sched.tick()                                                   # applied: back to OPEN with the reason, then offered again
    notes = store.notes(t.id)
    assert notes[0]["kind"] == "crash" and why in notes[0]["text"] and notes[0]["author"] == "task_scheduler"
    t = store.get_ticket(t.id)
    assert t.state == State.OPEN and t.worker_turns == 1 and "ended without reporting" in t.feedback   # offered, not yet taken
    assert a["assignment"] not in store.assignments() and len(store.assignments()) == 1
    assert any(why in line for line in sched_log(sched))


def test_a_reviewers_unusable_report_redoes_the_review(env):
    store, events, sched, t = env
    sched.tick()
    claim(events, offer(events))
    end(events, offer(events), REVIEW_REQUESTED)
    sched.tick()                                                   # READY_FOR_REVIEW: the worker offered the review
    a = standing(events, store, "reviewer")
    assert a["type"] == REVIEW_REQUESTED
    answer(events, a, {"verdict": "MAYBE"})
    sched.tick()                                                   # no verdict: redone
    notes = store.notes(t.id)
    assert notes[-1]["kind"] == "crash" and "changes.requested or review.approved" in notes[-1]["text"]
    assert store.get_ticket(t.id).state == State.READY_FOR_REVIEW and a["assignment"] not in store.assignments()
    assert [x["role"] for x in store.assignments().values()] == ["reviewer"]   # offered again
    assert store.get_ticket(t.id).review_rounds == 0               # not a rejection
    for _ in range(NO_VERDICT_BUDGET):                             # the budget's reviews are redone; the one after sends it back
        answer(events, offer(events), None, failed=True)
        sched.tick()
    t = store.get_ticket(t.id)
    assert t.state == State.REJECTED and t.rejected_by == "task_scheduler" and t.reviewer_rejections == 0
    assert f"{NO_VERDICT_BUDGET + 1} reviews in a row ended without a verdict" in t.feedback


def test_a_report_the_store_refuses_still_frees_the_ticket(env, monkeypatch):
    store, events, sched, t = env
    sched.tick()
    a = offer(events)
    claim(events, a)
    end(events, a, REVIEW_REQUESTED)
    monkeypatch.setattr(sched.reports, "review_requested", lambda *args: (_ for _ in ()).throw(RuntimeError("disk full")))
    sched.tick()
    assert a["assignment"] not in store.assignments()
    assert store.get_ticket(t.id).state == State.OPEN and len(store.assignments()) == 1   # back to waiting, and offered again
    assert any("disk full" in n["text"] for n in store.notes(t.id))


def sched_log(sched) -> list[str]:
    from tasking import scheduler
    return scheduler.LOG.read_text().splitlines()


def test_a_heartbeat_keeps_the_lease_and_what_the_holder_said(env):
    """``task.working`` extends the lease; its ``text``, when sent, is what the holder is doing now,
    kept on the assignment for the Tasks panel (whoever holds the ticket, a program or not)."""
    store, events, sched, _t = env
    sched.tick()
    a = offer(events)
    say = lambda kind, **d: events.publish(kind, {"assignment": a["assignment"], "ticket": a["ticket"], "role": a["role"], **d},
                                           source="worker/1", subject=a["ticket"])
    say(ACCEPTED, instance="worker/1")
    sched.tick()
    lease = store.assignments()[a["assignment"]]["lease_until"]
    say(WORKING, text="reading the ticket")
    sched.tick()
    rec = store.assignments()[a["assignment"]]
    assert rec["text"] == "reading the ticket" and rec["lease_until"] >= lease
    say(WORKING)                                                   # a bare heartbeat keeps the last words
    sched.tick()
    assert store.assignments()[a["assignment"]]["text"] == "reading the ticket"
    events.publish(WORKING, {"assignment": a["assignment"], "ticket": a["ticket"], "role": "worker", "text": "not mine"},
                   source="worker/2", subject=a["ticket"])        # not the holder: ignored
    sched.tick()
    assert store.assignments()[a["assignment"]]["text"] == "reading the ticket"
