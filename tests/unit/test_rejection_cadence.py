"""The planner (the coordinator role) is told at the threshold and its multiples, not on every
rejection; a send-back by the task system is not a reviewer's rejection. Driven over the bus: the
test plays the worker and the reviewers."""

from conftest import asked

from tasking.assignments import ACCEPTED, ASSIGNED, REPORTED, Follower
from tasking.events import Events
from tasking.model import State
from tasking.reports import NO_VERDICT_BUDGET
from tasking.scheduler import Scheduler
from tasking.store import LocalStore


def test_escalation_fires_at_the_threshold_and_its_multiples(project):
    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    sched = Scheduler(project, store, events)
    sched.recover()
    follow = Follower(events, start=0)
    m = store.create_milestone(name="m")
    t = store.create_ticket(title="hard", milestone_id=m.id)

    def turn(report) -> None:
        """One tick offers the ticket; the machine accepts and reports; the next tick applies it."""
        sched.tick()
        a = [e for e in follow.new() if e["type"] == ASSIGNED][-1]
        inst = f"{a['role']}/1"
        for kind, data in ((ACCEPTED, {"instance": inst}), (REPORTED, {"report": report, "failed": report is None})):
            events.publish(kind, {"assignment": a["assignment"], "ticket": t.id, "role": a["role"], **data}, source=inst, subject=t.id)
        sched.tick()

    fired = []
    for i in range(1, 8):
        turn({"state": "READY_FOR_REVIEW", "summary": "tried"})                       # the worker
        if i % 2:
            turn({"verdict": "REJECTED", "summary": "no", "feedback": "no"})          # a reviewer rejects
        else:
            for _ in range(NO_VERDICT_BUDGET + 1):                                    # the budget's reviews end without a
                turn(None)                                                            # verdict, then the task system sends it back
        t = store.get_ticket(t.id)
        assert t.state == State.REJECTED and [x["role"] for x in store.assignments().values()] == ["worker"]   # offered to a worker again
        assert t.rejected_by == ("reviewer" if i % 2 else "task_scheduler")           # the ticket says who sent it back
        if len(asked(events, "coordinator")) > len(fired):
            fired.append(i)
    assert fired == [3, 6]                                              # threshold 3: rounds 3 and 6, not 4, 5, 7
    t = store.get_ticket(t.id)
    assert t.review_rounds == 7 and t.reviewer_rejections == 4           # the task system's send-backs are not the reviewer's
    assert sum(n["kind"] == "review" for n in store.notes(t.id)) == 4
