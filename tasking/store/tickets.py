"""The tickets half of the store: tickets, milestones, notes, and the views the pages draw. One
JSON document (``tickets.json``) holds them with the counters behind the ids and the metric
counters. Every change is an observation on the bus (``ticket.*``, ``milestone.*``)."""

from __future__ import annotations

from typing import Any

from ..model import (
    ACTIVE,
    Milestone,
    MilestoneStatus,
    Note,
    State,
    Ticket,
    check_transition,
    effective_priorities,
    now,
)
from .base import Conflict, StoreError
from .files import read, write

EMPTY_TICKETS = {"tickets": {}, "milestones": {}, "notes": {}, "counters": {"ticket": 0, "milestone": 0}, "metric_counts": {}}


class Tickets:
    """Half of ``LocalStore`` (store/local.py), which supplies ``project``, ``events``, ``measures``,
    ``prefix``, ``_locked`` and the runtime half's ``assignments`` and ``capacity``."""

    def _t(self) -> dict:
        return read(self.project.tickets_file, EMPTY_TICKETS)

    def get_ticket(self, ticket_id: str) -> Ticket:
        d = self._t()["tickets"].get(ticket_id)
        if d is None:
            raise StoreError(f"no ticket {ticket_id}")
        return Ticket.from_dict(d)

    def list_tickets(self, *, state: State | None = None, milestone_id: str | None = None) -> list[Ticket]:
        out = [Ticket.from_dict(d) for d in self._t()["tickets"].values()]
        if state is not None:
            out = [t for t in out if t.state == state]
        if milestone_id is not None:
            out = [t for t in out if t.milestone_id == milestone_id]
        return sorted(out, key=lambda t: (t.priority, t.created_at, t.id))

    def tickets_by_id(self) -> dict[str, Ticket]:
        return {k: Ticket.from_dict(v) for k, v in self._t()["tickets"].items()}

    def create_ticket(self, **fields: Any) -> Ticket:
        with self._locked():
            data = self._t()
            ms = data["milestones"].get(fields.get("milestone_id") or "")
            if ms is None:
                raise StoreError(f"ticket needs an existing milestone; got {fields.get('milestone_id')!r}")
            if ms["status"] != MilestoneStatus.OPEN:
                raise StoreError(f"milestone {ms['id']} is closed and cannot receive tickets")
            for dep in fields.get("depends_on") or []:
                if dep not in data["tickets"]:
                    raise StoreError(f"unknown dependency {dep}")
            data["counters"]["ticket"] += 1
            tid = f"{self.prefix}-{data['counters']['ticket']}"
            t = Ticket(id=tid, **fields)
            data["tickets"][tid] = t.to_dict()
            points = self.measures.ticket_points(data["tickets"], data["metric_counts"], source=self.events.source, created=True)
            write(self.project.tickets_file, data)
        self.measures.write(points)
        self.events.emit("ticket.created", ticket=tid, title=t.title, by=t.created_by)
        return t

    def update_ticket(self, ticket_id: str, expected_version: int | None, **changes: Any) -> Ticket:
        with self._locked():
            data = self._t()
            d = data["tickets"].get(ticket_id)
            if d is None:
                raise StoreError(f"no ticket {ticket_id}")
            if expected_version is not None and d["version"] != expected_version:
                raise Conflict(f"{ticket_id} is at version {d['version']}, expected {expected_version}")
            if "milestone_id" in changes and changes["milestone_id"] != d["milestone_id"]:
                ms = data["milestones"].get(changes["milestone_id"] or "")
                if ms is None or ms["status"] != MilestoneStatus.OPEN:
                    raise StoreError("tickets can only be moved to an open milestone")
            if "depends_on" in changes:
                for dep in changes["depends_on"] or []:
                    if dep not in data["tickets"] or dep == ticket_id:
                        raise StoreError(f"bad dependency {dep}")
            if "state" in changes:
                raise StoreError("use transition() to change state")
            for k, v in changes.items():
                if k in Ticket.__dataclass_fields__:
                    d[k] = v
            d["version"] += 1
            d["updated_at"] = now()
            write(self.project.tickets_file, data)
            t = Ticket.from_dict(d)
        self.events.emit("ticket.updated", ticket=ticket_id, fields=sorted(changes))
        return t

    def transition(self, ticket_id: str, expected_version: int | None, to_state: State, **fields: Any) -> Ticket:
        with self._locked():
            data = self._t()
            d = data["tickets"].get(ticket_id)
            if d is None:
                raise StoreError(f"no ticket {ticket_id}")
            if expected_version is not None and d["version"] != expected_version:
                raise Conflict(f"{ticket_id} is at version {d['version']}, expected {expected_version}")
            src = State(d["state"])
            check_transition(src, to_state)
            if to_state == State.BLOCKED and not fields.get("blocked_reason"):
                raise StoreError("BLOCKED requires blocked_reason")
            for k, v in fields.items():
                if k in Ticket.__dataclass_fields__:
                    d[k] = v
            if to_state not in ACTIVE:                       # back to waiting: nobody owns it
                d["owner"] = fields.get("owner")
            d["state"] = to_state.value
            d["state_since"] = now()
            d["updated_at"] = d["state_since"]
            d["version"] += 1
            points = self.measures.ticket_points(data["tickets"], data["metric_counts"], source=self.events.source,
                                                 src=src.value, dst=to_state.value)
            write(self.project.tickets_file, data)
            t = Ticket.from_dict(d)
        self.measures.write(points)
        self.events.emit("ticket.transitioned", ticket=ticket_id, owner=t.owner, **{"from": src.value, "to": to_state.value})
        return t

    def reassert_levels(self) -> None:
        """The levels written again as they stand, without a change and without touching the
        counters: the scheduler does this every discovery step, so a quiet system is still seen
        measuring its stocks (tasking/measures.py)."""
        self.measures.write(self.measures.level_points(self._t()["tickets"]))

    def add_note(self, ticket_id: str, note: Note) -> None:
        with self._locked():
            data = self._t()
            if ticket_id not in data["tickets"]:
                raise StoreError(f"no ticket {ticket_id}")
            data["notes"].setdefault(ticket_id, []).append(note.to_dict())
            write(self.project.tickets_file, data)
        self.events.emit("ticket.note_added", ticket=ticket_id, author=note.author, kind=note.kind)

    def notes(self, ticket_id: str) -> list[dict]:
        return list(self._t()["notes"].get(ticket_id, []))

    def get_milestone(self, milestone_id: str) -> Milestone:
        d = self._t()["milestones"].get(milestone_id)
        if d is None:
            raise StoreError(f"no milestone {milestone_id}")
        return Milestone.from_dict(d)

    def list_milestones(self) -> list[Milestone]:
        return sorted((Milestone.from_dict(d) for d in self._t()["milestones"].values()),
                      key=lambda m: (m.order, m.created_at))

    def milestones_by_id(self) -> dict[str, Milestone]:
        return {k: Milestone.from_dict(v) for k, v in self._t()["milestones"].items()}

    def create_milestone(self, **fields: Any) -> Milestone:
        with self._locked():
            data = self._t()
            data["counters"]["milestone"] += 1
            mid = f"M{data['counters']['milestone']}"
            fields.setdefault("order", data["counters"]["milestone"])
            m = Milestone(id=mid, **fields)
            data["milestones"][mid] = m.to_dict()
            write(self.project.tickets_file, data)
        self.events.emit("milestone.created", milestone=mid, name=m.name)
        return m

    def update_milestone(self, milestone_id: str, expected_version: int | None, **changes: Any) -> Milestone:
        with self._locked():
            data = self._t()
            d = data["milestones"].get(milestone_id)
            if d is None:
                raise StoreError(f"no milestone {milestone_id}")
            if expected_version is not None and d["version"] != expected_version:
                raise Conflict(f"{milestone_id} is at version {d['version']}")
            if "status" in changes:
                raise StoreError("use close_milestone(); milestones cannot be reopened")
            for k in ("name", "description", "order"):
                if k in changes:
                    d[k] = changes[k]
            d["version"] += 1
            write(self.project.tickets_file, data)
            m = Milestone.from_dict(d)
        self.events.emit("milestone.updated", milestone=milestone_id)
        return m

    def close_milestone(self, milestone_id: str, expected_version: int | None) -> Milestone:
        with self._locked():
            data = self._t()
            d = data["milestones"].get(milestone_id)
            if d is None:
                raise StoreError(f"no milestone {milestone_id}")
            if d["status"] == MilestoneStatus.CLOSED:
                raise StoreError(f"{milestone_id} is already closed")
            if expected_version is not None and d["version"] != expected_version:
                raise Conflict(f"{milestone_id} is at version {d['version']}")
            open_tickets = [t["id"] for t in data["tickets"].values()
                            if t["milestone_id"] == milestone_id and t["state"] not in ("DONE", "CANCELLED")]
            if open_tickets:
                raise StoreError(f"cannot close {milestone_id}: unfinished tickets {', '.join(open_tickets)}")
            d["status"] = MilestoneStatus.CLOSED.value
            d["closed_at"] = now()
            d["version"] += 1
            write(self.project.tickets_file, data)
            m = Milestone.from_dict(d)
        self.events.emit("milestone.closed", milestone=milestone_id)
        return m

    def ticket_views(self) -> dict:
        """What the pages draw: every ticket with its readiness (why it waits, its priority with
        what changed it), the milestones in order, and the counts by state."""
        data = self._t()
        tickets = [Ticket.from_dict(d) for d in data["tickets"].values()]
        counts = {s.value: 0 for s in State}
        for x in tickets:
            counts[x.state.value] += 1
        by_id = {x.id: x for x in tickets}
        milestones = {k: Milestone.from_dict(v) for k, v in data["milestones"].items()}
        offered = {a["ticket"] for a in self.assignments().values() if a.get("role") == "worker"}   # held or offered to a worker
        cap = self.capacity()
        eff = effective_priorities(by_id, self.project.config.aging_days_per_level)

        def view(x: Ticket) -> dict:
            d = x.to_dict()
            d["priority_info"] = eff.get(x.id)
            if x.state == State.OPEN:
                waiting = [dep for dep in x.depends_on if dep not in by_id or by_id[dep].state != State.DONE]
                ms = milestones.get(x.milestone_id)
                d["waiting_on"] = waiting
                d["ready"] = not waiting and ms is not None and ms.status == MilestoneStatus.OPEN
                d["why_waiting"] = (f"waiting on {', '.join(waiting)}" if waiting
                                    else "milestone is closed" if ms is None or ms.status != MilestoneStatus.OPEN
                                    else "offered to a worker" if x.id in offered
                                    else "ready; worker capacity is 0" if cap["workers"] == 0
                                    else f"ready; waiting for a worker slot ({len(offered)}/{cap['workers']} in use)" if len(offered) >= cap["workers"]
                                    else "ready")
            return d
        return {"tickets": [view(x) for x in sorted(tickets, key=lambda x: (eff.get(x.id, {}).get("effective", x.priority), x.created_at))],
                "milestones": sorted(data["milestones"].values(), key=lambda m: m["order"]),
                "counts": counts}

    def snapshot(self) -> dict:
        """Everything at once, for the ``snapshot`` query: the views and the runtime record."""
        views = self.ticket_views()
        return {**views,
                "project": {"key": self.project.key, "repo_root": str(self.project.repo_root),
                            "base_branch": self.project.config.base_branch, "work": self.project.config.work},
                "capacity": self.capacity(),
                "assignments": list(self.assignments().values()),
                "worktrees": list(self.worktrees().values()),
                "gate_runs": self.gate_runs()[-20:],
                "pipeline": self.pipeline(),
                "pickup_paused": self.pickup_paused(),
                "probes": [p for p in self.probe_states().values() if not p.get("removed")],
                "active": sum(1 for t in views["tickets"] if State(t["state"]) in ACTIVE)}
