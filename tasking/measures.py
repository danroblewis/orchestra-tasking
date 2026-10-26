"""The task system's measurements, written to Orchestra's metrics store, named for what they
measure (Orchestra's discovery decides what is a stock and what is a flow):

- ``tasks_tickets{state}``: tickets in each state, every state written at every change, and
  written again, unchanged, every discovery step while the scheduler runs (``level_points``);
- ``tasks_tickets_created_total{machine, by}``: tickets created;
- ``tasks_ticket_transitions_total{from, to, machine, by}``: state changes.

On the counters ``machine`` is the writer, the machine whose record this is (the scheduler's,
``task_scheduler``: Orchestra's discovery balances a level with its own machine's counters
first), and ``by`` is the actor, the machine whose report or request the writer applied: the
``worker`` that reported READY_FOR_REVIEW, the ``reviewer`` whose verdict approved it, the
planner machine that asked for the ticket or cancelled it, or ``task_scheduler`` itself for its
own moves (a lease run out, a rejection for no verdict). Discovery puts a label's differing
values into a flow's name, so a flow reads "ticket transitions (IN_PROGRESS, READY_FOR_REVIEW,
worker)" and the machine that did the work is on the flow.

A level and the counters that move it are taken from the same change, at the same instant, so
they balance exactly (a level sampled now and then would be off by whatever moved in between).
Between changes the levels are re-asserted without the counters, so a reader can tell a quiet
system from one whose writer is gone: a level that is not written for a while looks like a
machine that stopped. Counters are cumulative across processes and restarts: their values are
kept with the tickets (or the runtime record) under the store's lock. A metrics store that
cannot be reached loses the points, never a ticket change."""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

from orchestra.events import machine_and_instance
from orchestra.metrics import Point
from orchestra.metrics import for_project as metrics_for

from .model import State

if TYPE_CHECKING:
    from .config import Project


class Measures:
    def __init__(self, project: Project):
        self.project = project
        self._store = None

    def _metrics(self):
        if self._store is None:
            self._store = metrics_for(self.project.orchestra)
        return self._store

    def write(self, points: list[Point]) -> None:
        if not points:
            return
        try:
            self._metrics().write(points)
        except Exception as e:  # noqa: BLE001 - measuring never breaks the work
            print(f"tasking: metrics not written ({type(e).__name__}: {e})", file=sys.stderr)

    def _labels(self, **labels: str) -> dict[str, str]:
        return {"project": self.project.key, **labels}

    @staticmethod
    def count(counts: dict, name: str, labels: dict) -> int:
        """Add one to a cumulative counter kept in ``counts`` (a dict saved under the store's lock)."""
        key = name + "|" + "|".join(f"{k}={v}" for k, v in sorted(labels.items()))
        counts[key] = counts.get(key, 0) + 1
        return counts[key]

    def ticket_points(self, tickets: dict, counts: dict, *, source: str | None, by: str | None = None, created: bool = False,
                      src: str | None = None, dst: str | None = None) -> list[Point]:
        """The points for one ticket change, from the ticket data as it now stands (``tickets``:
        id -> record); ``source`` is the writer's, ``by`` the actor's machine (the writer's own
        when None). ``counts`` is updated in place and must be saved with the tickets."""
        t = time.time()
        machine = machine_and_instance(source)[0] or "unknown"
        by = by or machine
        out = []
        if created:
            labels = {"machine": machine, "by": by}
            out.append(Point("tasks_tickets_created_total", float(self.count(counts, "created", labels)), t, self._labels(**labels)))
        if src and dst:
            labels = {"from": src, "to": dst, "machine": machine, "by": by}
            out.append(Point("tasks_ticket_transitions_total", float(self.count(counts, "transition", labels)), t, self._labels(**labels)))
        return out + self.level_points(tickets, t)

    def level_points(self, tickets: dict, t: float | None = None) -> list[Point]:
        """The levels as the tickets stand (``tickets``: id -> record): every state's count, no
        counter touched."""
        t = time.time() if t is None else t
        by_state = {s.value: 0 for s in State}
        for d in tickets.values():
            by_state[d["state"]] = by_state.get(d["state"], 0) + 1
        return [Point("tasks_tickets", float(n), t, self._labels(state=s, machine="task_scheduler")) for s, n in by_state.items()]
