"""``PREPARING`` (SPEC.md section 2): a worker's turn whose worktree is still being made. Its
transitions, where it falls back to, the pool slot a preparation holds, what is carried through
it, and how it shows on the board. The slow part itself is in tests/e2e/test_preparing_worktrees.py."""

import time

import pytest
from fastapi.testclient import TestClient
from orchestra.config import load_project as load_orchestra_project
from orchestra.web.app import create_app
from orchestra.web.auth import bearer

from tasking.events import Events
from tasking.model import TRANSITIONS, State, TransitionError, check_transition, fallback
from tasking.prepare import Prep
from tasking.reports import Reports
from tasking.scheduler import Scheduler
from tasking.store import LocalStore


@pytest.fixture
def rec(project):
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    ms = store.create_milestone(src="planner", name="m")
    return project, store, events, store.create_ticket(src="planner", title="a ticket", milestone_id=ms.id)


def test_preparing_comes_from_a_worker_waiting_state_and_goes_back_or_is_cancelled():
    assert {s for s, to in TRANSITIONS.items() if State.PREPARING in to} == {State.OPEN, State.REJECTED}
    assert TRANSITIONS[State.PREPARING] == {State.OPEN, State.REJECTED, State.CANCELLED}
    for no in (State.IN_PROGRESS, State.BLOCKED, State.DONE, State.READY_FOR_REVIEW):
        with pytest.raises(TransitionError):
            check_transition(State.PREPARING, no)                     # an offer goes out from where it waited


def test_a_preparing_ticket_falls_back_to_where_it_waited(rec):
    project, store, events, t = rec
    reports = Reports(project, store, events, None, lambda _m: None)
    t = store.transition(t.id, t.version, State.PREPARING, by="coordinator", preparing_from="OPEN")
    assert fallback(t) == State.OPEN
    reports.back_to_waiting(t.id, "the task system restarted")
    t = store.get_ticket(t.id)
    assert t.state == State.OPEN and t.preparing_from is None
    assert store.notes(t.id)[-1]["text"] == "the task system restarted; back to OPEN"
    t = store.transition(t.id, t.version, State.IN_PROGRESS, by="worker", owner="worker/1")
    t = store.transition(t.id, t.version, State.READY_FOR_REVIEW, by="worker")
    t = store.transition(t.id, t.version, State.IN_REVIEW, by="reviewer")
    t = store.transition(t.id, t.version, State.REJECTED, by="reviewer", feedback="redo", rejected_by="reviewer")
    t = store.transition(t.id, t.version, State.PREPARING, by="coordinator", preparing_from="REJECTED")
    reports.back_to_waiting(t.id, "its worktree could not be prepared")
    t = store.get_ticket(t.id)
    assert t.state == State.REJECTED and t.feedback == "redo"         # rework keeps what its worker is told


def test_a_slot_held_by_a_preparation_is_not_free_until_it_is_offered_or_dropped(rec):
    project, store, _events, t = rec
    store.put_preparing({"id": "T-1.worker.a", "ticket": t.id, "role": "worker", "from": "OPEN", "since": time.time()})
    slot, new = store.claim_slot("T-1.worker.a", t.id, 2)
    assert new and not store.pool()[slot["name"]]["free"]
    store.offer_prepared("T-1.worker.a", {"id": "T-1.worker.a", "ticket": t.id, "role": "worker"})
    assert store.preparing() == {} and not store.pool()[slot["name"]]["free"]   # the offer holds it now, in the same write
    store.drop_assignment("T-1.worker.a")
    assert store.pool()[slot["name"]]["free"]
    store.put_preparing({"id": "T-1.worker.b", "ticket": t.id, "role": "worker", "from": "OPEN", "since": time.time()})
    assert store.claim_slot("T-1.worker.b", t.id, 2)[0]["name"] == slot["name"]
    store.drop_preparing("T-1.worker.b")
    assert store.pool()[slot["name"]]["free"]


def test_what_the_offers_nobody_took_recorded_is_carried_through_preparing(rec):
    """Rejected work that already waited for its worker must not wait for it again because the
    ticket went through PREPARING (two moves, two versions)."""
    project, store, events, t = rec
    sched = Scheduler(project, store, events)
    store.set_untaken(t.id, "worker", {"ticket": t.id, "role": "worker", "misses": 2, "since": 1.0, "version": t.version,
                                       "returned": t.version, "again_at": 0.0})
    prep = Prep("T-1.worker.x", t.id, "worker", State.OPEN, lambda: None, version=t.version, moved=True)
    t = store.transition(t.id, t.version, State.PREPARING, by="coordinator", preparing_from="OPEN")
    sched._resting(store.tickets_by_id())                             # not forgotten while PREPARING
    t = store.transition(t.id, t.version, State.OPEN, by="coordinator", preparing_from=None)
    t = sched._carry_untaken(prep)
    rec_ = store.untaken()[f"{t.id}/worker"]
    assert rec_["version"] == rec_["returned"] == t.version and rec_["misses"] == 2


def test_the_board_and_the_views_show_a_preparation_with_its_role_and_since(rec):
    project, store, _events, t = rec
    store.transition(t.id, t.version, State.PREPARING, by="coordinator", preparing_from="OPEN")
    store.put_preparing({"id": "T-1.worker.a", "ticket": t.id, "role": "worker", "from": "OPEN", "since": 123.0})
    o = load_orchestra_project(project.repo_root)
    c = TestClient(create_app(o), headers=bearer(o))
    board = c.get("/api/plugins/tasks/board").json()
    card = next(x for x in board["tickets"] if x["id"] == t.id)
    assert card["state"] == "PREPARING" and card["preparing"] == {"role": "worker", "since": 123.0} and card["waiting"] is None
    assert board["counts"]["PREPARING"] == 1
    assert "tasks_tickets{state=\"PREPARING\"} 1" in c.get("/api/plugins/tasks/metrics").text
    assert "PREPARING" in c.get("/plugins/tasks/static/tasks.css").text
