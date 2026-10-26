"""The task system's metrics: levels and the counters that move them, from the same change."""

import pytest
from orchestra.metrics import for_project as metrics_for

from tasking.events import Events
from tasking.model import State
from tasking.store import LocalStore


@pytest.fixture
def store(project):
    return LocalStore(project, Events.for_project(project, source="worker/2"))


def test_ticket_levels_and_flows_balance(store):
    m = metrics_for(store.project.orchestra)
    key = store.project.key
    ms = store.create_milestone(name="m")
    a = store.create_ticket(title="a", milestone_id=ms.id)
    store.create_ticket(title="b", milestone_id=ms.id)
    a = store.transition(a.id, a.version, State.IN_PROGRESS)
    store.transition(a.id, a.version, State.READY_FOR_REVIEW)
    level = lambda s: m.latest("tasks_tickets", {"project": key, "state": s, "machine": "task_scheduler"})[1]
    assert (level("OPEN"), level("IN_PROGRESS"), level("READY_FOR_REVIEW"), level("DONE")) == (1, 0, 1, 0)
    assert m.latest("tasks_tickets_created_total", {"project": key, "machine": "worker"})[1] == 2       # worker/2 is the machine worker
    moved = lambda f, t: m.latest("tasks_ticket_transitions_total", {"project": key, "from": f, "to": t, "machine": "worker"})[1]
    assert moved("OPEN", "IN_PROGRESS") == 1 and moved("IN_PROGRESS", "READY_FOR_REVIEW") == 1
    # the counters live with the tickets, so another process (or a restart) carries on from them
    again = LocalStore(store.project, Events.for_project(store.project, source="worker/1"))
    again.create_ticket(title="c", milestone_id=ms.id)
    assert m.latest("tasks_tickets_created_total", {"project": key, "machine": "worker"})[1] == 3


def test_the_scheduler_reasserts_the_levels_every_discovery_step_without_a_change(project):
    """Between changes the levels are written again, once per discovery step, so a quiet system is
    seen measuring its stocks; no counter moves for it."""
    from tasking.scheduler import Scheduler

    events = Events.for_project(project, source="task_scheduler")
    store = LocalStore(project, events)
    ms = store.create_milestone(name="m")
    store.create_ticket(title="a", milestone_id=ms.id)
    m = metrics_for(project.orchestra)
    labels = {"project": project.key, "state": "OPEN", "machine": "task_scheduler"}
    created = lambda: len(m.raw("tasks_tickets_created_total", {"project": project.key, "machine": "task_scheduler"}))
    assert len(m.raw("tasks_tickets", labels)) == 1 and created() == 1            # the one change wrote both
    sched = Scheduler(project, store, events)
    sched.recover()
    store.set_operator_capacity(0, 0)                                               # nothing offered: no change
    sched.tick()
    levels = m.raw("tasks_tickets", labels)
    assert len(levels) == 2 and levels[-1][1] == 1.0 and created() == 1           # the level again, no counter
    assert all(len(m.raw("tasks_tickets", {**labels, "state": s.value})) == 2 for s in State)   # every state's level
    sched.tick()
    assert len(m.raw("tasks_tickets", labels)) == 2                                # within the step: not again
    sched.levels_at -= project.orchestra.config.discovery_step                      # a step later
    sched.tick()
    assert len(m.raw("tasks_tickets", labels)) == 3 and created() == 1
    assert m.raw("tasks_ticket_transitions_total", {"project": project.key}) == []  # no transition was ever counted
