"""The task system's metrics: levels and the counters that move them, from the same change."""

import pytest
from orchestra.metrics import for_project as metrics_for

from tasking.events import Events
from tasking.model import State
from tasking.store import LocalStore


@pytest.fixture
def store(project):
    return LocalStore(project, Events(project.events_file, source="worker/2"))


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
    again = LocalStore(store.project, Events(store.project.events_file, source="worker/1"))
    again.create_ticket(title="c", milestone_id=ms.id)
    assert m.latest("tasks_tickets_created_total", {"project": key, "machine": "worker"})[1] == 3
