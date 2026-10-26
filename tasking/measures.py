"""The task system's measurements, written to Orchestra's metrics store, named for what they
measure (Orchestra's discovery decides what is a stock and what is a flow):

- ``tasks_tickets{state}``: tickets in each state, every state written at every change;
- ``tasks_tickets_created_total{machine}``: tickets created, by the machine that created them;
- ``tasks_ticket_transitions_total{from, to, machine}``: state changes, by the machine that made
  them (the scheduler applies a worker's or reviewer's report; a planner's request is the
  scheduler's desk's).

A level and the counters that move it are taken from the same change, at the same instant, so
they balance exactly (a level sampled now and then would be off by whatever moved in between).
Counters are cumulative across processes and restarts: their values are kept with the tickets
(or the runtime record) under the store's lock. A metrics store that cannot be reached loses the
points, never a ticket change."""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

from orchestra.metrics import Point
from orchestra.metrics import for_project as metrics_for

from .model import State

if TYPE_CHECKING:
    from .config import Project


def machine_of(source: str | None) -> str:
    """``worker/1`` is the machine ``worker``."""
    return (source or "unknown").partition("/")[0]


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

    def ticket_points(self, tickets: dict, counts: dict, *, source: str | None, created: bool = False,
                      src: str | None = None, dst: str | None = None) -> list[Point]:
        """The points for one ticket change, from the ticket data as it now stands (``tickets``:
        id -> record). ``counts`` is updated in place and must be saved with the tickets."""
        t = time.time()
        machine = machine_of(source)
        out = []
        if created:
            labels = {"machine": machine}
            out.append(Point("tasks_tickets_created_total", float(self.count(counts, "created", labels)), t, self._labels(**labels)))
        if src and dst:
            labels = {"from": src, "to": dst, "machine": machine}
            out.append(Point("tasks_ticket_transitions_total", float(self.count(counts, "transition", labels)), t, self._labels(**labels)))
        by_state = {s.value: 0 for s in State}
        for d in tickets.values():
            by_state[d["state"]] = by_state.get(d["state"], 0) + 1
        out += [Point("tasks_tickets", float(n), t, self._labels(state=s, machine="task_scheduler")) for s, n in by_state.items()]
        return out
